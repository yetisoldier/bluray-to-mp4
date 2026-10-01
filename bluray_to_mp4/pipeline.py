"""End-to-end pipeline: scan -> detect -> recommend -> rip -> encode.

Movie mode rips the main feature; series mode rips every detected episode
and names them Plex/Jellyfin style. Encoding uses NVENC when available,
x265 otherwise. Subtitle OCR to sidecar SRT lands with issue #2; until
then a selected subtitle stream is still ripped and probed, and the
runner records where the SRT will go.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .binaries import resolve_ffmpeg
from .encode import EncodePlan, build_encode_args
from .ffprobe import ProbedStream, is_interlaced, nth_of_type, probe_streams
from .makemkv import (
    EncodeCancelled,
    Runner,
    _CancelState,
    find_drives,
    make_runner,
    rip_titles,
    scan_disc,
)
from .naming import movie_output_name
from .recommend import Selection, recommend_title
from .series import detect_series, episode_output_names, main_feature
from .subtitles import SubtitleOcrError, run_subtitle_ocr
from .tmdb import TmdbError, lookup_movie, season_episode_titles

if False:  # pragma: no cover - typing only
    from typing import Callable  # noqa: F401

__all__ = [
    "TitleJob",
    "PipelineResult",
    "EncodeCancelled",
    "EncodeError",
    "detect_encoder",
    "run_pipeline",
]


class EncodeError(RuntimeError):
    """ffmpeg encode failed."""


@dataclass
class TitleJob:
    """One title's full processing result (planned or completed)."""

    tid: int
    output_name: str
    duration_seconds: int = 0
    input_path: str = ""
    encode_args: list[str] = field(default_factory=list)
    selection: Selection | None = None
    subtitle_stream_index: int | None = None
    srt_path: str | None = None
    completed: bool = False


@dataclass
class PipelineResult:
    disc_name: str
    media_type: str
    is_series: bool
    encoder: str
    jobs: list[TitleJob] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def detect_encoder(*, binary: str = "ffmpeg", timeout: float = 30.0) -> str:
    """'nvenc' when a hardware HEVC encode succeeds, else 'x265'."""
    binary = resolve_ffmpeg(binary)
    try:
        result = subprocess.run(
            [
                binary, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
                "-c:v", "hevc_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "x265"
    return "nvenc" if result.returncode == 0 else "x265"


def _audio_ordinal(title, sid: int | None) -> int | None:
    """Ordinal of a MakeMKV audio sid among the title's audio streams.

    MakeMKV preserves stream order per type, so this ordinal maps to the
    same-position audio stream in the ripped MKV.
    """
    if sid is None:
        return None
    audio = sorted(title.streams_of("audio"), key=lambda s: s.sid)
    for ordinal, stream in enumerate(audio):
        if stream.sid == sid:
            return ordinal
    return None


def _subtitle_ordinal(title, sid: int | None) -> int | None:
    if sid is None:
        return None
    subs = sorted(title.streams_of("subtitle"), key=lambda s: s.sid)
    for ordinal, stream in enumerate(subs):
        if stream.sid == sid:
            return ordinal
    return None


def _fallback_label(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    return stem.replace("_", " ").strip() or "Episode"


def execute_encode(
    args: list[str],
    *,
    duration_seconds: int,
    binary: str = "ffmpeg",
    on_progress=None,
    cancel=None,
) -> None:
    """Run one ffmpeg encode, reporting progress and optional ETA.

    cancel: callable returning True -> terminate ffmpeg and raise
    CancelledError. Progress callback receives a 0..1 fraction plus an
    ETA-seconds estimate once enough samples exist.
    """
    binary = resolve_ffmpeg(binary)
    proc = subprocess.Popen(
        [binary, *args, "-progress", "pipe:1", "-nostats"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    total_us = max(duration_seconds, 1) * 1_000_000
    started = time.time()
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel is not None and cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise EncodeCancelled("cancelled by user")
            if line.startswith("out_time_us=") and on_progress is not None:
                try:
                    cur = int(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                frac = min(cur / total_us, 1.0)
                eta = None
                if frac > 0.002:  # enough samples for a stable rate
                    elapsed = time.time() - started
                    eta = max(elapsed / frac - elapsed, 0.0)
                on_progress(frac, eta)
    finally:
        proc.stdout.close()
        rc = proc.wait()
    if rc != 0 and not (cancel is not None and cancel()):
        raise EncodeError(f"ffmpeg exited with code {rc}")
    elif cancel is not None and cancel() and rc != 0:
        raise EncodeCancelled("cancelled by user")


def run_pipeline(
    index: int,
    outdir: str,
    *,
    encoder: str = "auto",
    dry_run: bool = False,
    mkv_runner: Runner | None = None,
    workdir: str | None = None,
    series_name: str | None = None,
    season: int = 1,
    start_episode: int = 1,
    episode_titles: dict[int, str] | None = None,
    year: int | None = None,
    title_overrides: dict[int, str] | None = None,
    cancel: "Callable[[], bool] | None" = None,
    tmdb_key: str = "",
    tmdb_fetcher=None,
    force_deinterlace: bool | None = None,
    on_stage=None,
    on_progress=None,
) -> PipelineResult:
    """Process one disc end to end.

    dry_run=True builds and returns the full plan (rip/encode arguments
    included) without touching makemkvcon or ffmpeg beyond scanning the
    disc, which makes the whole chain testable offline.

    cancel: callable returning True -> stop the run. Raise EncodeCancelled
    at the next check (between titles, mid-rip, mid-encode, before OCR);
    the exception carries the partial PipelineResult on .result.

    on_progress update tuples, one per progress line from each stage:
      ("rip", tid, frac)                 - MakeMKV PRGV progress, no ETA
      ("encode", tid, frac, eta-or-None) - ffmpeg progress; ETA-seconds is
                                          None until frac passes 0.2%, a
                                          float estimate afterwards
    """
    def stage(msg: str):
        if on_stage is not None:
            on_stage(msg)

    runner = mkv_runner
    cancel_state: _CancelState | None = None
    if runner is None:
        cancel_state = _CancelState()
        if cancel is not None:
            orig_cancel = cancel

            def cancel_and_flag():  # noqa: F811
                if orig_cancel():
                    cancel_state.requested = True
                return cancel_state.requested

            cancel = cancel_and_flag
        runner = make_runner(cancel_state=cancel_state)
    stage(f"Scanning disc {index} ...")
    disc = scan_disc(index, runner=runner)
    guess = detect_series(disc)

    result = PipelineResult(
        disc_name=disc.name,
        media_type=disc.media_type,
        is_series=guess.is_series,
        encoder=encoder if encoder != "auto" else None,  # resolved below
        reasons=list(guess.reasons),
    )

    if guess.is_series:
        stage(f"Series disc: {guess.episode_count} episodes detected")
        base_names = {t.tid: _fallback_label(t.filename) for t in disc.titles}
        if episode_titles is None and tmdb_key:
            try:
                titles = season_episode_titles(
                    series_name or disc.name, season,
                    start_episode=start_episode,
                    count=len(guess.episode_tids),
                    key=tmdb_key, fetcher=tmdb_fetcher,
                )
                episode_titles = {
                    tid: title for tid, title in zip(guess.episode_tids, titles)
                    if title
                }
                result.reasons.append(
                    f"Episode titles from TMDB ({len(episode_titles)} found)"
                )
            except TmdbError as exc:
                result.warnings.append(
                    f"TMDB lookup failed, using manual naming: {exc}"
                )
        names = episode_output_names(
            series_name or disc.name,
            season,
            start_episode=start_episode,
            episode_tids=guess.episode_tids,
            episode_titles=episode_titles,
            base_names=base_names,
            title_overrides=title_overrides,
        )
        selected = {tid: recommend_title(disc.title(tid)) for tid, _ in names}
    else:
        main = main_feature(disc)
        if main is None:
            raise RuntimeError("No titles long enough to process on this disc.")
        stage(f"Movie disc: main feature {main.duration_seconds // 60} min")
        if year is None and tmdb_key:
            try:
                hit = lookup_movie(disc.name, key=tmdb_key, fetcher=tmdb_fetcher)
                if hit and hit.get("year"):
                    year = hit["year"]
                    result.reasons.append(
                        f"Year {year} from TMDB ({hit.get('title')})"
                    )
            except TmdbError as exc:
                result.warnings.append(f"TMDB year lookup failed: {exc}")
        name = movie_output_name(disc.name, year=year)
        names = [(main.tid, name)]
        selected = {main.tid: recommend_title(main)}

    result.encoder = encoder if encoder != "auto" else detect_encoder()
    if result.encoder == "nvenc":
        result.reasons.append("NVENC hardware encoder detected")
    else:
        result.reasons.append("Using x265 software encoder (no NVENC detected)")

    os.makedirs(outdir, exist_ok=True)
    rip_dir = workdir or os.path.join(outdir, ".rip")
    os.makedirs(rip_dir, exist_ok=True)

    for tid, out_name in names:
        title = disc.title(tid)
        sel = selected[tid]
        job = TitleJob(
            tid=tid,
            output_name=out_name,
            selection=sel,
            input_path=os.path.join(rip_dir, title.filename),
        )
        job.encode_args = build_encode_args(
            EncodePlan(
                input_path=job.input_path,
                output_path=os.path.join(outdir, out_name),
                duration_seconds=title.duration_seconds,
                media_type=disc.media_type or "bluray",
                encoder=result.encoder,
                audio_sid=_audio_ordinal(title, sel.audio_sid),
            )
        )
        result.jobs.append(job)

    if dry_run:
        stage("Dry run: no rip or encode will be executed")
        for job in result.jobs:
            sub_ord = _subtitle_ordinal(
                disc.title(job.tid), job.selection.subtitle_sid
            )
            job.subtitle_stream_index = sub_ord
            if job.selection.subtitle_sid is not None:
                job.srt_path = os.path.splitext(
                    os.path.join(outdir, job.output_name)
                )[0] + ".srt"
                result.warnings.append(
                    "Subtitle OCR lands with roadmap #2; SRT path reserved: "
                    + job.srt_path
                )
        return result

    # Real run: rip, probe, resolve, encode - one title at a time.
    try:
        for job in result.jobs:
            if cancel is not None and cancel():
                raise EncodeCancelled("cancelled between titles")
            title = disc.title(job.tid)
            stage(f"Ripping title {job.tid} ({job.output_name}) ...")
            saved = rip_titles(
                index, [job.tid], rip_dir, runner=runner,
                on_progress=lambda frac, prgv: on_progress(("rip", job.tid, frac))
                if on_progress else None,
                cancel=cancel,
            )
            job.input_path = saved[0] if saved else job.input_path

            stage(f"Probing {os.path.basename(job.input_path)} ...")
            streams = probe_streams(job.input_path)
            audio_ord = _audio_ordinal(title, job.selection.audio_sid)
            audio = nth_of_type(streams, "audio", audio_ord or 0)
            if audio is None:
                raise RuntimeError(
                    f"expected an audio stream in {job.input_path}; rip may be corrupt"
                )
            deinterlace = is_interlaced(streams) if force_deinterlace is None else force_deinterlace

            sub_ord = _subtitle_ordinal(title, job.selection.subtitle_sid)
            sub = nth_of_type(streams, "subtitle", sub_ord) if sub_ord is not None else None
            job.subtitle_stream_index = sub.index if sub else None

            stage(f"Encoding {job.output_name} ({result.encoder}) ...")
            job.encode_args = build_encode_args(
                EncodePlan(
                    input_path=job.input_path,
                    output_path=os.path.join(outdir, job.output_name),
                    duration_seconds=title.duration_seconds,
                    media_type=disc.media_type or "bluray",
                    encoder=result.encoder,
                    audio_sid=nth_ordinal(streams, audio),
                    deinterlace=deinterlace,
                )
            )
            execute_encode(
                job.encode_args,
                duration_seconds=title.duration_seconds,
                on_progress=lambda frac, eta: on_progress(
                    ("encode", job.tid, frac, eta)
                )
                if on_progress else None,
                cancel=cancel,
            )
            job.completed = True

            if sub is not None:
                if cancel is not None and cancel():
                    raise EncodeCancelled("cancelled before subtitle OCR")
                stage(f"OCR subtitles for {job.output_name} ...")
                video_out = os.path.join(outdir, job.output_name)
                try:
                    ocr = run_subtitle_ocr(
                        job.input_path, sub.index, video_out, workdir=rip_dir,
                    )
                    job.srt_path = ocr.srt_path
                    result.warnings.extend(f"{job.output_name}: {n}" for n in ocr.notes)
                except SubtitleOcrError as exc:
                    result.warnings.append(
                        f"{job.output_name}: subtitle OCR failed ({exc}); "
                        "MP4 was produced without subtitles."
                    )

            stage(f"Done: {job.output_name}")

        # Full success + default workdir: the .rip intermediates are
        # reproducible junk, so reclaim the disk. A caller-supplied
        # workdir is never touched (it may be shared/persistent).
        if rip_dir == os.path.join(outdir, ".rip") and all(
            job.completed for job in result.jobs
        ):
            shutil.rmtree(rip_dir, ignore_errors=True)
    except Exception as exc:
        # Failure or cancel: keep the intermediates for debugging and
        # surface the partial result (with this note) on the exception.
        result.warnings.append(f"Kept intermediates in {rip_dir} for debugging")
        if getattr(exc, "result", None) is None:
            exc.result = result
        raise

    return result


def nth_ordinal(streams: list[ProbedStream], target: ProbedStream) -> int:
    """Ordinal of a stream within its own type (for 0:a:N / -map usage)."""
    n = 0
    for s in streams:
        if s.codec_type == target.codec_type:
            if s.index == target.index:
                return n
            n += 1
    return 0