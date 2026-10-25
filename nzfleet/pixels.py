"""Whether a redrawn screenshot differs from the tracked one by more than rendering noise."""

from __future__ import annotations

import struct
import zlib
from typing import Optional

SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHANNELS = {2: 3, 6: 4}
# A channel within EDGE of the tracked one is antialiasing; a screenshot moved when more than SHARE of its channels differ by more.
EDGE = 24
SHARE = 0.0005


def decode(data: Optional[bytes]):
    """(width, height, channels, rows of bytes) for an 8-bit RGB or RGBA PNG without interlacing, else None."""
    if not data or not data.startswith(SIGNATURE):
        return None
    pos, header, idat = len(SIGNATURE), None, []
    while pos + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + size]
        pos += size + 12
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
    if header is None or header[2] != 8 or header[3] not in CHANNELS or header[6] != 0:
        return None
    width, height = header[0], header[1]
    step = CHANNELS[header[3]]
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    stride = width * step
    if len(raw) != height * (stride + 1):
        return None
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        row = bytearray(raw[start + 1:start + 1 + stride])
        _unfilter(raw[start], row, previous, step)
        rows.append(bytes(row))
        previous = row
    return width, height, step, rows


def _unfilter(kind: int, row: bytearray, up: bytearray, step: int) -> None:
    for i in range(len(row)):
        left = row[i - step] if i >= step else 0
        above = up[i]
        corner = up[i - step] if i >= step else 0
        if kind == 1:
            row[i] = (row[i] + left) & 255
        elif kind == 2:
            row[i] = (row[i] + above) & 255
        elif kind == 3:
            row[i] = (row[i] + (left + above) // 2) & 255
        elif kind == 4:
            guess = left + above - corner
            nearest = min((abs(guess - left), 0), (abs(guess - above), 1), (abs(guess - corner), 2))[1]
            row[i] = (row[i] + (left, above, corner)[nearest]) & 255


def moved(before: Optional[bytes], after: Optional[bytes], golden: bool = False) -> bool:
    """Whether `after` is a different picture. Golden shots are held exactly; others tolerate antialiasing."""
    if before == after:
        return False
    one, two = decode(before), decode(after)
    if one is None or two is None or one[:3] != two[:3]:
        return True
    differing = 0
    for a, b in zip(one[3], two[3]):
        if a == b:
            continue
        if golden:
            return True
        differing += sum(1 for x, y in zip(a, b) if abs(x - y) > EDGE)
    return differing > SHARE * one[0] * one[1] * one[2]


def size_of(data: Optional[bytes]):
    """(width, height) of a PNG, or None when it is missing or not one this module reads."""
    one = decode(data)
    return None if one is None else one[:2]


def share_changed(before: Optional[bytes], after: Optional[bytes]) -> Optional[float]:
    """The share of channels that differ between two same-sized pictures, or None when they cannot be compared."""
    one, two = decode(before), decode(after)
    if one is None or two is None or one[:3] != two[:3]:
        return None
    differing = sum(1 for a, b in zip(one[3], two[3]) if a != b for x, y in zip(a, b) if x != y)
    return differing / (one[0] * one[1] * one[2])
