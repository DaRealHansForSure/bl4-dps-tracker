"""
The bits of RFC 6455 a local dashboard needs, with the standard library only.

Server side: answer the upgrade handshake, send text frames (unmasked, as servers must),
read the client's frames (masked, as clients must) to notice close and ping. No extensions,
no fragmentation on the sending side, no binary payloads. The functions are pure, so the
tests cover them without sockets.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from typing import BinaryIO

HANDSHAKE_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

FIN_BIT = 0x80
MASK_BIT = 0x80
OPCODE_MASK = 0x0F
LENGTH_MASK = 0x7F
LENGTH_16_BIT = 126
LENGTH_64_BIT = 127
MAX_7_BIT_LENGTH = 125
MAX_16_BIT_LENGTH = 0xFFFF
MAX_FRAME_BYTES = 1 << 20


def accept_key(client_key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((client_key.strip() + HANDSHAKE_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def handshake_response(client_key: str) -> bytes:
    """The complete 101 response for an upgrade request."""
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(client_key)}\r\n"
        "\r\n"
    ).encode("ascii")


def encode_frame(payload: bytes, opcode: int = OPCODE_TEXT) -> bytes:
    """One unmasked final frame, as a server sends it."""
    header = bytes([FIN_BIT | opcode])
    length = len(payload)
    if length <= MAX_7_BIT_LENGTH:
        header += bytes([length])
    elif length <= MAX_16_BIT_LENGTH:
        header += bytes([LENGTH_16_BIT]) + struct.pack("!H", length)
    else:
        header += bytes([LENGTH_64_BIT]) + struct.pack("!Q", length)
    return header + payload


def encode_text(text: str) -> bytes:
    return encode_frame(text.encode("utf-8"), OPCODE_TEXT)


def encode_close(code: int = 1000) -> bytes:
    return encode_frame(struct.pack("!H", code), OPCODE_CLOSE)


def encode_pong(payload: bytes) -> bytes:
    return encode_frame(payload, OPCODE_PONG)


@dataclass(frozen=True)
class Frame:
    opcode: int
    payload: bytes
    final: bool


class FrameError(ValueError):
    """A malformed or oversized frame; the connection should be closed."""


def read_frame(stream: BinaryIO) -> Frame | None:
    """Read one frame from a client. None when the stream ended cleanly."""
    head = stream.read(2)
    if len(head) < 2:
        return None
    first, second = head[0], head[1]
    final = bool(first & FIN_BIT)
    opcode = first & OPCODE_MASK
    masked = bool(second & MASK_BIT)
    length = second & LENGTH_MASK
    if length == LENGTH_16_BIT:
        length = struct.unpack("!H", _read_exactly(stream, 2))[0]
    elif length == LENGTH_64_BIT:
        length = struct.unpack("!Q", _read_exactly(stream, 8))[0]
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {length} bytes exceeds the limit")
    if not masked:
        raise FrameError("client frames must be masked")
    mask = _read_exactly(stream, 4)
    payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(_read_exactly(stream, length)))
    return Frame(opcode, payload, final)


def _read_exactly(stream: BinaryIO, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise FrameError("stream ended inside a frame")
        data += chunk
    return data
