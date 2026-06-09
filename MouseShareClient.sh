#!/bin/bash
# ====================================================================
#  Mouse Share Client - Ubuntu
#
#  FIRST TIME: open a terminal in this folder and run:
#      chmod +x MouseShareClient.sh && ./MouseShareClient.sh
#
#  This installs dependencies, sets up permissions, creates a
#  desktop shortcut, and launches the app.
#  After that just double-click "Mouse Share" on your desktop.
# ====================================================================

SELF="$(readlink -f "$0")"

# Ensure common paths are available (desktop launches may have minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:$PATH"

# -- Re-launch inside a terminal if double-clicked from file manager --
NEED_SETUP=0
python3 -c "import tkinter, evdev" 2>/dev/null || NEED_SETUP=1
[ -w /dev/uinput ] 2>/dev/null || NEED_SETUP=1

if [ "$NEED_SETUP" = "1" ] && [ ! -t 0 ] && [ -z "$MOUSESHARE_RELAUNCHED" ]; then
    export MOUSESHARE_RELAUNCHED=1
    for term in gnome-terminal x-terminal-emulator konsole xfce4-terminal lxterminal mate-terminal xterm; do
        if command -v "$term" >/dev/null 2>&1; then
            case "$term" in
                gnome-terminal) exec gnome-terminal -- bash "$SELF" ;;
                *)              exec "$term" -e bash "$SELF" ;;
            esac
        fi
    done
fi

# -- First-time dependency setup --
if [ "$NEED_SETUP" = "1" ]; then
    echo ""
    echo "  ===================================="
    echo "   Mouse Share - First-Time Setup"
    echo "  ===================================="
    echo ""

    if ! command -v python3 &>/dev/null; then
        echo "  [1/4] Installing Python 3 ..."
        sudo apt-get update -qq
        sudo apt-get install -y python3
    else
        echo "  [1/4] Python 3 - OK"
    fi

    if ! python3 -c "import tkinter" 2>/dev/null; then
        echo "  [2/4] Installing tkinter ..."
        sudo apt-get install -y python3-tk
    else
        echo "  [2/4] tkinter - OK"
    fi

    if ! python3 -c "import evdev" 2>/dev/null; then
        echo "  [3/4] Installing evdev ..."
        PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        sudo apt-get install -y "python${PYVER}-dev" python3-pip 2>/dev/null || \
        sudo apt-get install -y python3-dev python3-pip 2>/dev/null || true
        pip3 install --break-system-packages evdev 2>/dev/null || \
        pip3 install evdev 2>/dev/null || \
        python3 -m pip install --break-system-packages evdev 2>/dev/null || \
        python3 -m pip install evdev
    else
        echo "  [3/4] evdev - OK"
    fi

    if [ ! -w /dev/uinput ]; then
        echo "  [4/4] Setting up /dev/uinput permissions ..."
        sudo chmod 666 /dev/uinput
        echo 'KERNEL=="uinput", MODE="0666"' | sudo tee /etc/udev/rules.d/99-uinput.rules >/dev/null
        sudo udevadm control --reload-rules 2>/dev/null || true
        echo "         Done - will persist after reboot."
    else
        echo "  [4/4] /dev/uinput - OK"
    fi

    echo ""
    echo "  Setup complete!"
fi

# -- Build the Exec line (full path to bash + script) --
EXEC_LINE="/bin/bash $SELF"

# -- Install to applications menu (always trusted, no GNOME allow-launch needed) --
APP_DIR="$HOME/.local/share/applications"
APP_FILE="$APP_DIR/mouseshare-client.desktop"
mkdir -p "$APP_DIR"
cat > "$APP_FILE" << APPEOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Mouse Share
Comment=Share keyboard and mouse from Windows to this PC
Exec=${EXEC_LINE}
Icon=input-mouse
Terminal=false
Categories=Utility;
APPEOF
chmod +x "$APP_FILE"

# -- Desktop shortcut (symlink to the app entry — stays trusted) --
DESKTOP_FILE="$HOME/Desktop/mouseshare-client.desktop"
if [ -d "$HOME/Desktop" ]; then
    # Remove old broken shortcut if present
    rm -f "$HOME/Desktop/MouseShareClient.desktop" 2>/dev/null || true
    # Copy (not symlink — GNOME doesn't always trust symlinks on Desktop)
    cp "$APP_FILE" "$DESKTOP_FILE" 2>/dev/null || true
    chmod +x "$DESKTOP_FILE" 2>/dev/null || true
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

# -- Auto-start on login --
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART_FILE="$AUTOSTART_DIR/mouseshare-client.desktop"
mkdir -p "$AUTOSTART_DIR"
# Remove old file
rm -f "$AUTOSTART_DIR/MouseShareClient.desktop" 2>/dev/null || true
cat > "$AUTOSTART_FILE" << AEOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Mouse Share
Comment=Auto-start Mouse Share client on login
Exec=${EXEC_LINE}
Icon=input-mouse
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
AEOF
chmod +x "$AUTOSTART_FILE"

# -- Write embedded Python to temp file and run it --
LOGFILE="$HOME/.mouseshare_launch.log"
PYFILE=$(mktemp /tmp/mouseshare_client_XXXXXX.py)
trap 'rm -f "$PYFILE"' EXIT

cat > "$PYFILE" << '___MOUSESHARE_PY___'
"""
Mouse Share - Ubuntu Client (GUI)
Works on BOTH Wayland and X11 via Linux uinput (kernel-level input).
"""

import base64, json, os, pathlib, socket, struct, subprocess, threading, time, queue, sys, uuid
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# -- Embedded protocol --

PORT = 47984
DISCOVERY_PORT = 47985
APP_ID = "MouseShare"
_H = "!I"
_HS = struct.calcsize(_H)

def pack(m):
    r = json.dumps(m, separators=(",",":")).encode()
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

# -- Input injection via evdev / uinput --

try:
    import evdev
    from evdev import UInput, ecodes
    _EVDEV_IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    evdev = None
    UInput = None
    _EVDEV_IMPORT_ERROR = exc

    class _MissingEcodes:
        def __getattr__(self, _name):
            return 0

    ecodes = _MissingEcodes()

# --- key mappings ---

_SPECIAL = {
    "alt_l": ecodes.KEY_LEFTALT,    "alt_r": ecodes.KEY_RIGHTALT,
    "alt_gr": ecodes.KEY_RIGHTALT,
    "ctrl_l": ecodes.KEY_LEFTCTRL,  "ctrl_r": ecodes.KEY_RIGHTCTRL,
    "shift_l": ecodes.KEY_LEFTSHIFT,"shift_r": ecodes.KEY_RIGHTSHIFT,
    "cmd": ecodes.KEY_LEFTMETA,     "cmd_l": ecodes.KEY_LEFTMETA,
    "cmd_r": ecodes.KEY_RIGHTMETA,
    "tab": ecodes.KEY_TAB,          "enter": ecodes.KEY_ENTER,
    "space": ecodes.KEY_SPACE,      "backspace": ecodes.KEY_BACKSPACE,
    "delete": ecodes.KEY_DELETE,    "esc": ecodes.KEY_ESC,
    "up": ecodes.KEY_UP,            "down": ecodes.KEY_DOWN,
    "left": ecodes.KEY_LEFT,        "right": ecodes.KEY_RIGHT,
    "home": ecodes.KEY_HOME,        "end": ecodes.KEY_END,
    "page_up": ecodes.KEY_PAGEUP,   "page_down": ecodes.KEY_PAGEDOWN,
    "caps_lock": ecodes.KEY_CAPSLOCK,
    "num_lock": ecodes.KEY_NUMLOCK,
    "scroll_lock": ecodes.KEY_SCROLLLOCK,
    "insert": ecodes.KEY_INSERT,
    "print_screen": ecodes.KEY_SYSRQ,
    "pause": ecodes.KEY_PAUSE,
    "menu": ecodes.KEY_COMPOSE,
    "f1": ecodes.KEY_F1,   "f2": ecodes.KEY_F2,   "f3": ecodes.KEY_F3,
    "f4": ecodes.KEY_F4,   "f5": ecodes.KEY_F5,   "f6": ecodes.KEY_F6,
    "f7": ecodes.KEY_F7,   "f8": ecodes.KEY_F8,   "f9": ecodes.KEY_F9,
    "f10": ecodes.KEY_F10, "f11": ecodes.KEY_F11, "f12": ecodes.KEY_F12,
    "f13": ecodes.KEY_F13, "f14": ecodes.KEY_F14, "f15": ecodes.KEY_F15,
    "f16": ecodes.KEY_F16, "f17": ecodes.KEY_F17, "f18": ecodes.KEY_F18,
    "f19": ecodes.KEY_F19, "f20": ecodes.KEY_F20,
}

_CHAR = {" ": ecodes.KEY_SPACE, "\t": ecodes.KEY_TAB,
          "\n": ecodes.KEY_ENTER, "\r": ecodes.KEY_ENTER}
for _i in range(26):
    _c = chr(ord("a") + _i)
    _k = getattr(ecodes, f"KEY_{_c.upper()}")
    _CHAR[_c] = _k
    _CHAR[_c.upper()] = _k

_DIGIT_KEYS = [ecodes.KEY_0, ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
               ecodes.KEY_5, ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8, ecodes.KEY_9]
for _i in range(10):
    _CHAR[str(_i)] = _DIGIT_KEYS[_i]
for _c, _k in zip(")!@#$%^&*(", _DIGIT_KEYS):
    _CHAR[_c] = _k

for _c, _k in [
    ("-", ecodes.KEY_MINUS),    ("_", ecodes.KEY_MINUS),
    ("=", ecodes.KEY_EQUAL),    ("+", ecodes.KEY_EQUAL),
    ("[", ecodes.KEY_LEFTBRACE),("{", ecodes.KEY_LEFTBRACE),
    ("]", ecodes.KEY_RIGHTBRACE),("}", ecodes.KEY_RIGHTBRACE),
    ("\\",ecodes.KEY_BACKSLASH),("|", ecodes.KEY_BACKSLASH),
    (";", ecodes.KEY_SEMICOLON),(":", ecodes.KEY_SEMICOLON),
    ("'", ecodes.KEY_APOSTROPHE),('"', ecodes.KEY_APOSTROPHE),
    ("`", ecodes.KEY_GRAVE),    ("~", ecodes.KEY_GRAVE),
    (",", ecodes.KEY_COMMA),    ("<", ecodes.KEY_COMMA),
    (".", ecodes.KEY_DOT),      (">", ecodes.KEY_DOT),
    ("/", ecodes.KEY_SLASH),    ("?", ecodes.KEY_SLASH),
]:
    _CHAR[_c] = _k

_VK = {0x20: ecodes.KEY_SPACE}
for _i in range(26):
    _VK[0x41 + _i] = getattr(ecodes, f"KEY_{chr(ord('A') + _i)}")
for _i in range(10):
    _VK[0x30 + _i] = _DIGIT_KEYS[_i]
for _v, _k in [
    (0xBA, ecodes.KEY_SEMICOLON), (0xBB, ecodes.KEY_EQUAL),
    (0xBC, ecodes.KEY_COMMA),     (0xBD, ecodes.KEY_MINUS),
    (0xBE, ecodes.KEY_DOT),       (0xBF, ecodes.KEY_SLASH),
    (0xC0, ecodes.KEY_GRAVE),     (0xDB, ecodes.KEY_LEFTBRACE),
    (0xDC, ecodes.KEY_BACKSLASH), (0xDD, ecodes.KEY_RIGHTBRACE),
    (0xDE, ecodes.KEY_APOSTROPHE),
]:
    _VK[_v] = _k

_BTN = {"left": ecodes.BTN_LEFT, "right": ecodes.BTN_RIGHT, "middle": ecodes.BTN_MIDDLE}


def _deser(k):
    if k is None: return None
    kind, val = k
    if kind == "s": return _SPECIAL.get(val)
    if kind == "c": return _CHAR.get(val) or _CHAR.get(val.lower())
    if kind == "v": return _VK.get(val)
    return None


# -- uinput device wrapper --

class Injector:
    def __init__(self, screen_w=1920, screen_h=1080):
        self._sw = screen_w
        self._sh = screen_h
        mouse_cap = {
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y,
                            ecodes.REL_WHEEL, ecodes.REL_HWHEEL],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE],
        }
        kb_cap = {
            ecodes.EV_KEY: list(range(1, 272)),
        }
        self._m = UInput(mouse_cap, name="mouseshare-mouse")
        self._k = UInput(kb_cap,    name="mouseshare-kb")

    def move_to(self, x, y):
        self._m.write(ecodes.EV_REL, ecodes.REL_X, -(self._sw + 500))
        self._m.write(ecodes.EV_REL, ecodes.REL_Y, -(self._sh + 500))
        self._m.syn()
        time.sleep(0.03)
        if x > 0 or y > 0:
            self._m.write(ecodes.EV_REL, ecodes.REL_X, x)
            self._m.write(ecodes.EV_REL, ecodes.REL_Y, y)
            self._m.syn()

    def move(self, dx, dy):
        if dx: self._m.write(ecodes.EV_REL, ecodes.REL_X, dx)
        if dy: self._m.write(ecodes.EV_REL, ecodes.REL_Y, dy)
        self._m.syn()

    def click(self, btn_name, pressed):
        code = _BTN.get(btn_name, ecodes.BTN_LEFT)
        self._m.write(ecodes.EV_KEY, code, 1 if pressed else 0)
        self._m.syn()

    def scroll(self, dx, dy):
        if dy: self._m.write(ecodes.EV_REL, ecodes.REL_WHEEL, dy)
        if dx: self._m.write(ecodes.EV_REL, ecodes.REL_HWHEEL, dx)
        self._m.syn()

    def key_down(self, code):
        self._k.write(ecodes.EV_KEY, code, 1)
        self._k.syn()

    def key_up(self, code):
        self._k.write(ecodes.EV_KEY, code, 0)
        self._k.syn()

    def close(self):
        try: self._m.close()
        except Exception: pass
        try: self._k.close()
        except Exception: pass


# -- helpers --

def _can_uinput():
    return (
        _EVDEV_IMPORT_ERROR is None
        and os.path.exists("/dev/uinput")
        and os.access("/dev/uinput", os.W_OK)
    )

def _screen_size():
    """Get LOGICAL screen resolution (what compositor uses for cursor coords)."""
    try:
        out = subprocess.check_output(["xrandr", "--current"],
                                      text=True, stderr=subprocess.DEVNULL)
        def _parse_output_line(line):
            for part in line.split():
                if "x" in part and "+" in part and part[0].isdigit():
                    res = part.split("+")[0]
                    w, h = res.split("x")
                    return int(w), int(h)
            return None

        for line in out.splitlines():
            if " connected primary " in line:
                r = _parse_output_line(line)
                if r: return r
        for line in out.splitlines():
            if " connected " in line and "+" in line:
                r = _parse_output_line(line)
                if r: return r
    except Exception:
        pass
    try:
        r = tk.Tk(); r.withdraw()
        w, h = r.winfo_screenwidth(), r.winfo_screenheight()
        r.destroy(); return w, h
    except Exception:
        pass
    return 1920, 1080

def _config_path():
    return pathlib.Path.home() / ".mouseshare_client.json"

def _load_config():
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_config(data):
    try:
        _config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass

def _notify(title, message):
    try:
        subprocess.Popen(
            ["notify-send", title, message],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# -- App --

class ClientApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Mouse Share - Client")
        self.root.geometry("540x720")
        self.root.minsize(400, 500)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.connected = False
        self.receiving = False
        self.sock = None
        self._sock_lock = threading.Lock()
        self._stop = threading.Event()
        self._discover_stop = threading.Event()
        self._connecting = False
        self._q    = queue.Queue()
        self._out_q = queue.Queue()
        self._incoming_files = {}
        self._config = _load_config()
        self._trusted = self._config.get("trusted_server") or {}
        self._held = []
        self._edge_cd = 0.0
        self.return_edge = "left"
        self.vx = 0
        self.vy = 0
        self.injector = None
        self._conn_target_host = ""
        self._new_discovery = None

        self.SW, self.SH = _screen_size()
        self._build_ui()

        # Cross-check with tkinter (more reliable on Wayland with scaling)
        try:
            tk_w = self.root.winfo_screenwidth()
            tk_h = self.root.winfo_screenheight()
            if tk_w > 0 and tk_h > 0:
                self.SW = min(self.SW, tk_w)
                self.SH = min(self.SH, tk_h)
        except Exception:
            pass
        try:
            self._scrL.config(text=f"{self.SW} x {self.SH}")
        except Exception:
            pass

        self._tick()
        self._sync_cursor_pos()
        threading.Thread(target=self._discovery_loop, daemon=True).start()

    def _build_ui(self):
        ff = "Helvetica"
        mf = "Monospace"
        s = ttk.Style()
        s.configure("T.TLabel", font=(ff,18,"bold"))
        s.configure("S.TLabel", font=(ff,12))
        s.configure("G.TLabel", font=(ff,12,"bold"), foreground="#2E7D32")
        s.configure("A.TLabel", font=(ff,12,"bold"), foreground="#E65100")
        s.configure("R.TLabel", font=(ff,12,"bold"), foreground="#C62828")
        s.configure("B.TLabel", font=(ff,12,"bold"), foreground="#1565C0")
        s.configure("Big.TButton", font=(ff,13))
        s.configure("TLabelframe.Label", font=(ff,11))
        s.configure("TLabel", font=(ff,12))
        s.configure("TEntry", font=(ff,12))
        s.configure("TCombobox", font=(ff,12))

        m = ttk.Frame(self.root, padding=20); m.pack(fill="both", expand=True)

        ttk.Label(m, text="Mouse Share Client", style="T.TLabel").pack(anchor="w")
        ttk.Separator(m).pack(fill="x", pady=(8,14))

        # uinput permission banner
        self._perm_frame = ttk.Frame(m)
        ttk.Label(self._perm_frame,
                  text="!! /dev/uinput not writable. Run once in terminal:",
                  foreground="#C62828", font=(ff, 9, "bold")).pack(anchor="w")
        cmd_frame = ttk.Frame(self._perm_frame)
        cmd_frame.pack(fill="x", pady=(2, 0))
        self._cmd_entry = ttk.Entry(cmd_frame, font=(mf, 10))
        self._cmd_entry.insert(0, "sudo chmod 666 /dev/uinput")
        self._cmd_entry.config(state="readonly")
        self._cmd_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(cmd_frame, text="Copy", width=5,
                   command=self._copy_fix_cmd).pack(side="right", padx=(5,0))
        if not _can_uinput():
            self._perm_frame.pack(fill="x", pady=(0, 10))

        # config
        c = ttk.Frame(m); c.pack(fill="x")
        ttk.Label(c, text="Server IP:").grid(row=0, column=0, sticky="w", pady=3)
        self._host = tk.StringVar(value=self._trusted.get("host", ""))
        self._hostE = ttk.Entry(c, textvariable=self._host, width=20)
        self._hostE.grid(row=0, column=1, sticky="w", padx=(10,0), pady=3)
        self._hostE.focus_set()

        ttk.Label(c, text="Port:").grid(row=1, column=0, sticky="w", pady=3)
        self._port = tk.StringVar(value=str(self._trusted.get("port", PORT)))
        self._portE = ttk.Entry(c, textvariable=self._port, width=8)
        self._portE.grid(row=1, column=1, sticky="w", padx=(10,0), pady=3)

        # connect button
        self._btnVar = tk.StringVar(value="Connect")
        self._btn = ttk.Button(m, textvariable=self._btnVar,
                               command=self._toggle, style="Big.TButton")
        self._btn.pack(fill="x", pady=(14,14), ipady=6)
        self._auto_status = ttk.Label(
            m,
            text="Auto-connect is listening for trusted Windows PC",
            style="S.TLabel",
        )
        self._auto_status.pack(anchor="w", pady=(0,8))

        # status
        sf = ttk.LabelFrame(m, text="Status", padding=10); sf.pack(fill="x")
        for txt, attr, dflt, sty in [
            ("Connection:", "_connL", "Disconnected", "R.TLabel"),
            ("Mode:",       "_modeL", "-",       "S.TLabel"),
            ("Screen:",     "_scrL",  f"{self.SW} x {self.SH}", "S.TLabel"),
        ]:
            r = ttk.Frame(sf); r.pack(fill="x", pady=2)
            ttk.Label(r, text=txt, width=13, anchor="w").pack(side="left")
            lbl = ttk.Label(r, text=dflt, style=sty); lbl.pack(side="left")
            setattr(self, attr, lbl)

        # receiving banner
        self._banner = tk.Label(
            m, text=">> RECEIVING INPUT <<\nMove to return edge or Ctrl+Alt+S on server",
            bg="#2E7D32", fg="white", font=(ff, 11, "bold"), pady=8)

        # file share
        fs = ttk.LabelFrame(m, text="File Share", padding=8)
        fs.pack(fill="x", pady=(10,0))
        row = ttk.Frame(fs); row.pack(fill="x")
        self._file_path = tk.StringVar(value="")
        self._fileE = ttk.Entry(row, textvariable=self._file_path)
        self._fileE.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse", width=8, command=self._pick_file).pack(side="left", padx=(6,0))
        self._sendFileBtn = ttk.Button(fs, text="Send File to Windows", command=self._send_selected_file)
        self._sendFileBtn.pack(fill="x", pady=(6,0))
        self._fileStatus = ttk.Label(fs, text="Received files save to ~/Downloads/MouseShareReceived", style="S.TLabel")
        self._fileStatus.pack(anchor="w", pady=(5,0))

        trusted_name = self._trusted.get("name")
        if trusted_name:
            self._auto_status.config(text=f"Trusted Windows PC: {trusted_name}")

    def _copy_fix_cmd(self):
        self.root.clipboard_clear()
        self.root.clipboard_append("sudo chmod 666 /dev/uinput")

    def _tick(self):
        while True:
            try: kind, val = self._q.get_nowait()
            except queue.Empty: break
            if kind == "log":   pass
            elif kind == "conn":
                mp = {"connected":("Connected","G.TLabel"),
                      "connecting":("Connecting ...","A.TLabel"),
                      "disconnected":("Disconnected","R.TLabel")}
                t, st = mp.get(val, (val,"S.TLabel"))
                self._connL.config(text=t, style=st)
            elif kind == "mode":
                if val == "recv":
                    self._modeL.config(text="Receiving input", style="B.TLabel")
                    self._banner.pack(fill="x", pady=(8,0))
                    self.root.title("Mouse Share - RECEIVING")
                else:
                    self._modeL.config(text="Idle", style="G.TLabel")
                    self._banner.pack_forget()
                    self.root.title("Mouse Share - Client")
            elif kind == "perm_ok":
                self._perm_frame.pack_forget()
            elif kind == "file_status":
                self._fileStatus.config(text=val)
            elif kind == "auto_status":
                self._auto_status.config(text=val)
            elif kind == "auto_connect":
                host, port, name = val
                if not self.connected and not self._connecting:
                    self._start_connect(host, port, name, silent=True)
        self.root.after(80, self._tick)

    # -- periodic real-cursor edge check (GUI thread) --
    def _sync_cursor_pos(self):
        """Query REAL cursor position via tkinter and trigger edge switch
        if at the return edge. Catches virtual-cursor drift from Wayland
        scaling or compositor clamping mismatch. Runs every 50ms."""
        if self.receiving and time.time() >= self._edge_cd:
            try:
                x = self.root.winfo_pointerx()
                y = self.root.winfo_pointery()
                margin = 5
                hit = ((self.return_edge == "left"   and x <= margin) or
                       (self.return_edge == "right"  and x >= self.SW - 1 - margin) or
                       (self.return_edge == "top"    and y <= margin) or
                       (self.return_edge == "bottom" and y >= self.SH - 1 - margin))
                if hit:
                    self.receiving = False
                    self._release_all()
                    self._send({"t": "sb"})
                    self._ui("mode", "idle")
                    self._ui("log", "<< Edge hit - switching back")
            except Exception:
                pass
        self.root.after(50, self._sync_cursor_pos)

    def _ui(self, k, v): self._q.put((k, v))

    # -- connect / disconnect --

    def _toggle(self):
        if self.connected or self._stop.is_set() is False and self.sock:
            self._disconnect()
        else:
            self._start_connect()

    def _start_connect(self, host=None, port=None, trusted_name=None, silent=False):
        host = (host or self._host.get()).strip()
        if not host:
            if not silent:
                messagebox.showerror("Missing IP", "Enter the Windows server IP address, or start the Windows server for auto-connect.")
            return
        try:
            port = int(port or self._port.get())
            if not 1024 <= port <= 65535: raise ValueError
        except ValueError:
            if not silent:
                messagebox.showerror("Invalid port", "Port must be 1024-65535.")
            return

        if _EVDEV_IMPORT_ERROR is not None:
            if not silent:
                messagebox.showerror(
                    "Missing dependency",
                    "The Linux 'evdev' package is required.\n\n"
                    "Install it with:\n"
                    "  python3 -m pip install evdev\n\n"
                    "Then restart Mouse Share Client.")
            return

        if not _can_uinput():
            if not silent:
                messagebox.showerror(
                    "Permission needed",
                    "Cannot write to /dev/uinput.\n\n"
                    "Run once in a terminal:\n"
                    "  sudo chmod 666 /dev/uinput\n\n"
                    "Then click Connect again.")
            return

        if not self.injector:
            try:
                self.injector = Injector(self.SW, self.SH)
                self._ui("perm_ok", True)
            except Exception as e:
                if not silent:
                    messagebox.showerror("uinput error", f"Cannot create virtual devices:\n{e}")
                return

        self._stop.clear()
        self._connecting = True
        self._conn_target_host = host
        self._new_discovery = None
        threading.Thread(target=self._sender_loop, daemon=True).start()
        self._btnVar.set("Disconnect")
        self._host.set(host)
        self._port.set(str(port))
        self._hostE.config(state="disabled")
        self._portE.config(state="disabled")
        self._ui("conn", "connecting")
        self._ui("auto_status", f"Connecting to {trusted_name or host}")
        threading.Thread(target=self._conn_loop, args=(host, port, trusted_name), daemon=True).start()

    def _disconnect(self):
        self._stop.set()
        self._connecting = False
        self.receiving = False
        self._release_all()
        if self.sock:
            try: self.sock.close()
            except Exception: pass
        self.connected = False
        self._btnVar.set("Connect")
        self._hostE.config(state="normal")
        self._portE.config(state="normal")
        self._ui("conn", "disconnected")
        self._ui("mode", "idle")

    def _on_close(self):
        self._discover_stop.set()
        self._disconnect()
        if self.injector:
            self.injector.close()
        self.root.destroy()

    # -- network --

    def _discovery_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", DISCOVERY_PORT))
            sock.settimeout(1.0)
            while not self._discover_stop.is_set():
                try:
                    data, addr = sock.recvfrom(2048)
                    msg = json.loads(data.decode("utf-8"))
                except socket.timeout:
                    continue
                except Exception:
                    continue
                if msg.get("app") != APP_ID or msg.get("role") != "server":
                    continue
                name = msg.get("name") or addr[0]
                port = int(msg.get("port") or PORT)
                # If already connected to this same IP, nothing to do
                if self.connected:
                    continue
                # If reconnecting to old IP but server is now at new IP,
                # signal the conn_loop to switch target
                if self._connecting:
                    if addr[0] != self._conn_target_host:
                        self._new_discovery = (addr[0], port, name)
                    continue
                self._ui("auto_status", f"Found {name}; auto-connecting")
                self._ui("auto_connect", (addr[0], port, name))
        finally:
            try: sock.close()
            except Exception: pass

    def _conn_loop(self, host, port, trusted_name=None):
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(5)
                sock.connect((host, port))
                sock.settimeout(1.0)
                self.sock = sock
                self.connected = True
                self._connecting = False
                self._new_discovery = None
                self._trusted = {"name": trusted_name or host, "host": host, "port": port}
                self._config["trusted_server"] = self._trusted
                _save_config(self._config)
                self._ui("conn", "connected")
                self._ui("auto_status", f"Trusted Windows PC: {self._trusted['name']}")

                rd = Reader()
                while not self._stop.is_set():
                    try: data = sock.recv(4096)
                    except socket.timeout: continue
                    if not data: break
                    for msg in rd.feed(data):
                        self._handle(msg)

            except (ConnectionRefusedError, socket.timeout):
                pass
            except OSError:
                pass

            self.receiving = False
            self._release_all()
            self._ui("mode", "idle")
            self._ui("conn", "connecting" if not self._stop.is_set() else "disconnected")
            if sock:
                try: sock.close()
                except Exception: pass
            self.sock = None; self.connected = False
            self._connecting = True   # keep True so we stay in this loop

            # Check if discovery found the server at a NEW IP
            nd = self._new_discovery
            if nd:
                self._new_discovery = None
                host, port, trusted_name = nd
                self._conn_target_host = host
                self._ui("auto_status", f"Server moved to {host}; reconnecting")
                continue   # retry immediately with new IP

            # Wait 3 seconds before retrying, but check for new discovery
            for _ in range(30):
                if self._stop.is_set(): return
                nd = self._new_discovery
                if nd:
                    self._new_discovery = None
                    host, port, trusted_name = nd
                    self._conn_target_host = host
                    self._ui("auto_status", f"Server moved to {host}; reconnecting")
                    break
                time.sleep(0.1)

    def _send(self, m):
        if self.sock:
            self._out_q.put(m)

    def _send_now(self, m):
        with self._sock_lock:
            if self.sock:
                try: self.sock.sendall(pack(m))
                except OSError: pass

    def _sender_loop(self):
        while not self._stop.is_set():
            try:
                msg = self._out_q.get(timeout=0.05)
            except queue.Empty:
                continue
            self._send_now(msg)

    # -- message handling --

    def _handle(self, msg):
        t = msg.get("t")

        if t == "sw":
            self.receiving = True
            self.return_edge = msg.get("side", "left")
            self._edge_cd = time.time() + 1.5

            yr = msg.get("yr", 0.5); xr = msg.get("xr", 0.5)
            y = max(0, min(int(yr * self.SH), self.SH - 1))
            x = max(0, min(int(xr * self.SW), self.SW - 1))

            if   self.return_edge == "left":   ex, ey = 0, y
            elif self.return_edge == "right":  ex, ey = self.SW - 1, y
            elif self.return_edge == "top":    ex, ey = x, 0
            elif self.return_edge == "bottom": ex, ey = x, self.SH - 1
            else:                              ex, ey = 0, self.SH // 2

            self.injector.move_to(ex, ey)
            self.vx, self.vy = ex, ey

            self._ui("mode", "recv")

        elif t == "sn":
            self.receiving = False
            self._release_all()
            self._ui("mode", "idle")

        elif t == "mm" and self.receiving:
            dx = msg.get("dx", 0)
            dy = msg.get("dy", 0)
            self.injector.move(dx, dy)
            self.vx = max(0, min(self.vx + dx, self.SW - 1))
            self.vy = max(0, min(self.vy + dy, self.SH - 1))
            self._check_edge()

        elif t == "mc" and self.receiving:
            self.injector.click(msg.get("b", "left"), msg.get("p", False))

        elif t == "ms" and self.receiving:
            self.injector.scroll(msg.get("dx", 0), msg.get("dy", 0))

        elif t == "kd" and self.receiving:
            code = _deser(msg.get("k"))
            if code is not None:
                self._held.append(code)
                self.injector.key_down(code)

        elif t == "ku" and self.receiving:
            code = _deser(msg.get("k"))
            if code is not None:
                try: self._held.remove(code)
                except ValueError: pass
                self.injector.key_up(code)

        elif t in ("fo", "fc"):
            self._handle_file_msg(msg)

    def _release_all(self):
        if self.injector:
            for code in self._held:
                try: self.injector.key_up(code)
                except Exception: pass
        self._held.clear()

    def _check_edge(self):
        if not self.receiving: return
        if time.time() < self._edge_cd: return
        margin = 3
        hit = ((self.return_edge == "left"   and self.vx <= margin) or
               (self.return_edge == "right"  and self.vx >= self.SW - 1 - margin) or
               (self.return_edge == "top"    and self.vy <= margin) or
               (self.return_edge == "bottom" and self.vy >= self.SH - 1 - margin))
        if hit:
            self.receiving = False
            self._release_all()
            self._send({"t": "sb"})
            self._ui("mode", "idle")

    # -- file sharing --

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
        if not self.connected:
            messagebox.showerror("Not connected", "Connect to the Windows server before sending a file.")
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
        except Exception as e:
            self._ui("file_status", f"File send failed: {e}")

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
        elif msg.get("t") == "fc":
            item = self._incoming_files.get(fid)
            if not item:
                return
            if msg.get("done"):
                item["file"].close()
                self._incoming_files.pop(fid, None)
                self._ui("file_status", f"Received {item['path'].name}")
                _notify("Mouse Share", f"Received {item['path'].name}")
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
    ClientApp().run()
___MOUSESHARE_PY___

echo "[$(date)] Launching Mouse Share Client..." >> "$LOGFILE"
python3 "$PYFILE" 2>> "$LOGFILE" || echo "[$(date)] Exit code: $?" >> "$LOGFILE"
