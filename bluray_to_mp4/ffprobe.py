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
    "first_video_stream",
    "parse_frame_rate",
    "video_frame_rate",
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
    height: int = 0
    sample_aspect_ratio: str = ""
    display_aspect_ratio: str = ""
    r_frame_rate: str = ""    # v0.2.27: ffprobe '24000/1001' style ("" absent)
    avg_frame_rate: str = ""  # v0.2.27: fallback when r_frame_rate is unusable


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
                height=int(s.get("height") or 0),
                sample_aspect_ratio=s.get("sample_aspect_ratio", ""),
                display_aspect_ratio=s.get("display_aspect_ratio", ""),
                r_frame_rate=s.get("r_frame_rate", ""),
                avg_frame_rate=s.get("avg_frame_rate", ""),
            )
        )
    return streams


def parse_frame_rate(value: str) -> str | None:
    """Normalize a frame-rate string to an ffmpeg -framerate token.

    Accepts both producers' shapes: ffprobe's ratio form
    ('24000/1001', '25/1') and plain numbers ('23.976', '25', '29.97').
    Returns the trimmed original text (ffmpeg parses both forms
    natively; reformatting through float would drift precision), or
    None for absent/unusable values ('', '0/0', 'N/A', zero or
    negative rates, garbage) - the caller then falls back to the
    29.97 default with an honest warning (v0.2.27).
    """
    text = (value or "").strip()
    if not text:
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            numerator = float(num.strip())
            denominator = float(den.strip())
        except ValueError:
            return None
        if numerator <= 0 or denominator <= 0:
            return None
        return text
    try:
        rate = float(text)
    except ValueError:
        return None
    return text if rate > 0 else None


def video_frame_rate(streams: list[ProbedStream]) -> str | None:
    """First video stream's real frame rate (v0.2.27 desync fix).

    r_frame_rate first, avg_frame_rate fallback, both normalized for
    ffmpeg -framerate; None when no video stream or nothing parsable.
    """
    video = first_video_stream(streams)
    if video is None:
        return None
    return parse_frame_rate(video.r_frame_rate) or parse_frame_rate(
        video.avg_frame_rate
    )


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


def first_video_stream(streams: list[ProbedStream]) -> ProbedStream | None:
    """First video stream, or None when absent."""
    for s in streams:
        if s.codec_type == "video":
            return s
    return None


def first_video_width(streams: list[ProbedStream]) -> int | None:
    """Width of the first video stream, or None when absent/unparsed.

    Used by the rip-reuse verification (v0.2.15 design Gap 2): an
    existing leftover .mkv must be the same master the current disc's
    scan describes, and width is the discriminator.
    """
    video = first_video_stream(streams)
    return video.width if video and video.width else None