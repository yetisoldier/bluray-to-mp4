"""DVD AI-upscale stage for the v0.2.19 pipeline.

The stage prepares DVD frames to 720p geometry, runs Real-ESRGAN ncnn/Vulkan,
and writes a video-only mezzanine MKV. The final encode maps video from this
mezzanine and audio from the original MakeMKV rip.
"""

from __future__ import annotations

import math
import os
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
    "run_dvd_upscale",
]


class UpscaleError(RuntimeError):
    """The optional AI-upscale stage failed and should fail open."""


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
    tile_size: int = 2048
    target_height: int = 720


def model_name_for_mode(mode: str) -> str:
    if mode == "720p":
        return "realesrgan-x4plus"
    if mode == "720p_anime":
        return "realesr-animevideov3"
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


def _prepare_filter(deinterlace: bool) -> str:
    filters = []
    if deinterlace:
        filters.append("yadif")
    filters.extend(["scale=-2:360:flags=lanczos", "setsar=1"])
    return ",".join(filters)


def _target_filter(target_height: int) -> str:
    height = max(2, int(target_height or 720))
    if height % 2:
        height += 1
    return f"scale=-2:{height}:flags=lanczos,setsar=1"


def _tile_size_arg(tile_size: int) -> str:
    # The ncnn binary's default tile size is automatic. On large GPUs that
    # should be harmless, but Eric's first real run showed visible 3x2 tile
    # boundaries in the written frames. DVD inputs are tiny for a 3090 Ti, so
    # force a single oversized tile instead of allowing auto spatial splits.
    return str(max(2048, int(tile_size or 2048)))


def _write_concat_list(segments: list[Path]) -> str:
    fh = NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt")
    with fh:
        for segment in segments:
            path = str(segment).replace("'", "'\\''")
            fh.write(f"file '{path}'\n")
    return fh.name


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

    total_seconds = max(1, int(plan.duration_seconds or 1))
    chunk_seconds = max(10, int(plan.chunk_seconds or 120))
    chunks = math.ceil(total_seconds / chunk_seconds)
    workroot = Path(plan.workdir)
    workroot.mkdir(parents=True, exist_ok=True)
    output = Path(plan.output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    segments: list[Path] = []
    try:
        for idx in range(chunks):
            start = idx * chunk_seconds
            length = min(chunk_seconds, total_seconds - start)
            chunk = workroot / f"chunk_{idx:04d}"
            in_dir = chunk / "in"
            out_dir = chunk / "out"
            in_dir.mkdir(parents=True, exist_ok=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            segment = chunk / "segment.mkv"

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

            _run_process(
                [
                    binary,
                    "-i",
                    str(in_dir),
                    "-o",
                    str(out_dir),
                    "-s",
                    "2",
                    "-t",
                    _tile_size_arg(plan.tile_size),
                    "-m",
                    models,
                    "-n",
                    model,
                    "-f",
                    "png",
                ],
                label=f"Real-ESRGAN chunk {idx + 1}/{chunks}",
                cancel=cancel,
            )
            if not any(out_dir.iterdir()):
                raise UpscaleError(f"no upscaled frames produced for chunk {idx + 1}")

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
                    _target_filter(plan.target_height),
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
            if on_progress is not None:
                on_progress(min(1.0, len(segments) / chunks), None)

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
