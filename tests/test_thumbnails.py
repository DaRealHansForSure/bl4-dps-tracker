"""The thumbnail converter: a synthetic ZIP-compressed half-float EXR becomes a PNG whose
pixels are the sRGB values of the input."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from dmgtracker import thumbnails
from dmgtracker.thumbnails import ThumbnailError, convert_exr_to_png, thumbnail_key, thumbnail_path

WIDTH, HEIGHT = 20, 18
CHANNELS = ("A", "B", "G", "R")


def half(value: float) -> bytes:
    return struct.pack("<e", value)


def predict(data: bytes) -> bytes:
    """The EXR ZIP predictor: split even and odd bytes into two halves, then store every
    byte as its difference to the previous one plus 128."""
    split = data[0::2] + data[1::2]
    out = bytearray([split[0]]) if split else bytearray()
    for index in range(1, len(split)):
        out.append((split[index] - split[index - 1] + 128) & 0xFF)
    return bytes(out)


def make_exr(pixels: list[list[tuple[float, float, float, float]]], compression: int = thumbnails.EXR_COMPRESSION_ZIP) -> bytes:
    """A scanline EXR with A, B, G, R half channels in decreasing line order, like the
    engine writes it."""
    height, width = len(pixels), len(pixels[0])
    attributes = b""
    channel_list = b"".join(name.encode() + b"\x00" + struct.pack("<iBBBBii", 1, 0, 0, 0, 0, 1, 1) for name in CHANNELS) + b"\x00"
    attributes += b"channels\x00chlist\x00" + struct.pack("<i", len(channel_list)) + channel_list
    attributes += b"compression\x00compression\x00" + struct.pack("<i", 1) + bytes([compression])
    window = struct.pack("<iiii", 0, 0, width - 1, height - 1)
    attributes += b"dataWindow\x00box2i\x00" + struct.pack("<i", 16) + window
    attributes += b"displayWindow\x00box2i\x00" + struct.pack("<i", 16) + window
    attributes += b"lineOrder\x00lineOrder\x00" + struct.pack("<i", 1) + b"\x01"
    attributes += b"\x00"
    lines_per_chunk = thumbnails.SCANLINES_PER_CHUNK[compression]
    chunk_starts = list(range(0, height, lines_per_chunk))
    header = b"\x76\x2f\x31\x01" + struct.pack("<i", 2) + attributes
    offsets_position = len(header)
    body = b""
    offsets = []
    for start in reversed(chunk_starts):
        lines = []
        for y in range(start, min(start + lines_per_chunk, height)):
            for channel in CHANNELS:
                index = "RGBA".index(channel)
                lines.append(b"".join(half(pixel[index]) for pixel in pixels[y]))
        raw = b"".join(lines)
        packed = raw if compression == thumbnails.EXR_COMPRESSION_NONE else zlib.compress(predict(raw))
        if len(packed) >= len(raw):
            packed = raw
        offsets.append(offsets_position + 8 * len(chunk_starts) + len(body))
        body += struct.pack("<ii", start, len(packed)) + packed
    return header + struct.pack(f"<{len(offsets)}Q", *offsets) + body


def read_png(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == thumbnails.PNG_SIGNATURE
    position = 8
    width = height = 0
    idat = b""
    while position < len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        body = data[position + 8:position + 8 + length]
        if kind == b"IHDR":
            width, height = struct.unpack_from(">II", body)
        elif kind == b"IDAT":
            idat += body
        position += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    rows = b"".join(raw[row * stride + 1:(row + 1) * stride] for row in range(height))
    return width, height, rows


def srgb8(value: float) -> int:
    value = min(max(value, 0.0), 1.0)
    return round(255 * (12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055))


class ThumbnailKeyTests(unittest.TestCase):
    def test_a_seed_id_is_its_own_file_name(self) -> None:
        self.assertEqual(thumbnail_key("9bf02ca566b1c0010e1227cd"), "9bf02ca566b1c0010e1227cd")

    def test_a_per_launch_id_loses_the_characters_a_file_name_cannot_hold(self) -> None:
        """A co-op client has no item identities, so ids look like "instance:206" and the
        export failed with FileNotFoundError until the colon was replaced."""
        self.assertEqual(thumbnail_key("instance:206"), "instance-206")
        self.assertEqual(thumbnail_path(Path("pictures"), "instance:206").name, "instance-206.png")


class ThumbnailConversionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def pixels(self):
        return [[((x / WIDTH), (y / HEIGHT), 0.25, 1.0 if x < WIDTH // 2 else 0.0) for x in range(WIDTH)] for y in range(HEIGHT)]

    def check_round_trip(self, compression: int) -> None:
        pixels = self.pixels()
        exr = self.folder / "gun.exr"
        exr.write_bytes(make_exr(pixels, compression))
        png = thumbnail_path(self.folder, "ddbe5d456a2ec99a4c917f15")
        rests = []
        convert_exr_to_png(exr, png, rest=lambda: rests.append(1))
        width, height, rows = read_png(png.read_bytes())
        self.assertEqual((width, height), (WIDTH, HEIGHT))
        for y in range(HEIGHT):
            for x in range(WIDTH):
                red, green, blue, alpha = pixels[y][x]
                offset = (y * WIDTH + x) * 4
                self.assertEqual(tuple(rows[offset:offset + 4]), (srgb8(red), srgb8(green), srgb8(blue), round(alpha * 255)), f"pixel {x},{y}")
        self.assertGreater(len(rests), HEIGHT, "the converter rests at least once per scanline")

    def test_a_zip_compressed_exr_becomes_the_same_picture(self) -> None:
        self.check_round_trip(thumbnails.EXR_COMPRESSION_ZIP)

    def test_an_uncompressed_exr_too(self) -> None:
        self.check_round_trip(thumbnails.EXR_COMPRESSION_NONE)

    def test_values_outside_the_unit_range_are_clamped(self) -> None:
        pixels = [[(4.0, -1.0, float("nan"), 2.0)] * 2] * 2
        exr = self.folder / "hdr.exr"
        exr.write_bytes(make_exr(pixels))
        png = self.folder / "hdr.png"
        convert_exr_to_png(exr, png, rest=lambda: None)
        _width, _height, rows = read_png(png.read_bytes())
        self.assertEqual(tuple(rows[:4]), (255, 0, 0, 255))

    def test_a_file_that_is_not_an_exr_is_refused(self) -> None:
        other = self.folder / "other.exr"
        other.write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(ThumbnailError):
            convert_exr_to_png(other, self.folder / "other.png", rest=lambda: None)

    def test_the_predictor_round_trips(self) -> None:
        data = bytes(range(256)) * 3 + b"\x00\xff\x80"
        self.assertEqual(thumbnails._unpredict(predict(data)), data)
        self.assertEqual(thumbnails._unpredict(b""), b"")


if __name__ == "__main__":
    unittest.main()
