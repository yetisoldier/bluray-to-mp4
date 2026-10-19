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
    "AudioDurationProbe",
    "probe_audio_duration_seconds",
    "CANONICAL_RATES",
    "SNAP_TOLERANCE_FRACTION",
    "snap_to_canonical_rate",
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


# ---------------------------------------------------------------------
# v0.2.34 derived stamp rate (design §1.1/§1.2): the mezzanine's
# -framerate stamp comes from decode truth (decode-counted frames /
# the shipped audio stream's measured duration), snapped to a canonical
# rate. The container's r_frame_rate/avg_frame_rate fields - both of
# them, even when they agree - are demoted to diagnostic content: the
# 2026-10-18 Mamma Mia HWA redo probe caught both fields lying
# uniformly (30000/1001 on a soft-telecined 24000/1001 film).

# Minimum duration the derivation will divide by (design §1.1): a DVD
# title shorter than this is a probe artifact, not a movie - and the
# floor keeps the division safe from pathological rates.
AUDIO_DURATION_MIN_SECONDS = 10.0

# Maximum relative disagreement between the selected stream extent and
# the container format duration when both exist (design §1.1): legit
# audio/video extent skew on DVD rips is seconds (lead-in/out), never a
# quarter of the runtime, so 25% separates "normal skew" from "broken
# probe" with an order of magnitude of headroom.
AUDIO_DURATION_MAX_FORMAT_SKEW = 0.25


@dataclass
class AudioDurationProbe:
    """Result of the v0.2.34 audio-duration probe (design §1.1).

    seconds: the measured extent the stamp rate is derived against -
    the SELECTED audio stream's duration when one exists, the
    container format duration otherwise. from_format is True when the
    stream extent was absent/unparsable and the format figure stood in
    (the diagnostic records the fallback so the operator can distrust
    it). stream_ordinal is the audio ordinal that was probed (None when
    the format fallback ran without an audio selection).

    v0.2.35 C.3: format_seconds carries the container FORMAT duration
    the 25% sanity already resolved - an independent provenance from
    the stream extent, promoted to the snap-time cross-check. None
    when the format probe failed/absent or when seconds IS the format
    fallback (the guard needs a SECOND opinion, not the same figure).
    """

    seconds: float
    from_format: bool = False
    stream_ordinal: int | None = None
    format_seconds: float | None = None


def probe_audio_duration_seconds(
    path: str,
    audio_index: int | None,
    *,
    binary: str = "ffprobe",
) -> AudioDurationProbe:
    """Measured duration of the audio the encode will ship (§1.1).

    The final encode maps audio from the original rip
    (`-map {audio_input}:a:{N}?`), so the derivation's denominator is
    the duration of the SELECTED audio stream (the exact ordinal the
    encode maps from), probed via

        ffprobe -select_streams a:{N} -show_entries stream=duration

    When the rip has no audio stream at all, or the stream duration is
    absent/unparsable, the container format duration stands in
    (from_format=True; the diagnostic records the fallback). Sanity
    minimums refuse before any GPU work on: no usable figure at all;
    duration <= 0 or < AUDIO_DURATION_MIN_SECONDS; and a > 25%
    disagreement between the stream extent and the format duration
    when both exist (a broken stream extent must not silently set the
    stamp). Every refusal raises FfprobeError; the caller converts it
    into the pre-flight UpscaleError fail-open (design §1.3).
    """
    binary = resolve_ffprobe(binary)

    def _parse_stream_duration(text: str) -> float | None:
        # csv=p=0 emits one line per selected stream; the first
        # selected audio stream's duration is the figure we want.
        for line in (text or "").splitlines():
            token = line.strip()
            if not token:
                continue
            try:
                value = float(token)
            except ValueError:
                return None
            return value
        return None

    ordinal = 0 if audio_index is None else int(audio_index)
    result = subprocess.run(
        [binary, "-v", "error", "-select_streams", f"a:{ordinal}",
         "-show_entries", "stream=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stream_seconds = None
    if result.returncode == 0:
        stream_seconds = _parse_stream_duration(result.stdout)
        if stream_seconds is not None and stream_seconds <= 0:
            stream_seconds = None

    # The container format duration is needed twice: as the fallback
    # denominator when no stream extent exists, and as the sanity
    # baseline when one does (design §1.1: > 25% skew between the two
    # figures means the stream extent is broken - e.g. DTS core vs
    # HD-MA mis-flagging - and must not silently set the stamp).
    format_seconds: float | None = None
    try:
        format_seconds = probe_duration_seconds(path, binary=binary)
        if format_seconds <= 0:
            format_seconds = None
    except FfprobeError:
        format_seconds = None

    if stream_seconds is not None:
        if format_seconds is not None and abs(
            stream_seconds - format_seconds
        ) / format_seconds > AUDIO_DURATION_MAX_FORMAT_SKEW:
            raise FfprobeError(
                f"selected audio stream duration {stream_seconds:.1f} s "
                f"disagrees with the container duration "
                f"{format_seconds:.1f} s by more than "
                f"{AUDIO_DURATION_MAX_FORMAT_SKEW:.0%} on {path}; "
                "refusing to derive the stamp rate from a broken extent"
            )
        chosen = AudioDurationProbe(
            stream_seconds, from_format=False, stream_ordinal=ordinal,
            format_seconds=format_seconds)
    else:
        # Format-duration fallback: no audio stream, or the stream
        # duration field absent/unparsable (design §1.1 step 2).
        if format_seconds is None:
            raise FfprobeError(
                f"no usable audio or format duration for {path}; cannot "
                "derive the stamp rate"
            )
        chosen = AudioDurationProbe(
            format_seconds, from_format=True, stream_ordinal=None)

    if chosen.seconds <= 0 or chosen.seconds < AUDIO_DURATION_MIN_SECONDS:
        raise FfprobeError(
            f"audio duration {chosen.seconds} s is below the "
            f"{AUDIO_DURATION_MIN_SECONDS:g} s minimum for {path}; "
            "refusing to derive the stamp rate from it"
        )
    return chosen


# Canonical stamp rates (design §1.2). Tokens are exactly what gets
# handed to ffmpeg -framerate. 50 and 60000/1001 belong in the set:
# real DVDs carry 50i/59.94i video sources (sports/concert/documentary
# discs) that decode-count at those rates; excluding them would refuse
# legitimately-upscalable discs. 48/72/90/120 stay out: vanishingly rare
# on DVD and each entry is a false-snap target for a noisy measurement.
CANONICAL_RATES: tuple[tuple[str, float], ...] = (
    ("24000/1001", 24000.0 / 1001.0),
    ("24", 24.0),
    ("25", 25.0),
    ("30000/1001", 30000.0 / 1001.0),
    ("50", 50.0),
    ("60000/1001", 60000.0 / 1001.0),
)

# Relative snap tolerance (design §1.2): the same 0.1% constant the
# .33 CFR verdict uses for "is this clock difference real or
# rounding". Raw decode-truth rates are noisy at the fourth decimal
# (audio lead-in/out padding), so snapping converts measurement noise
# into the exactly-correct clock; anything outside the window refuses.
SNAP_TOLERANCE_FRACTION = 0.001


def snap_to_canonical_rate(derived: float) -> tuple[str, bool]:
    """Snap a derived rate to a canonical -framerate token (§1.2).

    Nearest-neighbor match within SNAP_TOLERANCE_FRACTION relative
    (|derived - canonical| / canonical <= 0.001). Returns (token,
    snapped); snapped=True records that the measurement was adjusted
    so the diagnostic can show raw and snapped side by side. Raises
    FfprobeError when nothing snaps - the caller (pre-flight) converts
    that into the refuse-before-GPU UpscaleError naming the derived
    rate and the nearest candidate (design §1.3).
    """
    value = float(derived)
    if value <= 0:
        raise FfprobeError(
            f"derived stamp rate {derived!r} is not a positive rate"
        )
    best_token = None
    best_value = None
    best_rel = None
    for token, canonical in CANONICAL_RATES:
        rel = abs(value - canonical) / canonical
        if best_rel is None or rel < best_rel:
            best_token, best_value, best_rel = token, canonical, rel
    assert best_token is not None and best_value is not None
    # Float-noise guard: a value MATHEMATICALLY exactly at the 0.1%
    # boundary (e.g. canonical * 0.999 computed in doubles) can land a
    # few ulps above the stored tolerance and refuse. The inclusive
    # edge (§1.2, F3 "±0.1% inclusive") absorbs that with a 1e-9
    # relative guard - six orders of magnitude tighter than any real
    # snap/refusal signal, so it changes no honest verdict.
    if best_rel <= SNAP_TOLERANCE_FRACTION * (1.0 + 1e-9):
        snapped = abs(value - best_value) > 0.0
        return (best_token, snapped)
    raise FfprobeError(
        f"derived stamp rate {value:.4f} fps does not match any "
        f"canonical rate within {SNAP_TOLERANCE_FRACTION:.1%} "
        f"(nearest: {best_token} at {best_rel:.2%}); refusing to guess "
        "the mezzanine clock"
    )