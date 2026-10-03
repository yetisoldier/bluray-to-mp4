"""Generate the Windows application icon used by PyInstaller.

The repository keeps the source icon as simple drawing code so builds do not
need Pillow, ImageMagick, or a checked-in binary .ico file.
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path


SIZES = (16, 24, 32, 48, 64, 128, 256)


def _crc(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + _crc(kind, payload)


def _png(width: int, height: int, pixels: list[tuple[int, int, int, int]]) -> bytes:
    rows = []
    for y in range(height):
        start = y * width
        row = pixels[start : start + width]
        rows.append(b"\x00" + b"".join(bytes(px) for px in row))
    payload = zlib.compress(b"".join(rows), level=9)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", payload) + _chunk(b"IEND", b"")


def _blend(dst: tuple[int, int, int, int], src: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    alpha = sa / 255
    out_a = sa + da * (1 - alpha)
    if out_a <= 0:
        return (0, 0, 0, 0)
    return (
        round((sr * alpha + dr * (da / 255) * (1 - alpha)) / (out_a / 255)),
        round((sg * alpha + dg * (da / 255) * (1 - alpha)) / (out_a / 255)),
        round((sb * alpha + db * (da / 255) * (1 - alpha)) / (out_a / 255)),
        round(out_a),
    )


def _inside_rounded(x: float, y: float, size: int, radius: float) -> bool:
    pad = size * 0.06
    left, top = pad, pad
    right, bottom = size - pad, size - pad
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _draw(size: int) -> bytes:
    pixels: list[tuple[int, int, int, int]] = []
    cx, cy = size * 0.48, size * 0.48
    disc_r = size * 0.34
    hub_r = size * 0.105
    radius = size * 0.18

    for y in range(size):
        for x in range(size):
            samples = []
            for oy in (0.25, 0.75):
                for ox in (0.25, 0.75):
                    sx, sy = x + ox, y + oy
                    color = (0, 0, 0, 0)
                    if _inside_rounded(sx, sy, size, radius):
                        shade = int(27 + 24 * (sy / size))
                        color = (16, shade, 43, 255)
                    dist = math.hypot(sx - cx, sy - cy)
                    if dist <= disc_r:
                        ring = int(220 - 55 * (dist / disc_r))
                        color = _blend(color, (ring, ring + 8, 232, 248))
                    if dist <= hub_r:
                        color = _blend(color, (22, 30, 48, 255))
                    if hub_r * 1.65 <= dist <= hub_r * 1.9:
                        color = _blend(color, (122, 138, 163, 185))

                    # Cyan film/play tab in the lower-right, large enough to
                    # stay recognizable at 16px without relying on text.
                    if sx > size * 0.50 and sy > size * 0.46:
                        x1, y1 = size * 0.50, size * 0.50
                        x2, y2 = size * 0.83, size * 0.62
                        x3, y3 = size * 0.58, size * 0.84
                        denom = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
                        a = ((y2 - y3) * (sx - x3) + (x3 - x2) * (sy - y3)) / denom
                        b = ((y3 - y1) * (sx - x3) + (x1 - x3) * (sy - y3)) / denom
                        c = 1 - a - b
                        if a >= 0 and b >= 0 and c >= 0:
                            color = _blend(color, (35, 211, 238, 245))
                    samples.append(color)
            pixels.append(tuple(round(sum(s[i] for s in samples) / 4) for i in range(4)))
    return _png(size, size, pixels)


def build_icon(path: Path) -> None:
    images = [_draw(size) for size in SIZES]
    offset = 6 + 16 * len(images)
    directory = [struct.pack("<HHH", 0, 1, len(images))]
    image_data = []
    for size, data in zip(SIZES, images):
        width = 0 if size == 256 else size
        directory.append(
            struct.pack("<BBBBHHII", width, width, 0, 0, 1, 32, len(data), offset)
        )
        image_data.append(data)
        offset += len(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(directory + image_data))


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("packaging/app_icon.ico")
    build_icon(out)
