"""DVD AI-upscale stage for the v0.2.19 pipeline.

The stage prepares DVD frames to 720p geometry, runs Real-ESRGAN ncnn/Vulkan,
and writes a video-only mezzanine MKV. The final encode maps video from this
mezzanine and audio from the original MakeMKV rip.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from .binaries import resolve_ffmpeg, resolve_realesrgan_ncnn
from .makemkv import EncodeCancelled

__all__ = [
    "UpscaleError",
    "UpscalePlan",
    "model_name_for_mode",
    "resolve_realesrgan_models",
    "verify_mezzanine_duration",
    "run_dvd_upscale",
]


class UpscaleError(RuntimeError):
    """The optional AI-upscale stage failed and should fail open."""


# A mezzanine whose duration drifts from the rip must never reach the
# encode. The old max(5 s, 2%) gate caught a gross 23.976/29.97 mismatch
# but allowed Mamma Mia's 15.050-second feature-length drift (only 0.22%).
# Container rounding is frame-scale, so permit 0.5 s plus a very small
# proportional allowance while rejecting any viewer-visible accumulation.
_MEZZANINE_DURATION_TOLERANCE_SECONDS = 0.5
_MEZZANINE_DURATION_TOLERANCE_FRACTION = 0.0001


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


def verify_mezzanine_duration(
    mezzanine_path: str,
    rip_duration_seconds: float,
    *,
    probe_duration,
) -> None:
    """Sanity gate: mezzanine duration must match the rip (v0.2.27).

    Raises UpscaleError naming both durations when they differ by more
    than max(0.5 seconds, 0.01%) - the pipeline's existing fail-open path
    then encodes the ORIGINAL rip with an honest warning instead of a
    silently desynced upscale. An unknown rip duration (probe failed
    and no title duration - effectively <= 0) must NOT pass the gate
    either (v0.2.29, Karen's validation): with rip=0 the tolerance
    floor would accept any mezzanine, so the gate refuses it exactly
    like an unprobeable mezzanine. probe_duration is injected (a
    probe_duration_seconds-shaped callable) so the gate is testable
    without ffprobe.
    """
    try:
        mezz = float(probe_duration(mezzanine_path))
    except Exception as exc:  # unprobeable mezzanine cannot be trusted
        raise UpscaleError(
            f"upscale mezzanine duration could not be probed "
            f"({exc}); refusing to encode a possibly desynced upscale"
        ) from exc
    rip = float(rip_duration_seconds or 0)
    if rip <= 0:
        raise UpscaleError(
            "rip duration unknown (the rip could not be probed and no "
            "title duration is available); refusing to encode a "
            "possibly desynced upscale"
        )
    tolerance = max(
        _MEZZANINE_DURATION_TOLERANCE_SECONDS,
        _MEZZANINE_DURATION_TOLERANCE_FRACTION * rip,
    )
    if abs(mezz - rip) > tolerance:
        raise UpscaleError(
            f"upscale mezzanine duration {mezz:.1f}s does not match the "
            f"rip's {rip:.1f}s (off by {abs(mezz - rip):.1f}s); refusing "
            "to encode a desynced upscale"
        )


def run_dvd_upscale(
    plan: UpscalePlan,
    *,
    on_progress=None,
    cancel=None,
) -> str:
    """Run Real-ESRGAN and return the completed mezzanine path.

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

    total_seconds = max(1, int(plan.duration_seconds or 1))
    chunk_seconds = max(10, int(plan.chunk_seconds or 120))
    chunks = math.ceil(total_seconds / chunk_seconds)
    workroot = Path(plan.workdir)
    workroot.mkdir(parents=True, exist_ok=True)
    output = Path(plan.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    def report(frac: float, step: str, idx: int) -> None:
        if on_progress is not None:
            on_progress(
                min(1.0, max(0.0, frac)),
                None,
                {"chunk": idx + 1, "chunks": chunks, "step": step},
            )

    segments: list[Path] = []
    try:
        for idx in range(chunks):
            start = idx * chunk_seconds
            length = min(chunk_seconds, total_seconds - start)
            chunk = workroot / f"chunk_{idx:04d}"
            in_dir = chunk / "in"
            out_dir = chunk / "out"
            shutil.rmtree(in_dir, ignore_errors=True)
            shutil.rmtree(out_dir, ignore_errors=True)
            in_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            segment = chunk / "segment.mkv"
            try:
                segment.unlink()
            except FileNotFoundError:
                pass

            report(len(segments) / chunks, "extracting frames", idx)
            _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-ss",
                    str(start),
                    "-t",
                    str(length),
                    "-i",
                    plan.input_path,
                    "-an",
                    "-sn",
                    "-vf",
                    _prepare_filter(plan.deinterlace),
                    str(in_dir / "frame_%08d.png"),
                ],
                label=f"upscale frame extraction chunk {idx + 1}/{chunks}",
                cancel=cancel,
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

            report(len(segments) / chunks, "writing mezzanine segment", idx)
            _run_process(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-nostdin",
                    "-framerate",
                    plan.fps,
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
        return str(output)
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
