"""
Mouse Share - Ubuntu Client
============================
Run this on your UBUNTU PC (the one you want to control remotely).

Usage:
    python3 client.py 192.168.1.10            # IP of your Windows PC
    python3 client.py 192.168.1.10 --port 47984

Requirements:
    pip3 install pynput

Notes:
    - Works on X11 out of the box.
    - On Wayland you may need to run with:
          sudo python3 client.py <IP>
      or switch to an X11 session, because Wayland restricts synthetic
      input injection for security reasons.
"""

import argparse
import socket
import sys
import threading
import time

from pynput.keyboard import Controller as KbCtrl, Key, KeyCode
from pynput.mouse import Button, Controller as MsCtrl

from protocol import PORT, Reader, pack

# ---------------------------------------------------------------------------
# Key deserialization (network -> pynput key)
# ---------------------------------------------------------------------------

# Simple VK-code map for the most common Windows virtual-key codes.
# Letters A-Z  = 0x41-0x5A
# Digits 0-9   = 0x30-0x39
_VK_MAP: dict[int, Key | KeyCode] = {0x20: Key.space}
for _i in range(26):
    _VK_MAP[0x41 + _i] = KeyCode(char=chr(ord("a") + _i))
for _i in range(10):
    _VK_MAP[0x30 + _i] = KeyCode(char=chr(ord("0") + _i))
# Punctuation (US keyboard layout)
for _vk, _ch in [
    (0xBA, ";"), (0xBB, "="), (0xBC, ","), (0xBD, "-"),
    (0xBE, "."), (0xBF, "/"), (0xC0, "`"), (0xDB, "["),
    (0xDC, "\\"), (0xDD, "]"), (0xDE, "'"),
]:
    _VK_MAP[_vk] = KeyCode(char=_ch)


def deser_key(k):
    """Deserialize a (type, value) tuple back to a pynput key object."""
    if k is None:
        return None
    kind, val = k
    if kind == "s":                          # special key (Key enum)
        try:
            return Key[val]
        except KeyError:
            return None
    if kind == "c":                          # character key
        return KeyCode(char=val)
    if kind == "v":                          # Windows virtual-key code
        return _VK_MAP.get(val)
    return None


_BTN_MAP = {"left": Button.left, "right": Button.right, "middle": Button.middle}


# ---------------------------------------------------------------------------
# Screen-size helper
# ---------------------------------------------------------------------------

def _get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the primary screen."""
    # Try tkinter first (usually available)
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.destroy()
        return w, h
    except Exception:
        pass

    # Fallback: parse xrandr
    try:
        import subprocess
        out = subprocess.check_output(["xrandr"], text=True)
        for line in out.splitlines():
            if " connected primary " in line or (" connected " in line and "+" in line):
                # e.g.  "eDP-1 connected primary 1920x1080+0+0 ..."
                for part in line.split():
                    if "x" in part and "+" in part:
                        res = part.split("+")[0]
                        w, h = res.split("x")
                        return int(w), int(h)
    except Exception:
        pass

    return 1920, 1080  # safe default


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, host: str, port: int = PORT):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

        self.receiving = False          # True while we're the active side
        self.return_edge: str = "left"  # edge that sends control back

        self.ms = MsCtrl()
        self.kb = KbCtrl()
        self._held_keys: list = []      # keys we pressed (for cleanup)

        self.scr_w, self.scr_h = _get_screen_size()
        self._edge_cd = 0.0             # cooldown for edge detection

    # ---- networking -------------------------------------------------------

    def _send(self, msg):
        if self.sock:
            try:
                self.sock.sendall(pack(msg))
            except OSError:
                pass

    # ---- event handling ---------------------------------------------------

    def _handle(self, msg):
        t = msg.get("t")

        # -- Server says: start receiving input --
        if t == "sw":
            self.receiving = True
            self.return_edge = msg.get("side", "left")
            self._edge_cd = time.time() + 1.0  # 1s grace before edge detection

            # Position cursor at the entry edge
            yr = msg.get("yr", 0.5)
            xr = msg.get("xr", 0.5)
            y = max(0, min(int(yr * self.scr_h), self.scr_h - 1))
            x = max(0, min(int(xr * self.scr_w), self.scr_w - 1))

            if self.return_edge == "left":
                self.ms.position = (1, y)
            elif self.return_edge == "right":
                self.ms.position = (self.scr_w - 2, y)
            elif self.return_edge == "top":
                self.ms.position = (x, 1)
            elif self.return_edge == "bottom":
                self.ms.position = (x, self.scr_h - 2)

            log(">> Receiving input")

        # -- Server says: stop receiving --
        elif t == "sn":
            self.receiving = False
            self._release_all()
            log("<< Idle")

        # -- Mouse move (relative delta) --
        elif t == "mm" and self.receiving:
            dx = msg.get("dx", 0)
            dy = msg.get("dy", 0)
            self.ms.move(dx, dy)
            self._check_return_edge()

        # -- Mouse click --
        elif t == "mc" and self.receiving:
            btn = _BTN_MAP.get(msg.get("b"))
            if btn:
                if msg.get("p"):
                    self.ms.press(btn)
                else:
                    self.ms.release(btn)

        # -- Mouse scroll --
        elif t == "ms" and self.receiving:
            self.ms.scroll(msg.get("dx", 0), msg.get("dy", 0))

        # -- Key down --
        elif t == "kd" and self.receiving:
            key = deser_key(msg.get("k"))
            if key:
                self._held_keys.append(key)
                try:
                    self.kb.press(key)
                except Exception:
                    pass

        # -- Key up --
        elif t == "ku" and self.receiving:
            key = deser_key(msg.get("k"))
            if key:
                try:
                    self._held_keys.remove(key)
                except ValueError:
                    pass
                try:
                    self.kb.release(key)
                except Exception:
                    pass

    def _release_all(self):
        """Release every key we think is currently held."""
        for key in self._held_keys:
            try:
                self.kb.release(key)
            except Exception:
                pass
        self._held_keys.clear()

    def _check_return_edge(self):
        """If cursor hit the return edge, ask the server to take back control."""
        if not self.receiving:
            return
        if time.time() < self._edge_cd:
            return

        x, y = self.ms.position
        hit = False
        if self.return_edge == "left" and x <= 0:
            hit = True
        elif self.return_edge == "right" and x >= self.scr_w - 1:
            hit = True
        elif self.return_edge == "top" and y <= 0:
            hit = True
        elif self.return_edge == "bottom" and y >= self.scr_h - 1:
            hit = True

        if hit:
            self.receiving = False
            self._release_all()
            self._send({"t": "sb"})
            log("<< Edge hit - switching back to Windows")

    # ---- main loop (auto-reconnect) ---------------------------------------

    def run(self):
        banner = f"""
  Mouse Share - Client (Ubuntu)
  ==============================
  Server       : {self.host}:{self.port}
  Screen       : {self.scr_w} x {self.scr_h}
  Switch back  : move cursor to return edge, or Ctrl+Alt+S on server
"""
        print(banner)

        while True:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock.connect((self.host, self.port))
                log(f"Connected to {self.host}")

                reader = Reader()
                while True:
                    data = self.sock.recv(4096)
                    if not data:
                        break
                    for msg in reader.feed(data):
                        self._handle(msg)

            except ConnectionRefusedError:
                log("Connection refused - retrying in 3s ...")
            except (ConnectionError, OSError) as exc:
                log(f"Disconnected ({exc})")

            self.receiving = False
            self._release_all()
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            time.sleep(3)
            log("Reconnecting ...")


def log(msg):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Mouse Share - run on your Ubuntu PC to receive keyboard & mouse from Windows"
    )
    p.add_argument("host", help="IP address (or hostname) of the Windows server")
    p.add_argument("--port", type=int, default=PORT, help=f"TCP port (default: {PORT})")
    args = p.parse_args()
    Client(host=args.host, port=args.port).run()
