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
    "probe_frame_count",
    "FrameCountProbe",
    "probe_cfr_check",
    "CfrVerdict",
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
    r_frame_rate: str = ""    # nominal/rational clock; avg fallback
    avg_frame_rate: str = ""  # actual average clock, preferred when usable


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

    Prefer avg_frame_rate because it describes the stream's actual frame
    clock. ffprobe's r_frame_rate is a nominal/rational rate and can be
    slightly faster than the timestamps on telecined or irregular DVD
    sources; rebuilding PNG frames at that rate causes cumulative A/V
    drift. Fall back to r_frame_rate when no usable average is reported.
    None means no video stream or no parsable rate.
    """
    video = first_video_stream(streams)
    if video is None:
        return None
    return parse_frame_rate(video.avg_frame_rate) or parse_frame_rate(
        video.r_frame_rate
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


# ---------------------------------------------------------------------
# v0.2.33 frame-exact chunking: the authoritative frame count probe and
# the CFR pre-flight (design §2.1/§2.2). Both run once, before any GPU
# work is committed, so a bad rip costs ~30 s of probing instead of
# hours of Real-ESRGAN on a doomed run.


class FrameCountProbe:
    """Result of the two-pass frame-count probe (v0.2.33 §2.1).

    total_frames: the authoritative decoded-frame count of the first
    video stream. from_metadata is True only when the slow decode-count
    pass failed and the container's nb_frames metadata had to stand in
    (a known lie source on VOB-sourced rips - the diagnostic records
    it so the operator can distrust the number).
    """

    def __init__(self, total_frames: int, *, from_metadata: bool = False):
        self.total_frames = int(total_frames)
        self.from_metadata = bool(from_metadata)


def _run_ffprobe_json(binary: str, args: list[str], path: str) -> dict:
    result = subprocess.run(
        [binary, "-v", "error"] + args + ["-of", "json", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise FfprobeError(
            f"ffprobe failed on {path}: {result.stderr.strip()[:300]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FfprobeError(f"unparsable ffprobe output: {exc}") from exc


def probe_frame_count(path: str, *, binary: str = "ffprobe") -> FrameCountProbe:
    """Two-pass authoritative frame count of the first video stream.

    v0.2.33 design §2.1. Pass 1 reads nb_frames/nb_read_frames metadata
    only (fast, unreliable on VOB-sourced DVD rips). Pass 2 runs
    ffprobe -count_frames, which decodes every packet and counts the
    frames the stream actually yields - the only figure that cannot
    be wrong about how many decoded frames the demuxer/decoder emit,
    which is exactly what the frame-exact extraction must reproduce.

    Selection rule: slow-pass nb_read_frames > 0 is authoritative;
    otherwise fast-pass nb_frames > 0 stands in with from_metadata=True;
    otherwise FfprobeError (the caller refuses before any chunk runs).
    """
    binary = resolve_ffprobe(binary)

    def _first_stream_int(data: dict, field: str) -> int:
        try:
            value = int(data["streams"][0].get(field) or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            return 0
        return value

    # Fast pass: metadata only, no decode.
    fast = _run_ffprobe_json(
        binary,
        ["-select_streams", "v:0", "-show_entries", "stream=nb_frames,nb_read_frames"],
        path,
    )
    fast_nb_frames = _first_stream_int(fast, "nb_frames")

    # Slow pass: decode-count every packet (~20-60 s on a DVD rip).
    slow = _run_ffprobe_json(
        binary,
        [
            "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames",
        ],
        path,
    )
    slow_nb_read_frames = _first_stream_int(slow, "nb_read_frames")

    if slow_nb_read_frames > 0:
        return FrameCountProbe(slow_nb_read_frames)
    if fast_nb_frames > 0:
        return FrameCountProbe(fast_nb_frames, from_metadata=True)
    raise FfprobeError(
        f"rip frame count could not be probed for {path}"
    )


@dataclass
class CfrVerdict:
    """Outcome of the VFR pre-flight (v0.2.33 §2.2).

    verdict: "CFR" | "VFR" | "UNKNOWN". r_frame_rate/avg_frame_rate
    carry the raw probe tokens ("24000/1001" or "" when unreported) so
    the diagnostic can record exactly what the clock probe saw.
    """

    verdict: str
    r_frame_rate: str = ""
    avg_frame_rate: str = ""


def _rate_value(value: str) -> float | None:
    """Numeric rate of an ffprobe token, or None when unusable.

    parse_frame_rate normalizes/rejects but returns the original text;
    this converts the SAME validated shapes to a float for the |r-a|/a
    comparison ("24000/1001" -> 23.976..., "25" -> 25.0).
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
        return numerator / denominator
    try:
        rate = float(text)
    except ValueError:
        return None
    return rate if rate > 0 else None


def probe_cfr_check(path: str, *, binary: str = "ffprobe") -> CfrVerdict:
    """Classify the rip's frame clock as CFR, VFR, or UNKNOWN.

    v0.2.33 design §2.2. Piggybacks on the -count_frames decode pass
    (which already decodes everything) to read r_frame_rate and
    avg_frame_rate in the same subprocess. Verdicts:

    - UNKNOWN: either rate missing/unparsable -> the caller refuses the
      upscale; a rip whose clock cannot even be named must not be
      rebuilt on a guessed constant clock.
    - VFR: |r - a| / a > 0.001 (0.1%) -> the caller refuses pre-flight;
      reassembling a variable-clock source at constant fps re-creates
      drift by construction.
    - CFR: proceed (the overwhelmingly common MakeMKV case).
    """
    binary = resolve_ffprobe(binary)
    data = _run_ffprobe_json(
        binary,
        [
            "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate,nb_read_frames",
        ],
        path,
    )
    try:
        stream = data["streams"][0]
    except (KeyError, IndexError) as exc:
        raise FfprobeError(f"no video stream reported for {path}") from exc
    r_raw = str(stream.get("r_frame_rate") or "").strip()
    a_raw = str(stream.get("avg_frame_rate") or "").strip()
    # parse_frame_rate is the existing validator (design §2.2): reuse it
    # so the pre-flight accepts exactly the shapes the fps probe does.
    r_token = parse_frame_rate(r_raw)
    a_token = parse_frame_rate(a_raw)
    r_value = _rate_value(r_token) if r_token else None
    a_value = _rate_value(a_token) if a_token else None
    if r_value is None or a_value is None:
        return CfrVerdict("UNKNOWN", r_raw, a_raw)
    if abs(r_value - a_value) / a_value > 0.001:
        return CfrVerdict("VFR", r_raw, a_raw)
    return CfrVerdict("CFR", r_raw, a_raw)