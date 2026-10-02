"""ffprobe wrapper: real stream layout of a ripped MKV.

MakeMKV preserves stream order within each type (video, audio, subtitle),
so the recommended Nth audio/subtitle stream of the scan maps to the Nth
stream of that type in the ripped MKV. ffprobe gives us the actual stream
indices (for -map and subtitle extraction) plus field_order (interlace
detection).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .binaries import resolve_ffprobe

__all__ = [
    "ProbedStream",
    "parse_probe",
    "probe_streams",
    "probe_duration_seconds",
    "nth_of_type",
    "is_interlaced",
    "first_video_width",
]

INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}


class FfprobeError(RuntimeError):
    """ffprobe failed or produced unusable output."""


@dataclass
class ProbedStream:
    index: int  # absolute stream index in the container
    codec_type: str  # 'video' | 'audio' | 'subtitle'
    codec_name: str
    channels: int = 0
    lang: str = ""
    field_order: str = ""
    width: int = 0  # v0.2.15: video width in pixels (0 = not reported)


def parse_probe(text: str) -> list[ProbedStream]:
    """Parse ffprobe -show_streams JSON into ProbedStream records."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FfprobeError(f"unparsable ffprobe output: {exc}") from exc
    streams: list[ProbedStream] = []
    for s in data.get("streams", []):
        streams.append(
            ProbedStream(
                index=int(s.get("index", 0)),
                codec_type=s.get("codec_type", ""),
                codec_name=s.get("codec_name", ""),
                channels=int(s.get("channels") or 0),
                lang=(s.get("tags") or {}).get("language", ""),
                field_order=s.get("field_order", ""),
                width=int(s.get("width") or 0),
            )
        )
    return streams


def probe_streams(path: str, *, binary: str = "ffprobe") -> list[ProbedStream]:
    """Run ffprobe on a media file and return its streams."""
    binary = resolve_ffprobe(binary)
    result = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-of", "json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise FfprobeError(
            f"ffprobe failed on {path}: {result.stderr.strip()[:300]}"
        )
    return parse_probe(result.stdout)


def probe_duration_seconds(path: str, *, binary: str = "ffprobe") -> float:
    """Container duration of a media file in seconds (single csv value).

    Used to verify that an existing rip in the workdir is complete
    before reusing it (v0.2.12): a partial file either fails to probe
    or reports a duration far shorter than the scanned title.
    """
    binary = resolve_ffprobe(binary)
    result = subprocess.run(
        [binary, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise FfprobeError(
            f"ffprobe failed on {path}: {result.stderr.strip()[:300]}"
        )
    text = result.stdout.strip()
    if not text:
        raise FfprobeError(f"ffprobe reported no duration for {path}")
    try:
        return float(text)
    except ValueError as exc:
        raise FfprobeError(
            f"unparsable ffprobe duration for {path}: {text[:100]!r}"
        ) from exc


def nth_of_type(streams: list[ProbedStream], codec_type: str, n: int) -> ProbedStream | None:
    """The Nth (0-based) stream of a given type."""
    seen = 0
    for s in streams:
        if s.codec_type == codec_type:
            if seen == n:
                return s
            seen += 1
    return None


def is_interlaced(streams: list[ProbedStream]) -> bool:
    """True when the video stream reports an interlaced field order."""
    for s in streams:
        if s.codec_type == "video":
            return s.field_order.lower() in INTERLACED_FIELD_ORDERS
    return False


def first_video_width(streams: list[ProbedStream]) -> int | None:
    """Width of the first video stream, or None when absent/unparsed.

    Used by the rip-reuse verification (v0.2.15 design Gap 2): an
    existing leftover .mkv must be the same master the current disc's
    scan describes, and width is the discriminator.
    """
    for s in streams:
        if s.codec_type == "video":
            return s.width or None
    return None