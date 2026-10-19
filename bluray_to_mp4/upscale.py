"""DVD AI-upscale stage for the v0.2.19 pipeline.

The stage prepares DVD frames to 720p geometry, runs Real-ESRGAN ncnn/Vulkan,
and writes a video-only mezzanine MKV. The final encode maps video from this
mezzanine and audio from the original MakeMKV rip.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .binaries import (
    resolve_ffmpeg,
    resolve_ffprobe,
    resolve_realesrgan_ncnn,
)
from .ffprobe import (
    FrameCountProbe,
    parse_frame_rate,
    probe_frame_count,
)
from .makemkv import EncodeCancelled

__all__ = [
    "UpscaleError",
    "UpscalePlan",
    "model_name_for_mode",
    "resolve_realesrgan_models",
    "verify_mezzanine_frames",
    "verify_mezzanine_clock",
    "run_dvd_upscale",
    "ProgressHeartbeat",
    "FINALIZE_STEPS",
    "FINALIZE_STEP_CONCAT",
    "FINALIZE_STEP_VERIFY_FRAMES",
    "FINALIZE_STEP_VERIFY_CLOCK",
]


class UpscaleError(RuntimeError):
    """The optional AI-upscale stage failed and should fail open."""


# v0.2.35 (design §2.1/§2.4): the finalize-phase step tokens. These are
# shared vocabulary: run_dvd_upscale/pipeline emit them through the
# progress detail dict, and monitor.classify_snapshot's FINALIZE_STEPS
# compares against the same strings (re-exported here so the two
# modules can never drift - a rename fails the P-A2/P-A4 pins visibly).
FINALIZE_STEP_CONCAT = "concatenating mezzanine"
FINALIZE_STEP_VERIFY_FRAMES = "verifying frame count (full decode)"
FINALIZE_STEP_VERIFY_CLOCK = "verifying audio clock"
FINALIZE_STEPS = frozenset({
    FINALIZE_STEP_CONCAT,
    FINALIZE_STEP_VERIFY_FRAMES,
    FINALIZE_STEP_VERIFY_CLOCK,
})

# v0.2.35 (design §2.2): finalize emissions clamp the bar at 0.999 -
# the mezzanine is not proven until the gates pass, and the CLI prints
# a newline at frac >= 1.0 (a periodic 1.0 ping would spam one line per
# tick through the whole decode gate).
_FINALIZE_FRAC_CLAMP = 0.999

# v0.2.35 (design §2.3): 15 s heartbeat interval - 12x inside the
# 300 s monitor stale window with scheduler-jitter headroom; fixed
# module constant, no config surface (design constraint).
_FINALIZE_HEARTBEAT_SECONDS = 15.0


class ProgressHeartbeat:
    """Pings on_progress every `interval` seconds with the last
    (frac, step, idx) until stop() (v0.2.35 design §2.3).

    Keeps the monitor's updated_at fresh through finalize-phase
    work that emits no progress of its own (ffprobe -count_frames
    emits nothing during a 20-60 min full decode). Daemon thread;
    every tick is wrapped so the thread never raises into the run.
    Ticks carry the same fields a real report does plus
    detail["heartbeat"] = True so consumers can tell a liveness ping
    from a state change; clamped frac + unchanged step leave
    progress_updated_at alone (progress genuinely did not move).
    """

    def __init__(self, on_progress, *, interval: float = _FINALIZE_HEARTBEAT_SECONDS):
        self._on_progress = on_progress
        self._interval = float(interval)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frac = _FINALIZE_FRAC_CLAMP
        self._step: str | None = None
        self._idx = 0
        self._chunks = 0

    def start(self, frac: float, step: str, idx: int, chunks: int) -> None:
        self._frac = min(_FINALIZE_FRAC_CLAMP, max(0.0, float(frac)))
        self._step = step
        self._idx = int(idx)
        self._chunks = int(chunks)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="b2m-finalize-heartbeat", daemon=True)
        self._thread.start()

    def _ping(self) -> None:
        on_progress = self._on_progress
        if on_progress is None:
            return
        try:
            on_progress(
                self._frac,
                None,
                {
                    "chunk": self._chunks,
                    "chunks": self._chunks,
                    "step": self._step,
                    "heartbeat": True,
                },
            )
        except Exception:
            # A bookkeeping ping must never kill the run (design §2.3:
            # "never raises"). The report path itself is the UI/monitor
            # update seam and is exception-safe upstream; this guard is
            # defense in depth for third-party consumers.
            pass

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._ping()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._thread = None


@dataclass(frozen=True)
class UpscalePlan:
    input_path: str
    output_path: str
    workdir: str
    mode: str
    duration_seconds: int
    deinterlace: bool = False
    chunk_seconds: int = 120
    fps: str = "30000/1001"
    binary: str = "realesrgan-ncnn-vulkan"
    models_path: str | None = None
    ffmpeg: str = "ffmpeg"
    tile_size: int = 0
    target_height: int = 720
    source_width: int = 0
    source_height: int = 0
    display_aspect_ratio: str = ""
    sample_aspect_ratio: str = ""
    # v0.2.33 frame-exact chunking (design §2.3): the authoritative
    # decode-counted total from probe_frame_count, required at run
    # time; chunk_frames is derived once via round(chunk_seconds*fps).
    total_frames: int | None = None
    chunk_frames: int | None = None
    frame_count_from_metadata: bool = False


def model_name_for_mode(mode: str) -> str:
    if mode == "720p":
        # v0.2.28: live-action profile now runs RealESRGAN_x2plus - a TRUE
        # native-2x RRDBNet (official general-purpose weights via the
        # official conversion chain, bundled as realesrgan-x2plus). This is
        # NOT the v0.2.25 x4plus-at--s 2 corruption case: x4plus is a native
        # 4x net and running it at -s 2 corrupts frames; x2plus IS a scale-2
        # net, so -s 2 is its native mode.
        return "realesrgan-x2plus"
    if mode == "720p_anime":
        return "realesr-animevideov3"
    raise UpscaleError(f"unknown DVD upscale mode: {mode}")


def realesrgan_scale_for_mode(mode: str) -> int:
    if mode == "720p":
        # realesrgan-x2plus is a true native-2x model: -s 2 is its native
        # mode (NOT the v0.2.25 x4plus-at--s 2 corruption, where a native
        # 4x net was misrun at 2x). A 640x360 square-pixel extraction lands
        # directly at 1280x720 with no 4x round trip and no downsample.
        return 2
    if mode == "720p_anime":
        # realesr-animevideov3 -s 2 is likewise native 2x for the anime
        # profile (unchanged since v0.2.26).
        return 2
    raise UpscaleError(f"unknown DVD upscale mode: {mode}")


def resolve_realesrgan_models(binary: str, models_path: str | None = None) -> str:
    if models_path:
        candidate = Path(models_path)
    else:
        candidate = Path(binary).resolve().parent / "models"
    if not candidate.is_dir():
        raise UpscaleError(
            "Real-ESRGAN models not found; expected a models folder next to "
            f"{binary} or set B2M_REALESRGAN_MODELS"
        )
    required = [
        "realesrgan-x4plus.param",
        "realesrgan-x4plus.bin",
        "realesrgan-x2plus.param",
        "realesrgan-x2plus.bin",
        "realesr-animevideov3-x2.param",
        "realesr-animevideov3-x2.bin",
    ]
    missing = [name for name in required if not (candidate / name).is_file()]
    if missing:
        raise UpscaleError(
            "Real-ESRGAN model folder is incomplete: " + ", ".join(missing)
        )
    return str(candidate)


def _cancelled(cancel) -> bool:
    return bool(cancel is not None and cancel())


def _run_process(cmd: list[str], *, label: str, cancel=None) -> None:
    if _cancelled(cancel):
        raise EncodeCancelled(f"cancelled before {label}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise UpscaleError(f"{label} could not start: {exc}") from exc

    output_tail: deque[str] = deque(maxlen=80)

    def drain(pipe) -> None:
        try:
            for line in pipe:
                output_tail.append(line.rstrip())
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    drain_threads: list[threading.Thread] = []
    for pipe in (proc.stdout, proc.stderr):
        if pipe is None:
            continue
        thread = threading.Thread(target=drain, args=(pipe,), daemon=True)
        thread.start()
        drain_threads.append(thread)

    while proc.poll() is None:
        if _cancelled(cancel):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            raise EncodeCancelled(f"cancelled during {label}")
        time.sleep(0.2)

    proc.wait()
    for thread in drain_threads:
        thread.join(timeout=1)
    if proc.returncode != 0:
        detail = [line for line in output_tail if line][-6:]
        raise UpscaleError(
            f"{label} failed with code {proc.returncode}: "
            + " | ".join(detail)[:600]
        )


def _even(value: float) -> int:
    rounded = max(2, int(round(value)))
    return rounded if rounded % 2 == 0 else rounded + 1


def _parse_ratio(value: str) -> float | None:
    if not value:
        return None
    if ":" in value:
        left, right = value.split(":", 1)
        try:
            den = float(right)
            if den == 0:
                return None
            return float(left) / den
        except ValueError:
            return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _source_aspect(plan: UpscalePlan) -> float | None:
    display = _parse_ratio(plan.display_aspect_ratio)
    if display:
        return display
    if plan.source_width > 0 and plan.source_height > 0:
        sar = _parse_ratio(plan.sample_aspect_ratio) or 1.0
        return (plan.source_width / plan.source_height) * sar
    return None


def _geometry_for_height(plan: UpscalePlan, height: int) -> tuple[int, int] | None:
    aspect = _source_aspect(plan)
    if not aspect:
        return None
    height = _even(height)
    return _even(height * aspect), height


def _fixed_size_filter(width: int, height: int) -> str:
    return f"scale={width}:{height}:flags=lanczos,setsar=1"


def _square_pixel_height_filter(height: int) -> str:
    height = _even(height or 720)
    # DVD storage pixels are often not the display shape. Use DAR to compute
    # the square-pixel width explicitly before clearing SAR to 1.
    return f"scale=w='trunc({height}*dar/2)*2':h={height}:flags=lanczos,setsar=1"


def _prepare_filter(deinterlace: bool) -> str:
    filters = []
    if deinterlace:
        filters.append("yadif")
    filters.append(_square_pixel_height_filter(360))
    return ",".join(filters)


def _prepare_filter_for_plan(plan: UpscalePlan) -> str:
    filters = []
    if plan.deinterlace:
        filters.append("yadif")
    geometry = _geometry_for_height(plan, 360)
    if geometry:
        filters.append(_fixed_size_filter(*geometry))
    else:
        filters.append(_square_pixel_height_filter(360))
    return ",".join(filters)


def _target_filter(target_height: int) -> str:
    return _square_pixel_height_filter(target_height)


def _target_filter_for_plan(plan: UpscalePlan) -> str:
    geometry = _geometry_for_height(plan, plan.target_height)
    if geometry:
        return _fixed_size_filter(*geometry)
    return _target_filter(plan.target_height)


def _count_pngs(directory: Path) -> int:
    return sum(1 for _ in directory.glob("*.png"))


def _debug_line(workroot: Path, text: str) -> None:
    try:
        with (workroot / "upscale_debug.log").open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


def _first_png_extent(directory: Path) -> int:
    for frame in sorted(directory.glob("*.png")):
        size = _png_size(frame)
        if size:
            return max(size)
    return 720


def _tile_size_arg(tile_size: int, frame_extent: int) -> str:
    frame_with_margin = int(frame_extent or 720) + 64
    if tile_size and tile_size > 0:
        requested = max(int(tile_size), frame_with_margin)
    else:
        # One tile, but only slightly larger than the actual frame handed to
        # Real-ESRGAN. DVD upscale extraction is normally 480x360 or 640x360.
        requested = max(512, frame_with_margin)
    rounded = int(math.ceil(requested / 64) * 64)
    return str(max(rounded, int(frame_extent or 0)))


def _write_concat_list(segments: list[Path]) -> str:
    fh = NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt")
    with fh:
        for segment in segments:
            path = str(segment).replace("'", "'\\''")
            fh.write(f"file '{path}'\n")
    return fh.name


# v0.2.35 §3.3: the resume manifest version + the informational field
# excluded from fingerprint comparison. Whole-file (sorted-key) JSON
# equality decides reuse - failing closed on ANY plan difference.
_RESUME_MANIFEST_VERSION = 1
_RESUME_MANIFEST_NAME = "resume_manifest.json"
_RESUME_MANIFEST_ATTEMPT_KEY = "attempt_started_at"


def _resume_manifest_payload(plan: UpscalePlan, *, binary: str, model: str,
                             esrgan_scale: int, segment_filter: str,
                             total_frames: int, chunk_frames: int) -> dict:
    """§3.3: the plan fingerprint a re-entered run must match exactly
    (sorted-key JSON equality; chunk_seconds is deliberately absent -
    chunk_frames + total_frames fully determine the bounds math, which
    is what must match). attempt_started_at is informational and
    excluded from the comparison."""
    return {
        "version": _RESUME_MANIFEST_VERSION,
        "input_path": os.path.abspath(plan.input_path),
        "mode": plan.mode,
        "binary_sha_basename": {"binary": binary, "model": model},
        "fps": plan.fps,
        "total_frames": int(total_frames),
        "chunk_frames": int(chunk_frames),
        "deinterlace": bool(plan.deinterlace),
        "target_height": int(plan.target_height),
        "segment_filter": segment_filter,
        "esrgan_scale": int(esrgan_scale),
        _RESUME_MANIFEST_ATTEMPT_KEY: datetime.now().isoformat(),
    }


def _write_resume_manifest(workroot: Path, payload: dict) -> None:
    try:
        with (workroot / _RESUME_MANIFEST_NAME).open(
                "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except OSError:
        pass  # unreadable workroot: resume is best-effort; the run proceeds


def _resume_manifest_matches(workroot: Path, payload: dict) -> bool:
    """True iff the on-disk manifest equals `payload` ignoring the
    informational attempt_started_at field (§3.3). Unreadable/corrupt/
    absent -> False (fail closed -> wholesale recompute)."""
    try:
        with (workroot / _RESUME_MANIFEST_NAME).open(encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(existing, dict):
        return False
    candidate = dict(payload)
    candidate.pop(_RESUME_MANIFEST_ATTEMPT_KEY, None)
    existing_cmp = dict(existing)
    existing_cmp.pop(_RESUME_MANIFEST_ATTEMPT_KEY, None)
    return existing_cmp == candidate


def _verify_segment(
    segment: Path,
    expected_count: int,
    plan_fps: str,
    *,
    ffmpeg_probe,
) -> bool:
    """§3.4: strict, cheap reuse check for one chunk segment.

    Four gates, all must pass: (1) decodability + exact decode count
    via ffprobe -count_frames (no metadata fast path - nb_frames is
    the exact lie class v0.2.33 retired for rips; uniform strict rule,
    bounded cost: FFV1 decodes >1000 fps, ~2-3 s per film chunk);
    (2) the count equals THIS chunk's expected count from bounds;
    (3) the segment's stamp rate equals the plan fps (canonical token
    comparison through parse_frame_rate on both sides - the count gate
    alone is blind to the v0.2.34 clock-lie class); (4) nonzero size
    (an interrupted mux can leave a plausible header on a sparse file).
    Any probe failure/missing field -> False (recompute, never crash).
    """
    try:
        if not segment.is_file() or segment.stat().st_size <= 0:
            return False
        probe_cmd = [
            ffmpeg_probe,
            "-v", "error",
            "-select_streams", "v:0",
            "-count_frames",
            "-show_entries", "stream=nb_read_frames,r_frame_rate",
            "-of", "json",
            str(segment),
        ]
        result = subprocess.run(
            probe_cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            return False
        data = json.loads(result.stdout or "{}")
        stream = data.get("streams", [{}])[0]
        read_frames = int(stream.get("nb_read_frames") or 0)
        if read_frames != int(expected_count):
            return False
        plan_token = parse_frame_rate(plan_fps)
        seg_token = parse_frame_rate(
            str(stream.get("r_frame_rate") or ""))
        return bool(plan_token and seg_token and plan_token == seg_token)
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return False


def verify_mezzanine_frames(
    mezzanine_path: str,
    rip_total_frames: int,
    *,
    probe_frames,
) -> None:
    """v0.2.33 gate: mezzanine frame count must equal the rip's exactly.

    Replaces the v0.2.27 duration gate (max(0.5 s, 0.01%) tolerance):
    under frame-exact chunking the mezzanine contains the rip's frames
    by construction, so any deviation is a nonzero integer difference -
    exact equality, no tolerance window. A mismatch raises UpscaleError
    naming both counts; the pipeline's fail-open path then encodes the
    ORIGINAL rip, retains the rejected mezzanine, and writes a
    diagnostic (design §4). rip_total_frames <= 0 (probe failed) must
    NOT pass either - an unknown count cannot certify a mezzanine.
    probe_frames is injected (a probe_frame_count-shaped callable) so
    the gate is testable without ffprobe.
    """
    if rip_total_frames is None or int(rip_total_frames) <= 0:
        raise UpscaleError(
            "rip frame count unknown; refusing to encode a possibly "
            "desynced upscale"
        )
    try:
        mezz_probe = probe_frames(mezzanine_path)
        # Accept either the FrameCountProbe object or a bare int (tests
        # and the design-doc signature sketch both pass shapes like
        # int(probe_frames(path))).
        mezz = (
            int(mezz_probe.total_frames)
            if isinstance(mezz_probe, FrameCountProbe)
            else int(mezz_probe)
        )
    except Exception as exc:
        raise UpscaleError(
            f"upscale mezzanine frame count could not be probed ({exc}); "
            "refusing to encode a possibly desynced upscale"
        ) from exc
    if mezz != int(rip_total_frames):
        raise UpscaleError(
            f"upscale mezzanine has {mezz} frames; the rip has "
            f"{int(rip_total_frames)}; refusing to encode a desynced upscale"
        )


def verify_mezzanine_clock(
    mezzanine_path: str,
    audio_duration_seconds: float,
    *,
    probe_duration,
    tolerance_fraction: float = 0.001,
    tolerance_floor_seconds: float = 2.0,
) -> None:
    """v0.2.34 clock gate: the mezzanine must PLAY as long as the audio.

    Composes with verify_mezzanine_frames on an orthogonal axis (design
    §3): the frames gate asks "did we rebuild every frame?" (count,
    exact); this gate asks "does the rebuilt mezzanine play for as long
    as the audio we ship?" (time, tolerance). It exists because the
    2026-10-18 Mamma Mia HWA probe falsified the assumption behind the
    v0.2.33 duration-gate deletion: correct count + wrong CLOCK =
    wrong duration (a 163,509-frame mezzanine at 29.97 plays 5,453 s
    against 6,818 s of audio), and verify_mezzanine_frames passes
    because the count is exact. It deliberately does NOT resurrect
    verify_mezzanine_duration: that gate probed the RIP against the
    title-scan integer (retired semantics); this gate probes the
    MEZZANINE against the selected audio stream's measured extent.

    Tolerance = max(tolerance_floor_seconds, tolerance_fraction x
    audio): single-digit seconds absorb the audio lead-in/out skew
    that survives even a correct stamp (mezzanine 6,819.7 s vs audio
    6,818.4 s on this disc - delta 1.3 s), while a rate-CLASS error
    (23.976 vs 29.97 stamps the same frames 25% apart = 1,366 s of
    delta) exceeds any tolerance here by two orders of magnitude.
    probe_duration is injected (a probe_duration_seconds-shaped
    callable) so the gate is testable without ffprobe; a probe failure
    raises UpscaleError so the same fail-open handler runs.
    """
    audio_seconds = float(audio_duration_seconds or 0.0)
    if audio_seconds <= 0:
        raise UpscaleError(
            "audio duration unknown; cannot verify the mezzanine clock"
        )
    tolerance = max(
        float(tolerance_floor_seconds),
        float(tolerance_fraction) * audio_seconds,
    )
    try:
        mezz_seconds = float(probe_duration(mezzanine_path))
    except Exception as exc:
        raise UpscaleError(
            f"upscale mezzanine duration could not be probed ({exc}); "
            "refusing to encode a possibly desynced upscale"
        ) from exc
    delta = abs(mezz_seconds - audio_seconds)
    if delta > tolerance:
        raise UpscaleError(
            f"upscale mezzanine plays for {mezz_seconds:.1f} s but the "
            f"audio runs {audio_seconds:.1f} s (delta {delta:.1f} s, "
            f"tolerance {tolerance:.1f} s); the stamped frame rate "
            "does not match the shipped audio clock - refusing to "
            "encode a desynced upscale"
        )


def _fps_float(fps: str) -> float:
    """Numeric fps from a plan token ("30000/1001" -> 29.970...).

    v0.2.33 §2.3: chunk_frames = round(chunk_seconds * fps) needs a
    float clock; the token's precision is preserved because the division
    happens once, not per frame.
    """
    text = (fps or "30000/1001").strip()
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            numerator = float(num.strip())
            denominator = float(den.strip())
        except ValueError:
            return 30000.0 / 1001.0
        if numerator <= 0 or denominator <= 0:
            return 30000.0 / 1001.0
        return numerator / denominator
    try:
        rate = float(text)
    except ValueError:
        return 30000.0 / 1001.0
    return rate if rate > 0 else 30000.0 / 1001.0


def compute_chunk_frames(chunk_seconds: int, fps: str) -> int:
    """Frames per full chunk: max(1, round(chunk_seconds * fps)) (§2.3).

    round() not floor(): a nominal "120 s" chunk at 23.976 is 2877
    frames, closer to the human label.
    """
    return max(1, round(max(10, int(chunk_seconds or 120)) * _fps_float(fps)))


def compute_chunk_bounds(total_frames: int, chunk_frames: int) -> list[tuple[int, int]]:
    """Per-chunk (first, count) frame bounds: [i*F, (i+1)*F) windows.

    The last chunk carries the remainder (§2.3). Integer-exact: bounds
    are frame indices, never durations.
    """
    total = int(total_frames)
    step = max(1, int(chunk_frames))
    bounds = []
    first = 0
    while first < total:
        count = min(step, total - first)
        bounds.append((first, count))
        first += count
    return bounds


def run_dvd_upscale(
    plan: UpscalePlan,
    *,
    on_progress=None,
    cancel=None,
) -> tuple[str, int]:
    """Run Real-ESRGAN and return (mezzanine_path, total_frames).

    v0.2.33: chunks are frame-index windows [i*F, (i+1)*F) extracted
    with ffmpeg's select filter - never -ss/-t time windows - and each
    chunk's PNG count is asserted against its expected count, so drift
    is impossible by construction and any decode/reassembly anomaly
    fails loudly instead of shipping a desynced mezzanine.

    This is an optional cosmetic stage. UpscaleError callers should fail open
    and encode the original DVD rip; EncodeCancelled still propagates.
    """
    binary = resolve_realesrgan_ncnn(plan.binary)
    if binary == plan.binary and not Path(binary).is_file():
        raise UpscaleError(
            "realesrgan-ncnn-vulkan was not found; set B2M_REALESRGAN_NCNN "
            "or install it beside the app tools"
        )
    models = resolve_realesrgan_models(
        binary,
        plan.models_path or os.environ.get("B2M_REALESRGAN_MODELS"),
    )
    ffmpeg = resolve_ffmpeg(plan.ffmpeg)
    model = model_name_for_mode(plan.mode)
    esrgan_scale = realesrgan_scale_for_mode(plan.mode)
    segment_filter = (
        f"scale=-2:{_even(plan.target_height)}:flags=lanczos,setsar=1"
        if esrgan_scale != 2
        else "setsar=1"
    )

    # v0.2.33 §2.3: chunk by FRAME INDEX, not time windows. total_frames
    # comes from the decode-counted pre-flight probe and is required;
    # chunk_frames = round(chunk_seconds * fps) keeps the operator's
    # ~2-minute mental model while boundaries stay indexed, not timed.
    if plan.total_frames is None or int(plan.total_frames) <= 0:
        raise UpscaleError(
            "rip frame count unknown; refusing to run a frame-exact "
            "upscale without it"
        )
    total_frames = int(plan.total_frames)
    chunk_seconds = max(10, int(plan.chunk_seconds or 120))
    F = (
        int(plan.chunk_frames)
        if plan.chunk_frames is not None and int(plan.chunk_frames) > 0
        else compute_chunk_frames(chunk_seconds, plan.fps)
    )
    bounds = compute_chunk_bounds(total_frames, F)
    chunks = len(bounds)
    workroot = Path(plan.workdir)
    workroot.mkdir(parents=True, exist_ok=True)
    output = Path(plan.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    # v0.2.35 §3.3/§3.5: the resume fingerprint. Written fresh every
    # attempt (after the pre-concat unlink above - concat is redone
    # every attempt, so resume semantics end at the SEGMENT layer);
    # compared against the workroot's existing manifest. A mismatch
    # (different plan intent, or corrupt/unreadable) means the old
    # chunks are garbage from another intent: wholesale rmtree, start
    # clean - failing closed, same philosophy as the v0.2.12
    # rip-reuse width tier.
    manifest_payload = _resume_manifest_payload(
        plan, binary=binary, model=model, esrgan_scale=esrgan_scale,
        segment_filter=segment_filter, total_frames=total_frames,
        chunk_frames=F,
    )
    resume_allowed = _resume_manifest_matches(workroot, manifest_payload)
    if not resume_allowed and any(workroot.iterdir()):
        shutil.rmtree(workroot, ignore_errors=True)
        workroot.mkdir(parents=True, exist_ok=True)
        _debug_line(workroot, "resume: fingerprint=mismatch -> workroot cleared")
    _write_resume_manifest(workroot, manifest_payload)
    resumed_count = 0

    def report(frac: float, step: str, idx: int) -> None:
        if on_progress is not None:
            on_progress(
                min(1.0, max(0.0, frac)),
                None,
                {"chunk": idx + 1, "chunks": chunks, "step": step},
            )

    def report_finalize(step: str) -> None:
        """v0.2.35 §2.1/§2.2: finalize-phase emission - the bar clamps
        at 0.999 (the mezzanine is not yet proven) and the chunk line
        reads the LAST chunk so the UI keeps its `chunk N of N` shape
        while the step names the finalize work."""
        if on_progress is not None:
            on_progress(
                _FINALIZE_FRAC_CLAMP,
                None,
                {"chunk": chunks, "chunks": chunks, "step": step},
            )

    segments: list[Path] = []
    try:
        for idx in range(chunks):
            first, count = bounds[idx]
            chunk = workroot / f"chunk_{idx:04d}"
            in_dir = chunk / "in"
            out_dir = chunk / "out"
            segment = chunk / "segment.mkv"
            # v0.2.35 §3.4: the resume decision. Only a complete,
            # VERIFIED segment.mkv earns reuse (§3.2: PNG dirs are
            # never resume units - a partial out/ is indistinguishable
            # from a complete one without deep inspection, and a
            # segment whose truth was proven at verify time skips the
            # PNG assertions by construction). in/out rmtree becomes
            # CONDITIONAL: a resumed chunk never creates them.
            if (
                resume_allowed
                and segment.is_file()
                and _verify_segment(segment, count, plan.fps,
                                    ffmpeg_probe=resolve_ffprobe("ffprobe"))
            ):
                segments.append(segment)
                resumed_count += 1
                report(len(segments) / chunks, "resumed chunk", idx)
                continue
            shutil.rmtree(in_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)
            in_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            try:
                segment.unlink()
            except FileNotFoundError:
                pass

            report(len(segments) / chunks, "extracting frames", idx)
            # v0.2.33 §2.4: select=between(n,a,b) addresses decoded frame
            # index n (0-based) - the frame-exact anchor, independent of
            # timestamps. No -ss: the design's simpler first cut (full
            # decode; Real-ESRGAN dominates by an order of magnitude).
            # -vsync 0 (passthrough) is mandatory so ffmpeg does not
            # duplicate/drop frames to hit an implied CFR output clock.
            _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-i",
                    plan.input_path,
                    "-an",
                    "-sn",
                    "-vf",
                    (
                        f"select='between(n,{first},{first + count - 1})',"
                        + _prepare_filter(plan.deinterlace)
                    ),
                    "-vsync",
                    "0",
                    "-start_number",
                    str(first + 1),
                    str(in_dir / "frame_%08d.png"),
                ],
                label=f"upscale frame extraction chunk {idx + 1}/{chunks}",
                cancel=cancel,
            )
            # Per-chunk frame-count assertion (§2.4): the first real run
            # self-validates the select-index behavior for free.
            actual_in = _count_pngs(in_dir)
            if actual_in != count:
                raise UpscaleError(
                    f"chunk {idx + 1}/{chunks} extracted {actual_in} "
                    f"frames; expected {count} "
                    f"(frames {first}..{first + count - 1})"
                )
            if not any(in_dir.iterdir()):
                raise UpscaleError(f"no frames extracted for chunk {idx + 1}")

            input_frames = _count_pngs(in_dir)
            input_size = _png_size(in_dir / "frame_00000001.png")
            # Auto tiling (-t 0) lets the Vulkan driver split frames into a
            # tile grid, which reintroduced the v0.2.23 seam-line artifact in
            # v0.2.25. Force a single tile from the measured frame extent
            # plus a 64px safety margin (explicit tile_size honored as a
            # floor).
            frame_extent = max(input_size) if input_size else 0
            tile_size = _tile_size_arg(plan.tile_size, frame_extent)
            esrgan_cmd = [
                binary,
                "-i",
                str(in_dir),
                "-o",
                str(out_dir),
                "-s",
                str(esrgan_scale),
                "-t",
                tile_size,
                "-m",
                models,
                "-n",
                model,
                "-f",
                "png",
            ]
            _debug_line(
                workroot,
                (
                    f"chunk {idx + 1}/{chunks}: input_frames={input_frames} "
                    f"input_first_png={input_size or 'unknown'} "
                    f"model={model} scale={esrgan_scale} tile_size={tile_size} "
                    f"command={shlex.join(esrgan_cmd)}"
                ),
            )
            report(len(segments) / chunks, "AI upscaling", idx)
            _run_process(
                esrgan_cmd,
                label=f"Real-ESRGAN chunk {idx + 1}/{chunks}",
                cancel=cancel,
            )
            output_frames = _count_pngs(out_dir)
            output_size = _png_size(out_dir / "frame_00000001.png")
            _debug_line(
                workroot,
                (
                    f"chunk {idx + 1}/{chunks}: realesrgan_exit=0 "
                    f"output_frames={output_frames} "
                    f"output_first_png={output_size or 'unknown'}"
                ),
            )
            if not output_frames:
                raise UpscaleError(f"no upscaled frames produced for chunk {idx + 1}")
            # Per-chunk assertion after Real-ESRGAN too (§2.4): the engine
            # must return exactly the frames it was handed.
            if output_frames != count:
                raise UpscaleError(
                    f"chunk {idx + 1}/{chunks} upscaled {output_frames} "
                    f"frames; expected {count}"
                )

            report(len(segments) / chunks, "writing mezzanine segment", idx)
            _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-framerate",
                    plan.fps,
                    # v0.2.33: PNGs carry the GLOBAL frame numbering
                    # (extraction wrote -start_number first+1), so the
                    # image2 reader must be told where this chunk's
                    # numbering starts - the default of 1 only matches
                    # chunk 0.
                    "-start_number",
                    str(first + 1),
                    "-i",
                    str(out_dir / "frame_%08d.png"),
                    "-an",
                    "-vf",
                    segment_filter,
                    "-c:v",
                    "ffv1",
                    "-level",
                    "3",
                    str(segment),
                ],
                label=f"upscale mezzanine segment {idx + 1}/{chunks}",
                cancel=cancel,
            )
            segments.append(segment)
            shutil.rmtree(in_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)
            report(len(segments) / chunks, "chunk complete", idx)

        # v0.2.35 §3.7: one debug line per attempt summarizing the
        # split - the log/debug lane for "did resume actually happen".
        _debug_line(
            workroot,
            f"resume: reused={resumed_count} "
            f"recompute={chunks - resumed_count} "
            + ("fingerprint=match" if resumed_count else "fingerprint="
               + ("match" if resume_allowed else "fresh")),
        )
        report_finalize(FINALIZE_STEP_CONCAT)
        heartbeat = ProgressHeartbeat(
            on_progress, interval=_FINALIZE_HEARTBEAT_SECONDS)
        heartbeat.start(_FINALIZE_FRAC_CLAMP, FINALIZE_STEP_CONCAT, chunks - 1, chunks)
        try:
            if len(segments) == 1:
                shutil.move(str(segments[0]), output)
            else:
                concat = _write_concat_list(segments)
                try:
                    _run_process(
                        [
                            ffmpeg,
                            "-y",
                            "-hide_banner",
                            "-nostdin",
                            "-f",
                            "concat",
                            "-safe",
                            "0",
                            "-i",
                            concat,
                            "-c",
                            "copy",
                            str(output),
                        ],
                        label="upscale mezzanine concat",
                        cancel=cancel,
                    )
                finally:
                    try:
                        os.remove(concat)
                    except OSError:
                        pass
            if not output.is_file() or output.stat().st_size == 0:
                raise UpscaleError("upscale mezzanine was not created")
            shutil.rmtree(workroot, ignore_errors=True)
        finally:
            heartbeat.stop()
        return (str(output), total_frames)
    except EncodeCancelled:
        raise
    except Exception as exc:
        try:
            if output.exists():
                output.unlink()
        except OSError:
            pass
        if isinstance(exc, UpscaleError):
            raise
        raise UpscaleError(str(exc)) from exc
