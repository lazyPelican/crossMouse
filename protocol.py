"""
Mouse Share - Network Protocol
Shared between server (Windows) and client (Ubuntu).
Copy this file to BOTH machines.
"""

import json
import struct

PORT = 47984

# Message types
# mm = mouse move, mc = mouse click, ms = mouse scroll
# kd = key down, ku = key up
# sw = switch to client (forwarding active)
# sn = switch to normal (forwarding stopped)
# sb = switch back (client requests return to server)
# fo = file offer, fc = file chunk

_HDR = "!I"  # 4-byte big-endian unsigned int
_HDR_SZ = struct.calcsize(_HDR)


def pack(msg: dict) -> bytes:
    """Encode a message dict into length-prefixed JSON bytes."""
    raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    return struct.pack(_HDR, len(raw)) + raw


class Reader:
    """Streaming message decoder. Feed it raw bytes, get back complete messages."""

    __slots__ = ("buf",)

    def __init__(self):
        self.buf = b""

    def feed(self, data: bytes) -> list[dict]:
        self.buf += data
        msgs = []
        while len(self.buf) >= _HDR_SZ:
            (n,) = struct.unpack(_HDR, self.buf[:_HDR_SZ])
            if len(self.buf) < _HDR_SZ + n:
                break
            try:
                msgs.append(json.loads(self.buf[_HDR_SZ : _HDR_SZ + n]))
            except json.JSONDecodeError:
                pass  # skip corrupt messages
            self.buf = self.buf[_HDR_SZ + n :]
        return msgs
