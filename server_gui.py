"""
Mouse Share - Windows Server (GUI)
===================================
Run on your WINDOWS PC.  Build with:  build_windows.bat
"""

import base64, json, os, pathlib, socket, struct, subprocess, threading, time, queue, sys, ctypes, ctypes.wintypes, uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

try:
    from PIL import Image, ImageDraw
    import pystray
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False

# ── Embedded protocol ──────────────────────────────────────────────────

PORT = 47984
DISCOVERY_PORT = 47985
APP_ID = "MouseShare"
_H = "!I"
_HS = struct.calcsize(_H)

def pack(m):
    r = json.dumps(m, separators=(",", ":")).encode()
    return struct.pack(_H, len(r)) + r

class Reader:
    def __init__(self): self.buf = b""
    def feed(self, d):
        self.buf += d; out = []
        while len(self.buf) >= _HS:
            n, = struct.unpack(_H, self.buf[:_HS])
            if len(self.buf) < _HS + n: break
            try: out.append(json.loads(self.buf[_HS:_HS+n]))
            except Exception: pass
            self.buf = self.buf[_HS+n:]
        return out

# ── Helpers ─────────────────────────────────────────────────────────────

_OPP = {"right":"left","left":"right","top":"bottom","bottom":"top"}

def ser_key(k):
    if isinstance(k, Key): return ("s", k.name)
    if isinstance(k, KeyCode):
        if k.char is not None: return ("c", k.char)
        if k.vk  is not None: return ("v", k.vk)
    return None

def ser_btn(b): return b.name

def _is_hotkey(held, key):
    c = Key.ctrl_l in held or Key.ctrl_r in held
    a = Key.alt_l  in held or Key.alt_r  in held
    s = isinstance(key, KeyCode) and key.char == "s"
    return c and a and s

def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "?.?.?.?"

def _notify(title, message):
    try:
        safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
        safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
            f"$xml = '<toast><visual><binding template=\"ToastGeneric\"><text>{safe_title}</text><text>{safe_msg}</text></binding></visual></toast>';"
            "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$doc.LoadXml($xml);"
            f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{APP_ID}');"
            "$notifier.Show([Windows.UI.Notifications.ToastNotification]::new($doc));"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        try: ctypes.windll.user32.MessageBeep(0x40)
        except Exception: pass


def _create_mouse_icon(size=64):
    """Generate a mouse-cursor icon image (PIL Image)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Draw a simple mouse/pointer shape
    s = size
    # Mouse body (oval)
    body_x0, body_y0 = int(s*0.2), int(s*0.15)
    body_x1, body_y1 = int(s*0.8), int(s*0.85)
    d.rounded_rectangle([body_x0, body_y0, body_x1, body_y1], radius=int(s*0.25),
                        fill=(50, 50, 50, 255), outline=(200, 200, 200, 255), width=2)
    # Center line
    cx = s // 2
    d.line([(cx, body_y0 + int(s*0.08)), (cx, int(s*0.5))],
           fill=(200, 200, 200, 255), width=2)
    # Horizontal divider
    d.line([(body_x0 + 4, int(s*0.5)), (body_x1 - 4, int(s*0.5))],
           fill=(200, 200, 200, 255), width=2)
    # Scroll wheel
    wheel_y = int(s * 0.35)
    d.ellipse([cx-3, wheel_y-5, cx+3, wheel_y+5],
              fill=(100, 180, 255, 255), outline=(200, 200, 200, 255))
    return img


# ── App ─────────────────────────────────────────────────────────────────

class ServerApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Mouse Share — Server")
        self.root.geometry("460x680")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass

        # ── state ──
        self.running   = False
        self.forwarding = False
        self.conn      = None
        self._cLock    = threading.Lock()
        self._swLock   = threading.Lock()
        self._held     = set()
        self._edge_cd  = 0.0
        self._kb = self._ms = self._srv = None
        self._q  = queue.Queue()
        self._out_q = queue.Queue()
        self._sender_stop = threading.Event()
        self._discovery_stop = threading.Event()
        self._incoming_files = {}
        self._device_name = socket.gethostname()
        self._cursor_hidden = False
        self._saved_pos = None          # saved cursor pos before forwarding
        self._current_ip = _local_ip()
        self._tray_icon = None
        self._quitting = False

        u = ctypes.windll.user32
        self.SW, self.SH = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        self.CX, self.CY = self.SW // 2, self.SH // 2

        # Set window icon
        self._set_window_icon()

        self._build_ui()
        self._tick()
        self._refresh_ip()          # periodic IP check

    def _set_window_icon(self):
        """Set the window icon to our mouse icon."""
        if not _HAS_TRAY:
            return
        try:
            icon_img = _create_mouse_icon(32)
            # Convert PIL image to tkinter PhotoImage via temporary .ico
            import tempfile
            ico_path = os.path.join(tempfile.gettempdir(), "mouseshare_icon.ico")
            # Save as ICO with multiple sizes
            icon_img_16 = icon_img.resize((16, 16), Image.LANCZOS)
            icon_img_32 = icon_img.resize((32, 32), Image.LANCZOS)
            icon_img_48 = icon_img.resize((48, 48), Image.LANCZOS)
            icon_img_32.save(ico_path, format="ICO",
                           sizes=[(16,16), (32,32), (48,48)],
                           append_images=[icon_img_16, icon_img_48])
            self.root.iconbitmap(ico_path)
        except Exception:
            pass

    # ── UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        s = ttk.Style()
        s.configure("T.TLabel",   font=("Segoe UI", 15, "bold"))
        s.configure("S.TLabel",   font=("Segoe UI", 10))
        s.configure("G.TLabel",   font=("Segoe UI", 10, "bold"), foreground="#2E7D32")
        s.configure("A.TLabel",   font=("Segoe UI", 10, "bold"), foreground="#E65100")
        s.configure("R.TLabel",   font=("Segoe UI", 10, "bold"), foreground="#C62828")
        s.configure("B.TLabel",   font=("Segoe UI", 10, "bold"), foreground="#1565C0")
        s.configure("Big.TButton", font=("Segoe UI", 11))

        m = ttk.Frame(self.root, padding=20); m.pack(fill="both", expand=True)

        # title
        ttk.Label(m, text="Mouse Share Server", style="T.TLabel").pack(anchor="w")
        ttk.Separator(m).pack(fill="x", pady=(8, 14))

        # config
        c = ttk.Frame(m); c.pack(fill="x")
        ttk.Label(c, text="Ubuntu is on my:").grid(row=0, column=0, sticky="w", pady=3)
        self._side = tk.StringVar(value="Right")
        self._sideC = ttk.Combobox(c, textvariable=self._side,
                                   values=["Left","Right","Top","Bottom"],
                                   state="readonly", width=10)
        self._sideC.grid(row=0, column=1, sticky="w", padx=(10,0), pady=3)

        ttk.Label(c, text="Port:").grid(row=1, column=0, sticky="w", pady=3)
        self._port = tk.StringVar(value=str(PORT))
        self._portE = ttk.Entry(c, textvariable=self._port, width=8)
        self._portE.grid(row=1, column=1, sticky="w", padx=(10,0), pady=3)

        # local IP (dynamic label)
        f = ttk.Frame(m); f.pack(fill="x", pady=(10,0))
        ttk.Label(f, text="Your IP:", font=("Segoe UI", 9)).pack(side="left")
        self._ipLabel = ttk.Label(f, text=f"  {self._current_ip}",
                                  font=("Segoe UI", 11, "bold"), foreground="#1565C0")
        self._ipLabel.pack(side="left")
        ttk.Label(f, text="  (auto-detected)",
                  font=("Segoe UI", 8), foreground="#888").pack(side="left")

        # start / stop
        self._btnVar = tk.StringVar(value="Start Server")
        self._btn = ttk.Button(m, textvariable=self._btnVar,
                               command=self._toggle, style="Big.TButton")
        self._btn.pack(fill="x", pady=(14,14), ipady=6)

        # status box
        sf = ttk.LabelFrame(m, text="Status", padding=10); sf.pack(fill="x")
        for label_text, attr, default, sty in [
            ("Connection:", "_connL", "Stopped",          "R.TLabel"),
            ("Mode:",       "_modeL", "—",           "S.TLabel"),
            ("Hotkey:",     "_hkL",   "Ctrl + Alt + S",   "S.TLabel"),
        ]:
            r = ttk.Frame(sf); r.pack(fill="x", pady=2)
            ttk.Label(r, text=label_text, width=13, anchor="w").pack(side="left")
            lbl = ttk.Label(r, text=default, style=sty)
            lbl.pack(side="left")
            setattr(self, attr, lbl)

        # forwarding banner (hidden initially)
        self._banner = tk.Label(m, text="⮞  FORWARDING TO UBUNTU  ⮜\nPress Ctrl+Alt+S to return",
                                bg="#1565C0", fg="white",
                                font=("Segoe UI", 11, "bold"), pady=8)

        # file share
        fs = ttk.LabelFrame(m, text="File Share", padding=8); fs.pack(fill="x", pady=(10,0))
        row = ttk.Frame(fs); row.pack(fill="x")
        self._file_path = tk.StringVar(value="")
        self._fileE = ttk.Entry(row, textvariable=self._file_path)
        self._fileE.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", width=8, command=self._pick_file).pack(side="left", padx=(6,0))
        self._sendFileBtn = ttk.Button(fs, text="Send File to Ubuntu", command=self._send_selected_file)
        self._sendFileBtn.pack(fill="x", pady=(6,0))
        self._fileStatus = ttk.Label(fs, text="Received files save to Downloads\\MouseShareReceived", style="S.TLabel")
        self._fileStatus.pack(anchor="w", pady=(5,0))

        ttk.Label(m, text=f"Advertised as: {self._device_name}", style="S.TLabel").pack(anchor="w", pady=(10,0))

    def _tick(self):
        """Poll the queue for thread->GUI updates (runs every 80 ms)."""
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._append_log(val)
            elif kind == "conn":
                styles = {"connected":"G.TLabel","waiting":"A.TLabel",
                          "disconnected":"R.TLabel","stopped":"R.TLabel"}
                labels = {"connected":"Connected","waiting":"Waiting for client …",
                          "disconnected":"Disconnected","stopped":"Stopped"}
                self._connL.config(text=labels.get(val,val), style=styles.get(val,"S.TLabel"))
            elif kind == "mode":
                if val == "fwd":
                    self._modeL.config(text="Forwarding → Ubuntu", style="B.TLabel")
                    self._banner.pack(fill="x", pady=(8,0))
                    self.root.title("Mouse Share — FORWARDING")
                else:
                    self._modeL.config(text="Windows active", style="G.TLabel")
                    self._banner.pack_forget()
                    self.root.title("Mouse Share — Server")
            elif kind == "file_status":
                self._fileStatus.config(text=val)
            elif kind == "ip_changed":
                self._ipLabel.config(text=f"  {val}")
        self.root.after(80, self._tick)

    def _append_log(self, msg):
        pass

    def _ui(self, kind, val):
        """Thread-safe GUI update."""
        self._q.put((kind, val))

    # ── IP auto-refresh ─────────────────────────────────────────────────

    def _refresh_ip(self):
        """Check IP every 5 seconds and update if changed."""
        new_ip = _local_ip()
        if new_ip != self._current_ip:
            self._current_ip = new_ip
            self._ui("ip_changed", new_ip)
        self.root.after(5000, self._refresh_ip)

    # ── system tray ─────────────────────────────────────────────────────

    def _setup_tray(self):
        """Create system tray icon (call once)."""
        if not _HAS_TRAY:
            return
        icon_img = _create_mouse_icon(64)
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon("MouseShare", icon_img, "Mouse Share", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _tray_show(self, icon=None, item=None):
        """Restore window from tray."""
        self.root.after(0, self._do_show)

    def _do_show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_quit(self, icon=None, item=None):
        """Quit from tray menu."""
        self._quitting = True
        if self._tray_icon:
            self._tray_icon.stop()
        self.root.after(0, self._do_quit)

    def _do_quit(self):
        self._stop_server()
        self.root.destroy()

    def _on_close(self):
        """Minimize to system tray instead of quitting."""
        if _HAS_TRAY and not self._quitting:
            self.root.withdraw()
            # Setup tray on first minimize
            if not self._tray_icon:
                self._setup_tray()
        else:
            self._stop_server()
            if self._tray_icon:
                try: self._tray_icon.stop()
                except Exception: pass
            self.root.destroy()

    # ── start / stop ────────────────────────────────────────────────────

    def _toggle(self):
        if self.running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self):
        try:
            port = int(self._port.get())
            if not 1024 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number between 1024 and 65535.")
            return

        self.running = True
        self._btnVar.set("Stop Server")
        self._sideC.config(state="disabled")
        self._portE.config(state="disabled")
        self._ui("conn", "waiting")
        self._ui("mode", "normal")
        self._ui("log", "Server started")

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._srv.bind(("0.0.0.0", port))
        except OSError as e:
            messagebox.showerror("Port in use", f"Cannot bind to port {port}:\n{e}")
            self.running = False
            self._btnVar.set("Start Server")
            self._sideC.config(state="readonly")
            self._portE.config(state="normal")
            return
        self._srv.listen(1)
        self._srv.settimeout(1.0)           # so we can check self.running

        self._sender_stop.clear()
        self._discovery_stop.clear()
        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._discovery_loop, args=(port,), daemon=True).start()
        threading.Thread(target=self._net_loop, daemon=True).start()
        self._start_normal()

    def _stop_server(self):
        was_fwd = self.forwarding
        self.running = False
        self.forwarding = False
        self._discovery_stop.set()
        self._stop_listeners()
        self._show_cursor()             # always restore cursor on stop
        if was_fwd:
            self._release_remote()
            self._send({"t":"sn"})
        with self._cLock:
            if self.conn:
                try: self.conn.close()
                except Exception: pass
                self.conn = None
        self._sender_stop.set()
        if self._srv:
            try: self._srv.close()
            except Exception: pass
            self._srv = None
        self._btnVar.set("Start Server")
        self._sideC.config(state="readonly")
        self._portE.config(state="normal")
        self._ui("conn", "stopped")
        self._ui("mode", "normal")
        self._ui("log", "Server stopped")

    # ── networking ──────────────────────────────────────────────────────

    def _send(self, m):
        if self.conn:
            self._out_q.put(m)

    def _send_now(self, m):
        with self._cLock:
            if self.conn:
                try: self.conn.sendall(pack(m))
                except OSError: self.conn = None

    def _sender_loop(self):
        pending_mm = None
        next_mouse_flush = 0.0
        while not self._sender_stop.is_set():
            try:
                msg = self._out_q.get(timeout=0.004)
            except queue.Empty:
                msg = None

            if msg:
                if msg.get("t") == "mm":
                    if pending_mm:
                        pending_mm["dx"] += msg.get("dx", 0)
                        pending_mm["dy"] += msg.get("dy", 0)
                    else:
                        pending_mm = msg
                    now = time.perf_counter()
                    if now >= next_mouse_flush:
                        self._send_now(pending_mm)
                        pending_mm = None
                        next_mouse_flush = now + 0.006
                    continue
                if pending_mm:
                    self._send_now(pending_mm)
                    pending_mm = None
                self._send_now(msg)

            now = time.perf_counter()
            if pending_mm and now >= next_mouse_flush:
                self._send_now(pending_mm)
                pending_mm = None
                next_mouse_flush = now + 0.006

    def _discovery_loop(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            while not self._discovery_stop.is_set():
                # Build message fresh each time so IP changes are picked up
                msg = json.dumps({
                    "app": APP_ID,
                    "role": "server",
                    "name": self._device_name,
                    "port": port,
                }).encode("utf-8")
                try:
                    sock.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    pass
                self._discovery_stop.wait(1.0)
        finally:
            try: sock.close()
            except Exception: pass

    def _net_loop(self):
        while self.running:
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._cLock:
                old = self.conn; self.conn = conn
            if old:
                try: old.close()
                except Exception: pass
            self._ui("conn", "connected")
            self._ui("log", f"Client connected: {addr[0]}")
            rd = Reader()
            try:
                while self.running:
                    conn.settimeout(1.0)
                    try:
                        data = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not data: break
                    for msg in rd.feed(data):
                        if msg.get("t") == "sb":
                            self._req_switch(False)
                        elif msg.get("t") in ("fo", "fc"):
                            self._handle_file_msg(msg)
            except OSError:
                pass
            with self._cLock:
                if self.conn is conn: self.conn = None
            self._ui("conn", "waiting" if self.running else "stopped")
            self._ui("log", "Client disconnected")
            if self.forwarding:
                self._req_switch(False)

    # ── mode switching ──────────────────────────────────────────────────

    def _req_switch(self, to_fwd):
        threading.Thread(target=self._do_switch, args=(to_fwd,), daemon=True).start()

    def _do_switch(self, to_fwd):
        if not self._swLock.acquire(blocking=False): return
        try:
            if to_fwd == self.forwarding: return
            if to_fwd and not self.conn:
                self._ui("log", "No client connected!"); return

            self._stop_listeners(); time.sleep(0.05)

            if not to_fwd and self.forwarding:
                self._release_remote()
                self._send({"t":"sn"})

            self.forwarding = to_fwd
            self._held.clear()

            if to_fwd:
                # Save cursor position (at the edge) so we can restore it later
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                self._saved_pos = (pt.x, pt.y)

                # Hide cursor, then move to centre invisibly for delta capture
                self._hide_cursor()
                ctypes.windll.user32.SetCursorPos(self.CX, self.CY)
                time.sleep(0.02)

                side = self._side.get().lower()
                self._send({"t":"sw","side":_OPP[side],
                            "yr": pt.y / self.SH, "xr": pt.x / self.SW})
                self._start_fwd()
                self._ui("mode","fwd"); self._ui("log",">> Forwarding to Ubuntu")
                try: ctypes.windll.user32.MessageBeep(0x40)
                except Exception: pass
            else:
                # Restore cursor to the edge where it was, nudged slightly
                # inward so the normal-mode edge detection doesn't instantly
                # re-trigger a switch back to Ubuntu.
                self._show_cursor()
                self._edge_cd = time.time()   # 1-sec cooldown vs re-trigger
                if self._saved_pos:
                    sx, sy = self._saved_pos
                    side = self._side.get().lower()
                    # Nudge 6px inward from edge so listener won't fire
                    if   side == "right":  sx = min(sx, self.SW - 6)
                    elif side == "left":   sx = max(sx, 6)
                    elif side == "bottom": sy = min(sy, self.SH - 6)
                    elif side == "top":    sy = max(sy, 6)
                    ctypes.windll.user32.SetCursorPos(sx, sy)
                    self._saved_pos = None
                self._start_normal()
                self._ui("mode","normal"); self._ui("log","<< Back to Windows")
                try: ctypes.windll.user32.MessageBeep(0)
                except Exception: pass
        finally:
            self._swLock.release()

    def _hide_cursor(self):
        if not self._cursor_hidden:
            ctypes.windll.user32.ShowCursor(False)
            self._cursor_hidden = True

    def _show_cursor(self):
        if self._cursor_hidden:
            ctypes.windll.user32.ShowCursor(True)
            self._cursor_hidden = False

    def _release_remote(self):
        for k in list(self._held):
            sk = ser_key(k)
            if sk: self._send({"t":"ku","k":sk})

    # ── listeners ───────────────────────────────────────────────────────

    def _stop_listeners(self):
        for L in (self._kb, self._ms):
            if L:
                try: L.stop()
                except Exception: pass

    # normal mode (no suppression) ─────────────────────────────────────

    def _start_normal(self):
        self._kb = keyboard.Listener(on_press=self._nkP, on_release=self._nkR)
        self._ms = mouse.Listener(on_move=self._nmM)
        self._kb.start(); self._ms.start()

    def _nkP(self, k):
        self._held.add(k)
        if _is_hotkey(self._held, k): self._req_switch(True)
    def _nkR(self, k):
        self._held.discard(k)
    def _nmM(self, x, y):
        now = time.time()
        if now - self._edge_cd < 1.0: return
        side = self._side.get().lower()
        hit = ((side=="right"  and x >= self.SW-2) or
               (side=="left"   and x <= 1) or
               (side=="bottom" and y >= self.SH-2) or
               (side=="top"    and y <= 1))
        if hit:
            self._edge_cd = now; self._req_switch(True)

    # forwarding mode ─────────────────────────────────────────────────
    #
    # Keyboard: suppress=True  (keys only go to Ubuntu)
    # Mouse:    suppress=True + center-and-snap
    #   -> local mouse events are consumed, we compute delta from screen centre,
    #     send it, then snap the cursor back to centre.  The snap
    #     generates a synthetic move with dx=dy=0 which we skip.

    def _start_fwd(self):
        self._kb = keyboard.Listener(on_press=self._fkP, on_release=self._fkR, suppress=True)
        self._ms = mouse.Listener(on_move=self._fmM, on_click=self._fmC,
                                  on_scroll=self._fmS, suppress=True)
        self._kb.start(); self._ms.start()

    def _fkP(self, k):
        self._held.add(k)
        if _is_hotkey(self._held, k): self._req_switch(False); return
        sk = ser_key(k)
        if sk: self._send({"t":"kd","k":sk})
    def _fkR(self, k):
        self._held.discard(k)
        sk = ser_key(k)
        if sk: self._send({"t":"ku","k":sk})
    def _fmM(self, x, y):
        dx, dy = x - self.CX, y - self.CY
        if dx or dy:
            self._send({"t":"mm","dx":dx,"dy":dy})
            # Snap cursor back to centre so next delta is correct
            ctypes.windll.user32.SetCursorPos(self.CX, self.CY)
    def _fmC(self, x, y, b, p):
        self._send({"t":"mc","b":ser_btn(b),"p":p})
    def _fmS(self, x, y, dx, dy):
        self._send({"t":"ms","dx":dx,"dy":dy})

    # ── file sharing ────────────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(title="Choose a file to send")
        if path:
            self._file_path.set(path)

    def _send_selected_file(self):
        path = self._file_path.get().strip().strip('"')
        if not path:
            path = filedialog.askopenfilename(title="Choose a file to send")
            if path:
                self._file_path.set(path)
        if not path:
            return
        if not self.conn:
            messagebox.showerror("No client", "Connect the Ubuntu client before sending a file.")
            return
        threading.Thread(target=self._send_file, args=(path,), daemon=True).start()

    def _send_file(self, path):
        try:
            size = os.path.getsize(path)
            name = os.path.basename(path)
            fid = uuid.uuid4().hex
            self._ui("file_status", f"Sending {name} ...")
            self._send_now({"t":"fo","id":fid,"name":name,"size":size})
            sent = 0
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(49152)
                    if not chunk:
                        break
                    sent += len(chunk)
                    self._send_now({
                        "t":"fc",
                        "id":fid,
                        "data":base64.b64encode(chunk).decode("ascii"),
                        "done":False,
                    })
                    time.sleep(0)
                    if sent == size or sent % (1024 * 1024) < len(chunk):
                        self._ui("file_status", f"Sending {name}: {sent * 100 // max(size, 1)}%")
            self._send_now({"t":"fc","id":fid,"data":"","done":True})
            self._ui("file_status", f"Sent {name}")
            _notify("Mouse Share", f"Sent {name}")
            self._ui("log", f"Sent file: {name}")
        except Exception as e:
            self._ui("file_status", f"File send failed: {e}")
            self._ui("log", f"File send failed: {e}")

    def _recv_dir(self):
        path = pathlib.Path.home() / "Downloads" / "MouseShareReceived"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _unique_path(self, folder, name):
        safe = os.path.basename(name) or "received-file"
        target = folder / safe
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        i = 1
        while True:
            candidate = folder / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    def _handle_file_msg(self, msg):
        fid = msg.get("id")
        if not fid:
            return
        if msg.get("t") == "fo":
            target = self._unique_path(self._recv_dir(), msg.get("name", "received-file"))
            handle = open(target, "wb")
            self._incoming_files[fid] = {"path": target, "file": handle, "got": 0, "size": msg.get("size", 0)}
            self._ui("file_status", f"Receiving {target.name} ...")
            self._ui("log", f"Receiving file: {target.name}")
        elif msg.get("t") == "fc":
            item = self._incoming_files.get(fid)
            if not item:
                return
            if msg.get("done"):
                item["file"].close()
                self._incoming_files.pop(fid, None)
                self._ui("file_status", f"Received {item['path'].name}")
                _notify("Mouse Share", f"Received {item['path'].name}")
                self._ui("log", f"Saved file: {item['path']}")
                return
            data = base64.b64decode(msg.get("data", ""))
            item["file"].write(data)
            item["got"] += len(data)
            size = max(item["size"], 1)
            if item["got"] == item["size"] or item["got"] % (1024 * 1024) < len(data):
                self._ui("file_status", f"Receiving {item['path'].name}: {item['got'] * 100 // size}%")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    ServerApp().run()
