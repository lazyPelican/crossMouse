"""
Mouse Share - Windows Server
=============================
Run this on your WINDOWS PC (the one with the physical keyboard & mouse).

Usage:
    python server.py                       # Ubuntu is to the right (default)
    python server.py --side left           # Ubuntu is to the left
    python server.py --side right --port 47984

Switching:
    Ctrl+Alt+S   = toggle forwarding on/off  (works anytime)
    Screen edge  = move cursor to the configured edge to switch to Ubuntu
                   move cursor to the return edge on Ubuntu to switch back

Requirements:
    pip install pynput
"""

import argparse
import ctypes
import ctypes.wintypes
import queue
import socket
import sys
import threading
import time

from pynput import keyboard, mouse
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button

from protocol import PORT, Reader, pack

# ---------------------------------------------------------------------------
# Key serialization (Windows -> network)
# ---------------------------------------------------------------------------

def ser_key(key):
    """Serialize a pynput key to a (type, value) tuple for JSON."""
    if isinstance(key, Key):
        return ("s", key.name)
    if isinstance(key, KeyCode):
        if key.char is not None:
            return ("c", key.char)
        if key.vk is not None:
            return ("v", key.vk)
    return None


def ser_button(btn):
    return btn.name   # "left", "right", "middle"


# ---------------------------------------------------------------------------
# Hotkey definition: Ctrl + Alt + S
# ---------------------------------------------------------------------------

_MOD_KEYS = {Key.ctrl_l, Key.ctrl_r, Key.alt_l, Key.alt_r}


def _is_hotkey(held_keys, trigger_key):
    """Return True if Ctrl+Alt are held and trigger_key is 's'."""
    has_ctrl = Key.ctrl_l in held_keys or Key.ctrl_r in held_keys
    has_alt = Key.alt_l in held_keys or Key.alt_r in held_keys
    is_s = isinstance(trigger_key, KeyCode) and trigger_key.char == "s"
    return has_ctrl and has_alt and is_s


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

_OPPOSITE = {"right": "left", "left": "right", "top": "bottom", "bottom": "top"}


class Server:
    def __init__(self, side="right", port=PORT):
        self.side = side
        self.port = port
        self.forwarding = False

        # Network
        self.conn = None
        self._conn_lock = threading.Lock()
        self._out_q = queue.Queue()
        self._sender_stop = threading.Event()

        # Switch debounce
        self._switch_lock = threading.Lock()
        self._edge_cd = 0.0          # edge-detection cooldown timestamp

        # Input tracking
        self._held = set()            # currently-pressed keys (for hotkey detection)

        # Screen info
        u32 = ctypes.windll.user32
        self.scr_w = u32.GetSystemMetrics(0)
        self.scr_h = u32.GetSystemMetrics(1)
        self.cx = self.scr_w // 2     # anchor point (screen centre)
        self.cy = self.scr_h // 2

        # Listener handles
        self._kb = None
        self._ms = None

    # ---- networking -------------------------------------------------------

    def _send(self, msg):
        if self.conn:
            self._out_q.put(msg)

    def _send_now(self, msg):
        with self._conn_lock:
            if self.conn:
                try:
                    self.conn.sendall(pack(msg))
                except OSError:
                    self.conn = None

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

    def _net_thread(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(1)
        log(f"Listening on port {self.port} ...")
        while True:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._conn_lock:
                old = self.conn
                self.conn = conn
            if old:
                try:
                    old.close()
                except OSError:
                    pass
            log(f"Client connected from {addr[0]}")
            reader = Reader()
            try:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    for msg in reader.feed(data):
                        if msg.get("t") == "sb":          # client wants to switch back
                            self._request_switch(False)
            except OSError:
                pass
            with self._conn_lock:
                if self.conn is conn:
                    self.conn = None
            log("Client disconnected")
            if self.forwarding:
                self._request_switch(False)

    # ---- mode switching ---------------------------------------------------

    def _request_switch(self, to_fwd):
        threading.Thread(target=self._do_switch, args=(to_fwd,), daemon=True).start()

    def _do_switch(self, to_fwd):
        if not self._switch_lock.acquire(blocking=False):
            return
        try:
            if to_fwd == self.forwarding:
                return
            if to_fwd and not self.conn:
                log("No client connected!")
                return

            # 1. Stop current listeners
            self._stop_listeners()
            time.sleep(0.05)          # let Windows unhook cleanly

            # 2. If leaving forwarding mode, release all keys on client
            if not to_fwd and self.forwarding:
                self._release_remote_keys()
                self._send({"t": "sn"})          # tell client: normal mode

            self.forwarding = to_fwd
            self._held.clear()

            if to_fwd:
                # Centre cursor so we get maximum delta range before screen-clip
                ctypes.windll.user32.SetCursorPos(self.cx, self.cy)
                time.sleep(0.02)

                # Tell client: you're active, cursor enters from opposite side
                self._send({
                    "t": "sw",
                    "side": _OPPOSITE[self.side],
                    "yr": self.cy / self.scr_h,
                    "xr": self.cx / self.scr_w,
                })
                self._start_fwd()
                log(">> Forwarding to Ubuntu")
                try:
                    ctypes.windll.user32.MessageBeep(0x40)
                except Exception:
                    pass
            else:
                self._start_normal()
                log("<< Back to Windows")
                try:
                    ctypes.windll.user32.MessageBeep(0)
                except Exception:
                    pass
        finally:
            self._switch_lock.release()

    def _release_remote_keys(self):
        """Send key-up for every key we think is held on the remote side."""
        for key in list(self._held):
            k = ser_key(key)
            if k:
                self._send({"t": "ku", "k": k})

    # ---- listener management ----------------------------------------------

    def _stop_listeners(self):
        for listener in (self._kb, self._ms):
            if listener:
                try:
                    listener.stop()
                except Exception:
                    pass

    # ---- NORMAL mode (no suppression) -------------------------------------

    def _start_normal(self):
        self._kb = keyboard.Listener(
            on_press=self._nk_press,
            on_release=self._nk_release,
        )
        self._ms = mouse.Listener(on_move=self._nm_move)
        self._kb.start()
        self._ms.start()

    def _nk_press(self, key):
        self._held.add(key)
        if _is_hotkey(self._held, key):
            self._request_switch(True)

    def _nk_release(self, key):
        self._held.discard(key)

    def _nm_move(self, x, y):
        """Detect cursor hitting the configured screen edge."""
        now = time.time()
        if now - self._edge_cd < 1.0:
            return
        hit = False
        if self.side == "right" and x >= self.scr_w - 2:
            hit = True
        elif self.side == "left" and x <= 1:
            hit = True
        elif self.side == "bottom" and y >= self.scr_h - 2:
            hit = True
        elif self.side == "top" and y <= 1:
            hit = True
        if hit:
            self._edge_cd = now
            self._request_switch(True)

    # ---- FORWARDING mode (suppress=True) ----------------------------------
    #
    # Mouse forwarding uses a centre-and-snap loop:
    #   - Let Windows report the cursor's real proposed position.
    #   - Send the delta from the screen centre.
    #   - Snap the cursor back to centre so the next event is a fresh delta.
    #
    # This avoids accumulating stale deltas or clipping at the screen edges.

    def _start_fwd(self):
        self._kb = keyboard.Listener(
            on_press=self._fk_press,
            on_release=self._fk_release,
            suppress=True,
        )
        self._ms = mouse.Listener(
            on_move=self._fm_move,
            on_click=self._fm_click,
            on_scroll=self._fm_scroll,
            suppress=True,
        )
        self._kb.start()
        self._ms.start()

    def _fk_press(self, key):
        self._held.add(key)
        if _is_hotkey(self._held, key):
            self._request_switch(False)
            return  # don't forward the trigger key
        k = ser_key(key)
        if k:
            self._send({"t": "kd", "k": k})

    def _fk_release(self, key):
        self._held.discard(key)
        k = ser_key(key)
        if k:
            self._send({"t": "ku", "k": k})

    def _fm_move(self, x, y):
        dx = x - self.cx
        dy = y - self.cy
        if dx or dy:
            self._send({"t": "mm", "dx": dx, "dy": dy})
            ctypes.windll.user32.SetCursorPos(self.cx, self.cy)

    def _fm_click(self, x, y, button, pressed):
        self._send({"t": "mc", "b": ser_button(button), "p": pressed})

    def _fm_scroll(self, x, y, dx, dy):
        self._send({"t": "ms", "dx": dx, "dy": dy})

    # ---- main loop --------------------------------------------------------

    def run(self):
        banner = f"""
  Mouse Share - Server (Windows)
  ==============================
  Ubuntu position : {self.side}
  Hotkey          : Ctrl + Alt + S
  Screen          : {self.scr_w} x {self.scr_h}
  Port            : {self.port}
"""
        print(banner)
        threading.Thread(target=self._sender_loop, daemon=True).start()
        threading.Thread(target=self._net_thread, daemon=True).start()
        self._start_normal()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log("Shutting down.")
            self._sender_stop.set()
            self._stop_listeners()


def log(msg):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Mouse Share - run on your Windows PC to share keyboard & mouse with Ubuntu"
    )
    p.add_argument(
        "--side",
        default="right",
        choices=["left", "right", "top", "bottom"],
        help="Which side of your Windows screen the Ubuntu monitor is on (default: right)",
    )
    p.add_argument("--port", type=int, default=PORT, help=f"TCP port (default: {PORT})")
    args = p.parse_args()
    Server(side=args.side, port=args.port).run()
