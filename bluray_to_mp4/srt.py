"""SRT subtitle handling: parse, clean, and write SubRip files.

Subtitle Edit's Tesseract OCR is ~95-99% accurate on film PGS tracks, so
the normalizer is deliberately conservative: fix numbering, timestamps,
encoding artifacts, and blank cues, but never touch cue text content
beyond stripping stray whitespace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Cue", "SrtError", "parse_srt", "normalize_cues", "write_srt"]

_TIMING = re.compile(
    r"^\s*(\d{1,2}):(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?\s*-->\s*"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?\s*$"
)


class SrtError(ValueError):
    """Unusable SRT content."""


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str


def _to_ms(h: str, m: str, s: str, ms: str | None) -> int:
    return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + int((ms or "").ljust(3, "0")[:3])


def _fmt_time(total_ms: int) -> str:
    ms = total_ms % 1000
    total_s = total_ms // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt(content: str) -> list[Cue]:
    """Parse SRT text into cues; tolerant of index lines and BOM."""
    content = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        # Drop a leading numeric index line if present.
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        match = _TIMING.match(lines[0])
        if not match:
            raise SrtError(f"unparsable cue timing: {lines[0][:60]!r}")
        g = match.groups()
        start = _to_ms(*g[:4])
        end = _to_ms(*g[4:])
        text = "\n".join(ln.rstrip() for ln in lines[1:]).strip("\n")
        cues.append(Cue(start, end, text))
    return cues


def normalize_cues(cues: list[Cue]) -> list[Cue]:
    """Clean OCR'd cues: drop empties, clamp bad durations, sort by time."""
    out: list[Cue] = []
    for cue in cues:
        text = cue.text.strip()
        if not text:
            continue
        start, end = cue.start_ms, cue.end_ms
        if end < start:  # OCR artifacts occasionally invert timestamps.
            start, end = end, start
        if end == start:  # zero-length cue: hold for 1s.
            end = start + 1000
        out.append(Cue(start, end, text))
    out.sort(key=lambda c: (c.start_ms, c.end_ms))
    return out


def write_srt(cues: list[Cue]) -> str:
    """Render cues as a numbered SRT document (CRLF, UTF-8 BOM for players)."""
    parts: list[str] = []
    for i, cue in enumerate(normalize_cues(cues), start=1):
        parts.append(
            f"{i}\r\n{_fmt_time(cue.start_ms)} --> {_fmt_time(cue.end_ms)}\r\n"
            f"{cue.text}\r\n"
        )
    return "\r\n".join(parts)


def write_srt_file(path: str, cues: list[Cue]) -> None:
    """Write an SRT file players and media servers load automatically."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(write_srt(cues))