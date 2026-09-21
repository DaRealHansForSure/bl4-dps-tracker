"""
Item thumbnails: the gun pictures the inventory renders, exported as PNG files.

The item cards name a render target per gun (`TextureRenderTarget2D'/Engine/Transient.
SceneCaptureComponent2D_...:OakInventoryThumbnail_...'`, 512 x 256, RGBA16f) that the
game draws the gun into. The engine's `KismetRenderingLibrary.ExportRenderTarget` writes
such a float target as an OpenEXR file (ZIP-compressed half floats), which no browser
shows, so this module decodes that EXR with the standard library alone (zlib and struct
read half floats) and writes a PNG: linear light to sRGB, alpha kept, so the gun stands
free on the page. One picture per item id, next to the item name table.
"""

from __future__ import annotations

import struct
import time
import zlib
from array import array
from itertools import accumulate
from operator import add
from pathlib import Path
from typing import Callable

THUMBNAILS_DIR_NAME = "thumbnails"
EXR_MAGIC = b"\x76\x2f\x31\x01"
EXR_COMPRESSION_NONE = 0
EXR_COMPRESSION_ZIPS = 2
EXR_COMPRESSION_ZIP = 3
EXR_PIXEL_TYPE_HALF = 1
SCANLINES_PER_CHUNK = {EXR_COMPRESSION_NONE: 1, EXR_COMPRESSION_ZIPS: 1, EXR_COMPRESSION_ZIP: 16}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_COLOR_RGBA = 6
CHANNEL_ORDER = ("R", "G", "B", "A")
# The byte a stored difference stands for, once the predictor's bias is taken off.
UNBIAS_TABLE = bytes((value - 128) & 0xFF for value in range(256))


class ThumbnailError(ValueError):
    """The EXR file is not one this decoder handles."""


def thumbnail_path(folder: Path, item_id: str) -> Path:
    return folder / f"{item_id}.png"


def convert_exr_to_png(exr_path: Path, png_path: Path, rest: Callable[[], None] = lambda: time.sleep(0)) -> None:
    """Decode the exported render target and write it as an RGBA PNG. `rest` is called
    between the chunks of work (a few milliseconds each) so that, inside the game, the
    game thread gets the interpreter back whenever it asks."""
    width, height, planes = _decode_exr(exr_path.read_bytes(), rest)
    rest()
    png_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = png_path.with_suffix(".png.tmp")
    temporary.write_bytes(_encode_png(width, height, planes))
    temporary.replace(png_path)


# ------------------------------------------------------------------ EXR

def _decode_exr(data: bytes, rest: Callable[[], None] = lambda: None) -> tuple[int, int, dict[str, bytearray]]:
    """(width, height, 8-bit planes by channel name) of a scanline EXR with half-float
    R, G, B and A channels."""
    if data[:4] != EXR_MAGIC:
        raise ThumbnailError("not an EXR file")
    header, position = _read_header(data)
    channels = header.get("channels")
    if not channels or any(pixel_type != EXR_PIXEL_TYPE_HALF for _name, pixel_type in channels):
        raise ThumbnailError(f"unsupported channels {channels}")
    compression = header.get("compression", EXR_COMPRESSION_NONE)
    if compression not in SCANLINES_PER_CHUNK:
        raise ThumbnailError(f"unsupported compression {compression}")
    x_min, y_min, x_max, y_max = header["dataWindow"]
    width, height = x_max - x_min + 1, y_max - y_min + 1
    chunk_lines = SCANLINES_PER_CHUNK[compression]
    chunk_count = -(-height // chunk_lines)
    offsets = struct.unpack_from(f"<{chunk_count}Q", data, position)
    line_bytes = width * 2 * len(channels)
    planes = {name: bytearray(width * height) for name, _type in channels}
    to_srgb, to_linear_8bit = _half_tables(rest)
    for offset in offsets:
        rest()
        y, size = struct.unpack_from("<ii", data, offset)
        raw = data[offset + 8:offset + 8 + size]
        lines_in_chunk = min(chunk_lines, y_max - y + 1)
        expected = line_bytes * lines_in_chunk
        if compression == EXR_COMPRESSION_NONE:
            pixels = raw
        else:
            pixels = _unpredict(zlib.decompress(raw)) if size < expected else raw
        for line in range(lines_in_chunk):
            rest()
            row = y - y_min + line
            line_start = line * line_bytes
            for index, (name, _type) in enumerate(channels):
                start = line_start + index * width * 2
                halves = struct.unpack_from(f"<{width}H", pixels, start)
                table = to_linear_8bit if name == "A" else to_srgb
                planes[name][row * width:(row + 1) * width] = bytes([table[value] for value in halves])
    return width, height, planes


def _read_header(data: bytes) -> tuple[dict, int]:
    """The header attributes the decoder needs, and where the chunk offset table starts."""
    header: dict = {}
    position = 8
    while True:
        name, position = _c_string(data, position)
        if name == "":
            return header, position
        _type, position = _c_string(data, position)
        size = struct.unpack_from("<i", data, position)[0]
        position += 4
        value = data[position:position + size]
        position += size
        if name == "channels":
            header["channels"] = _read_channels(value)
        elif name == "compression":
            header["compression"] = value[0]
        elif name == "dataWindow":
            header["dataWindow"] = struct.unpack("<iiii", value)


def _read_channels(value: bytes) -> list[tuple[str, int]]:
    channels: list[tuple[str, int]] = []
    position = 0
    while position < len(value) - 1:
        name, position = _c_string(value, position)
        pixel_type = struct.unpack_from("<i", value, position)[0]
        channels.append((name, pixel_type))
        # pixel type, pLinear and reserved, x sampling, y sampling
        position += 16
    return channels


def _c_string(data: bytes, position: int) -> tuple[str, int]:
    end = data.index(b"\x00", position)
    return data[position:end].decode("latin-1"), end + 1


def _unpredict(packed: bytes) -> bytes:
    """Undo the EXR ZIP predictor: every byte after the first is stored as its difference
    to the previous byte plus 128, and the even and odd bytes of the result are kept in
    two halves. The running sum is taken with C-level tools (accumulate over ints, then
    the low byte of every sum through a memoryview): a Python loop over a megabyte would
    hold the interpreter for a tenth of a second."""
    if not packed:
        return b""
    differences = packed[:1] + packed[1:].translate(UNBIAS_TABLE)
    sums = array("q", accumulate(differences, add))
    reconstructed = bytes(memoryview(sums).cast("B")[0::8])
    half = (len(reconstructed) + 1) // 2
    interleaved = bytearray(len(reconstructed))
    interleaved[0::2] = reconstructed[:half]
    interleaved[1::2] = reconstructed[half:half + len(interleaved) // 2]
    return bytes(interleaved)


_tables: tuple[list[int], list[int]] | None = None
TABLE_SLICE = 4096


def _half_tables(rest: Callable[[], None] = lambda: None) -> tuple[list[int], list[int]]:
    """8-bit values for every half-float bit pattern: colour through the sRGB curve, alpha
    linear, both clamped to 0..1 (the capture is HDR linear light). Built once per
    process, in slices with a rest between them."""
    global _tables
    if _tables is not None:
        return _tables
    to_srgb: list[int] = []
    to_linear: list[int] = []
    for start in range(0, 65536, TABLE_SLICE):
        rest()
        for bits in range(start, start + TABLE_SLICE):
            value = struct.unpack("<e", struct.pack("<H", bits))[0]
            if value != value or value <= 0.0:
                value = 0.0
            elif value > 1.0:
                value = 1.0
            if value <= 0.0031308:
                srgb = 12.92 * value
            else:
                srgb = 1.055 * value ** (1 / 2.4) - 0.055
            to_srgb.append(round(srgb * 255))
            to_linear.append(round(value * 255))
    _tables = (to_srgb, to_linear)
    return _tables


# ------------------------------------------------------------------ PNG

def _encode_png(width: int, height: int, planes: dict[str, bytearray]) -> bytes:
    """An 8-bit RGBA PNG from the planes, one filter byte of zero per scanline."""
    rows = bytearray()
    channel_planes = [planes[name] for name in CHANNEL_ORDER]
    for row in range(height):
        if row % 8 == 0:
            time.sleep(0)
        rows.append(0)
        start, end = row * width, (row + 1) * width
        pixels = bytearray(width * 4)
        for index, plane in enumerate(channel_planes):
            pixels[index::4] = plane[start:end]
        rows += pixels
    header = struct.pack(">IIBBBBB", width, height, 8, PNG_COLOR_RGBA, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + _png_chunk(b"IEND", b"")


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
