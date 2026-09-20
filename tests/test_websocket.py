"""The WebSocket helpers: handshake, frame encoding, client frame decoding."""

from __future__ import annotations

import io
import struct
import unittest

from dmgtracker.websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_TEXT,
    FrameError,
    accept_key,
    encode_close,
    encode_frame,
    encode_pong,
    encode_text,
    handshake_response,
    read_frame,
)


def masked_frame(payload: bytes, opcode: int = OPCODE_TEXT, mask: bytes = b"\x01\x02\x03\x04", final: bool = True) -> bytes:
    """A client frame as a browser would send it."""
    first = (0x80 if final else 0) | opcode
    length = len(payload)
    if length <= 125:
        header = bytes([first, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([first, 0x80 | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, 0x80 | 127]) + struct.pack("!Q", length)
    body = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + body


class HandshakeTests(unittest.TestCase):
    def test_accept_key_matches_the_rfc_example(self) -> None:
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_response_is_a_101_with_the_accept_header(self) -> None:
        response = handshake_response("dGhlIHNhbXBsZSBub25jZQ==").decode("ascii")
        self.assertTrue(response.startswith("HTTP/1.1 101 Switching Protocols\r\n"))
        self.assertIn("Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n", response)
        self.assertTrue(response.endswith("\r\n\r\n"))


class EncodeTests(unittest.TestCase):
    def test_short_text_frame(self) -> None:
        self.assertEqual(encode_text("hi"), b"\x81\x02hi")

    def test_medium_frame_uses_16_bit_length(self) -> None:
        payload = b"x" * 300
        frame = encode_frame(payload)
        self.assertEqual(frame[:4], b"\x81\x7e\x01\x2c")
        self.assertEqual(frame[4:], payload)

    def test_large_frame_uses_64_bit_length(self) -> None:
        payload = b"x" * 70000
        frame = encode_frame(payload)
        self.assertEqual(frame[:2], b"\x81\x7f")
        self.assertEqual(struct.unpack("!Q", frame[2:10])[0], 70000)

    def test_close_and_pong(self) -> None:
        self.assertEqual(encode_close(1000), b"\x88\x02\x03\xe8")
        self.assertEqual(encode_pong(b"ab"), b"\x8a\x02ab")


class ReadFrameTests(unittest.TestCase):
    def test_masked_text_frame_is_unmasked(self) -> None:
        frame = read_frame(io.BytesIO(masked_frame(b"hello")))
        self.assertEqual((frame.opcode, frame.payload, frame.final), (OPCODE_TEXT, b"hello", True))

    def test_lengths_over_125_and_over_65535(self) -> None:
        self.assertEqual(read_frame(io.BytesIO(masked_frame(b"y" * 300))).payload, b"y" * 300)
        self.assertEqual(len(read_frame(io.BytesIO(masked_frame(b"z" * 70000))).payload), 70000)

    def test_control_frames(self) -> None:
        self.assertEqual(read_frame(io.BytesIO(masked_frame(b"", OPCODE_PING))).opcode, OPCODE_PING)
        self.assertEqual(read_frame(io.BytesIO(masked_frame(b"\x03\xe8", OPCODE_CLOSE))).opcode, OPCODE_CLOSE)

    def test_end_of_stream_is_none(self) -> None:
        self.assertIsNone(read_frame(io.BytesIO(b"")))

    def test_unmasked_client_frame_is_an_error(self) -> None:
        with self.assertRaises(FrameError):
            read_frame(io.BytesIO(b"\x81\x02hi"))

    def test_truncated_frame_is_an_error(self) -> None:
        with self.assertRaises(FrameError):
            read_frame(io.BytesIO(masked_frame(b"hello")[:-2]))


if __name__ == "__main__":
    unittest.main()
