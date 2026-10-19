"""End-to-end pipeline: scan -> detect -> recommend -> rip -> encode.

Movie mode rips the main feature; series mode rips every detected episode
and names them Plex/Jellyfin style. Encoding uses NVENC when available,
x265 otherwise. A selected subtitle stream is OCR'd to a sidecar SRT
(shipped in v0.2.2); dry runs report the reserved SRT path instead of
running the OCR.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .binaries import resolve_ffmpeg
from .encode import (
    BLURAY_GB_PER_HOUR,
    DVD_GB_PER_HOUR,
    EncodePlan,
    build_encode_args,
)

DVD_UPSCALE_GB_PER_HOUR = 1.5
DVD_UPSCALE_MODES = {"off", "720p", "720p_anime"}
# v0.2.35 §2.2: the gate-block finalize emissions clamp the bar at 0.999
# (the mezzanine is not proven until BOTH gates pass; a 1.0 emission
# makes the CLI print a newline per heartbeat tick).
_FINALIZE_FRAC_GATE = 0.999
from .ffprobe import (
    CfrVerdict,
    FfprobeError,
    FrameCountProbe,
    ProbedStream,
    first_video_stream,
    first_video_width,
    is_interlaced,
    nth_of_type,
    probe_audio_duration_seconds,
    probe_cfr_check,
    probe_duration_seconds,
    probe_frame_count,
    probe_streams,
    snap_to_canonical_rate,
)
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
from .subtitles import (
    SubtitleOcrError,
    mux_language,
    mux_srt_into_mp4,
    run_subtitle_ocr,
)
from .tmdb import TmdbError, lookup_movie, season_episode_titles
from .upscale import (
    FINALIZE_STEP_VERIFY_CLOCK,
    FINALIZE_STEP_VERIFY_FRAMES,
    ProgressHeartbeat,
    UpscaleError,
    UpscalePlan,
    _FINALIZE_HEARTBEAT_SECONDS,
    run_dvd_upscale,
    verify_mezzanine_clock,
    verify_mezzanine_frames,
)

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
    upscale_mode: str = "off"
    upscale_mezzanine_path: str | None = None
    completed: bool = False
    error: str | None = None  # set when this job failed but the queue continued


@dataclass
class PipelineResult:
    disc_name: str
    media_type: str
    is_series: bool
    encoder: str
    jobs: list[TitleJob] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # per-job failures (C6)


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


# v0.2.15 (design Gap 2): rip-reuse width verification. A DVD and a
# Blu-ray pressing of the same movie match on duration; the video width
# is what separates the two masters.
_RESOLUTION_RE = re.compile(r"(\d{3,5})[x\u00d7](\d{3,5})")
_DVD_WIDTH_MAX = 854    # anamorphic PAL/NTSC ceilings
_BLURAY_WIDTH_MIN = 1280


def _scanned_video_width(title) -> int | None:
    """Width the disc scan reports for the title's first video stream
    (SINFO attr 19, e.g. '3840x2160'), or None when absent/unparsable."""
    videos = title.streams_of("video")
    if not videos:
        return None
    match = _RESOLUTION_RE.search(videos[0].resolution or "")
    return int(match.group(1)) if match else None


def _was_overridden(job: TitleJob, stream_overrides: dict | None) -> bool:
    """True when the user picked a specific subtitle stream for this
    title on the Review page (v0.2.15). An explicit choice disables the
    rip-time first-stream fallback exactly like an explicit decline
    does; only an untouched recommendation keeps the fallback."""
    entry = (stream_overrides or {}).get(job.tid) or {}
    return entry.get("subtitle_sid") is not None


def _apply_stream_overrides(
    sel: Selection,
    title,
    override: dict[str, int | None] | None,
) -> tuple[Selection, str | None]:
    """Merge one Review-page stream override into a recommendation.

    override = {"audio_sid": int | None, "subtitle_sid": int | None} for
    this title's tid. Returns (selection, note); note is a warning string
    whenever the user's choice was applied or had to be ignored, so the
    History page can see the pick was manual (v0.2.15 design Gap 1).

    Rules:
    - override None/empty or a key absent -> that choice is untouched.
    - audio_sid must exist among the title's audio streams; an unknown
      sid keeps the recommendation (never encode a silent file on a
      typo'd/stale sid) and is reported. audio_sid None also keeps the
      recommendation - audio is required, so None means "unchanged".
    - subtitle_sid None means "user wants no subtitles": subtitle_sid is
      cleared and subtitle_user_declined is set so the rip-time
      first-stream fallback stays off for this title.
    - subtitle_sid with a real value must exist among the title's
      subtitle streams; an unknown sid keeps the recommendation + note.
    """
    if not override:
        return sel, None
    applied: list[str] = []
    ignored: list[str] = []
    audio_sid = override.get("audio_sid")
    if audio_sid is not None:
        if any(s.sid == audio_sid for s in title.streams_of("audio")):
            sel.audio_sid = audio_sid
            applied.append(f"audio sid {audio_sid}")
        else:
            ignored.append(f"audio sid {audio_sid}")
    if "subtitle_sid" in override:
        sub_sid = override["subtitle_sid"]
        if sub_sid is None:
            # "(no subtitles)" picked: record the explicit decline so the
            # rip-time fallback can tell "no selection" from "none".
            sel.subtitle_sid = None
            sel.subtitle_user_declined = True
            applied.append("subtitles: none")
        elif any(s.sid == sub_sid for s in title.streams_of("subtitle")):
            sel.subtitle_sid = sub_sid
            applied.append(f"subtitle sid {sub_sid}")
        else:
            ignored.append(f"subtitle sid {sub_sid}")
    notes: list[str] = []
    if applied:
        notes.append(
            f"Title {title.tid}: user override - {', '.join(applied)}"
        )
    if ignored:
        notes.append(
            f"Title {title.tid}: ignored user override "
            f"({', '.join(ignored)} not on this disc); "
            "kept the recommendation"
        )
    return sel, (" ".join(notes) if notes else None)


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


def _normalize_dvd_upscale_mode(mode: str | None, media_type: str | None) -> str:
    if media_type != "dvd":
        return "off"
    mode = mode or "off"
    if mode not in DVD_UPSCALE_MODES:
        raise ValueError(f"unknown DVD upscale mode: {mode}")
    return mode


def _upscale_mezzanine_path(rip_dir: str, tid: int) -> str:
    return os.path.join(rip_dir, ".upscale", f"title_{tid:02d}_720p.mkv")


def _retain_rejected_mezzanine(job, rip_dir, probe_ctx, error_text):
    """v0.2.33 retention (design §4): keep a rejected mezzanine.

    On a gate trip the mezzanine at job.upscale_mezzanine_path is
    renamed to <name>_720p.rejected-<ISO8601>.mkv (never deleted; the
    operator decides) next to a written <same>.diagnostic.json with
    expected vs actual counts, the fps probe values, and the per-chunk
    expectations, so drift is measurable from artifacts, not logs.
    probe_ctx carries the pipeline's live pre-flight locals (rip_fps,
    frame_probe, cfr, rip_total_frames) from the except site. Returns
    (rejected_path, diagnostic_path) or None when nothing was on disk
    to retain. Never raises - retention must not mask the original
    failure.
    """
    try:
        mezz = job.upscale_mezzanine_path
        if not mezz or not os.path.isfile(mezz):
            return None
        mezz_path = Path(mezz)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        stem = mezz_path.stem  # title_00_720p
        rejected = mezz_path.with_name(f"{stem}.rejected-{stamp}.mkv")
        os.replace(mezz, rejected)
        diagnostic = mezz_path.with_name(f"{stem}.rejected-{stamp}.diagnostic.json")

        rip_fps = probe_ctx.get("rip_fps")
        frame_probe = probe_ctx.get("frame_probe")
        cfr = probe_ctx.get("cfr")
        audio_probe = probe_ctx.get("audio_probe")
        derived_rate = probe_ctx.get("derived_rate")
        rate_snapped = probe_ctx.get("rate_snapped")
        audio_ordinal = probe_ctx.get("audio_ordinal")
        rip_total = probe_ctx.get("rip_total_frames") or 0
        chunk_frames = None
        expected_per_chunk: list[int] = []
        try:
            from .upscale import compute_chunk_bounds, compute_chunk_frames

            if rip_total and rip_fps:
                chunk_frames = compute_chunk_frames(120, rip_fps)
                expected_per_chunk = [
                    count for _, count in compute_chunk_bounds(rip_total, chunk_frames)
                ]
        except Exception:
            chunk_frames = None
            expected_per_chunk = []
        mezzanine_frame_count = None
        try:
            probe = probe_frame_count(str(rejected))
            mezzanine_frame_count = int(probe.total_frames)
        except Exception:
            mezzanine_frame_count = None
        # v0.2.34 §4: the diagnostic records the full rate picture - the
        # container's claimed rates next to the decode-truth derivation
        # (raw + snapped + the chosen stamp), the audio duration and its
        # source, and (when the clock gate tripped post-upscale) the
        # mezzanine's own container duration and the gate's tolerance.
        mezzanine_duration = None
        clock_tolerance = None
        audio_seconds = getattr(audio_probe, "seconds", None)
        format_seconds = getattr(audio_probe, "format_seconds", None)
        format_rate = (
            rip_total / format_seconds
            if rip_total and format_seconds
            else None
        )
        if audio_seconds:
            try:
                mezzanine_duration = probe_duration_seconds(str(rejected))
            except Exception:
                mezzanine_duration = None
            clock_tolerance = max(2.0, 0.001 * float(audio_seconds))
        payload = {
            "rip_path": job.input_path,
            "rip_total_frames": rip_total,
            "frame_count_from_metadata": bool(
                getattr(frame_probe, "from_metadata", False)
            ),
            "rip_fps": rip_fps,
            "r_frame_rate": getattr(cfr, "r_frame_rate", ""),
            "avg_frame_rate": getattr(cfr, "avg_frame_rate", ""),
            "cfr_verdict": getattr(cfr, "verdict", ""),
            "audio_duration_seconds": getattr(audio_probe, "seconds", None),
            "audio_duration_source": (
                "format" if getattr(audio_probe, "from_format", False)
                else "stream" if audio_probe is not None else None
            ),
            "audio_stream_ordinal": (
                audio_ordinal if audio_ordinal is not None
                else getattr(audio_probe, "stream_ordinal", None)
            ),
            "derived_rate_raw": derived_rate,
            "derived_rate_snapped": rate_snapped if rate_snapped is not None else (
                None if derived_rate is None else True
            ),
            "stamp_fps": rip_fps,
            "container_r_frame_rate": getattr(cfr, "r_frame_rate", ""),
            "container_avg_frame_rate": getattr(cfr, "avg_frame_rate", ""),
            "mezzanine_duration_seconds": mezzanine_duration,
            "clock_tolerance_seconds": clock_tolerance,
            # v0.2.35 §7.2: the C.3 cross-provenance figures land in the
            # rejection diagnostic too (additive keys, never raise).
            "format_duration_seconds": format_seconds,
            "format_derived_rate": format_rate,
            "chunks": len(expected_per_chunk),
            "chunk_frames": chunk_frames,
            "expected_per_chunk": expected_per_chunk,
            "mezzanine_frame_count": mezzanine_frame_count,
            "error": error_text,
        }
        with open(diagnostic, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return (str(rejected), str(diagnostic))
    except Exception:
        return None


def _resolve_gbph(value: float | None, media_type: str | None) -> float | None:
    """v0.2.18: Review-page size target resolution. An explicit value
    wins; None or a value equal to the disc type's default collapses
    to None, which EncodePlan already maps to its built-in media-type
    constant - so untouched runs stay byte-identical in dry-run output
    and EncodePlan reprs."""
    default = DVD_GB_PER_HOUR if media_type == "dvd" else BLURAY_GB_PER_HOUR
    return None if value is None or abs(value - default) < 1e-9 else value


def run_pipeline(
    index: int,
    outdir: str,
    *,
    encoder: str = "auto",
    dry_run: bool = False,
    mkv_runner: Runner | None = None,
    workdir: str | None = None,
    movie_name: str | None = None,
    series_name: str | None = None,
    season: int = 1,
    start_episode: int = 1,
    episode_titles: dict[int, str] | None = None,
    year: int | None = None,
    title_overrides: dict[int, str] | None = None,
    stream_overrides: dict[int, dict[str, int | None]] | None = None,  # v0.2.15
    target_gb_per_hour: float | None = None,  # v0.2.18
    dvd_upscale_mode: str = "off",  # v0.2.19
    cancel: "Callable[[], bool] | None" = None,
    tmdb_key: str = "",
    tmdb_fetcher=None,
    want_subtitles: bool = True,
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

    want_subtitles=False honors the UI "Include subtitles" toggle: no
    subtitle ordinal is resolved, no OCR runs, no .srt path is reserved
    (and no reserved-path warning in dry runs).

    stream_overrides (v0.2.15, Review-page combos) maps tid ->
    {"audio_sid": int | None, "subtitle_sid": int | None} and is merged
    into the recommendation per title: a sid must exist on the disc
    (invalid ones keep the recommendation + a warning note), an explicit
    subtitle None means "no subtitles for this title" and disables the
    rip-time first-stream fallback for it.

    movie_name: the reviewed movie title shown in the UI. It overrides the
    raw disc volume label for TMDB lookup and the final output filename.

    target_gb_per_hour (v0.2.18, Review-page size target): explicit value
    wins over the media-type default; None (or a value equal to the
    disc's default) leaves EncodePlan on its built-in default.

    dvd_upscale_mode (v0.2.19): DVD-only optional upscale contract:
    "off", "720p", or "720p_anime". Non-DVD scans force this to "off".

    on_progress update tuples, one per progress line from each stage:
      ("rip", tid, frac)                 - MakeMKV PRGV progress, no ETA
      ("upscale", tid, frac, eta-or-None, detail-or-None) - AI-upscale
                                          stage progress; detail is a dict with
                                          chunk/chunks/step when available
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
    # G4: B2M_TMDB_KEY env var honored (CLI help promises it); an explicit
    # tmdb_key argument always wins.
    if not (tmdb_key or "").strip():
        tmdb_key = (os.environ.get("B2M_TMDB_KEY", "") or "").strip()

    result = PipelineResult(
        disc_name=disc.name,
        media_type=disc.media_type,
        is_series=guess.is_series,
        encoder=encoder if encoder != "auto" else None,  # resolved below
        reasons=list(guess.reasons),
    )
    upscale_mode = _normalize_dvd_upscale_mode(
        dvd_upscale_mode, disc.media_type)
    if upscale_mode != "off":
        result.reasons.append(
            "DVD AI upscale planned: "
            + ("720p anime" if upscale_mode == "720p_anime" else "720p")
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
        selected = {}
        for tid, _ in names:
            t = disc.title(tid)
            sel = recommend_title(t)
            sel, note = _apply_stream_overrides(
                sel, t, (stream_overrides or {}).get(tid))
            selected[tid] = sel
            if note:
                result.warnings.append(note)
    else:
        main = main_feature(disc)
        if main is None:
            raise RuntimeError("No titles long enough to process on this disc.")
        stage(f"Movie disc: main feature {main.duration_seconds // 60} min")
        if year is None and tmdb_key:
            try:
                hit = lookup_movie(
                    movie_name or disc.name, key=tmdb_key, fetcher=tmdb_fetcher)
                if hit and hit.get("year"):
                    year = hit["year"]
                    result.reasons.append(
                        f"Year {year} from TMDB ({hit.get('title')})"
                    )
            except TmdbError as exc:
                result.warnings.append(f"TMDB year lookup failed: {exc}")
        name = movie_output_name(movie_name or disc.name, year=year)
        names = [(main.tid, name)]
        sel = recommend_title(main)
        sel, note = _apply_stream_overrides(
            sel, main, (stream_overrides or {}).get(main.tid))
        selected = {main.tid: sel}
        if note:
            result.warnings.append(note)

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
            duration_seconds=title.duration_seconds,
            input_path=os.path.join(rip_dir, title.filename),
            upscale_mode=upscale_mode,
            upscale_mezzanine_path=(
                _upscale_mezzanine_path(rip_dir, tid)
                if upscale_mode != "off" else None
            ),
        )
        job.encode_args = build_encode_args(
            EncodePlan(
                input_path=job.input_path,
                video_input_path=job.upscale_mezzanine_path,
                output_path=os.path.join(outdir, out_name),
                duration_seconds=title.duration_seconds,
                media_type=disc.media_type or "bluray",
                encoder=result.encoder,
                audio_sid=_audio_ordinal(title, sel.audio_sid),
                target_gb_per_hour=_resolve_gbph(
                    target_gb_per_hour, disc.media_type),
            )
        )
        result.jobs.append(job)

    if dry_run:
        stage("Dry run: no rip or encode will be executed")
        for job in result.jobs:
            sub_ord = (
                _subtitle_ordinal(disc.title(job.tid), job.selection.subtitle_sid)
                if want_subtitles else None
            )
            job.subtitle_stream_index = sub_ord
            if want_subtitles and job.selection.subtitle_sid is not None:
                job.srt_path = os.path.splitext(
                    os.path.join(outdir, job.output_name)
                )[0] + ".srt"
                result.warnings.append(
                    "Subtitle OCR will run (subtitle stream selected, "
                    "subtitles on); a stream missing from the rip falls "
                    "back to the first subtitle track (v0.2.13). "
                    "SRT sidecar reserved: " + job.srt_path
                )
        return result

    # A prior cancelled/killed run can leave partial .mkv intermediates
    # behind; makemkvcon robot mode then HANGS on its interactive
    # "already exists - overwrite?" prompt with nobody to answer it
    # (validation run 3, 2026-10-02). The .rip dir is ours alone - purge
    # stale contents before ripping so every run starts clean.
    # v0.2.12: files this run actually plans to produce are EXPECTED and
    # survive the purge - a complete leftover rip is duration-verified in
    # the job loop below and reused instead of re-ripped (Eric's "Mamma
    # Mia! Here We Go Again": a finished t00.mkv sat in .rip after the
    # cancelled run). Only files no planned job expects are removed.
    # v0.2.35 §3.6: when any job plans an upscale, the whole .upscale
    # tree (per-title workroots + mezzanines) is expected too - it is
    # the per-chunk resume material this run may verify and reuse, not
    # stale output. The anti-hang guarantee is unaffected: the purge
    # exists for makemkvcon's interactive overwrite prompt, which only
    # concerns top-level rip outputs; .upscale/ is never a makemkv
    # target. Upscale off (or a non-DVD scan) keeps the old behavior -
    # a leftover .upscale tree IS stale then.
    _expected = {os.path.normcase(job.input_path) for job in result.jobs}
    if any(job.upscale_mode != "off" for job in result.jobs):
        _expected.add(
            os.path.normcase(os.path.join(rip_dir, ".upscale")))
    for _stale in os.listdir(rip_dir):
        _p = os.path.join(rip_dir, _stale)
        if os.path.normcase(_p) in _expected:
            continue
        try:
            if os.path.isdir(_p):
                shutil.rmtree(_p, ignore_errors=True)
            else:
                os.remove(_p)
        except OSError:
            pass  # locked file: makemkv will surface its own error

    # Real run: rip, probe, resolve, encode - one title at a time.
    # C6: a job failure is isolated - recorded on the job and in
    # result.errors, and the queue continues with the next title.
    # EncodeCancelled still propagates (cancel is not a job failure).
    try:
        for job in result.jobs:
            if cancel is not None and cancel():
                raise EncodeCancelled("cancelled between titles")
            title = disc.title(job.tid)
            try:
                # v0.2.12: a complete rip already sitting in the workdir
                # (left by a cancelled run after its rip finished) is
                # ffprobe-verified and REUSED - no hours-long re-rip.
                # Corrupt/partial leftovers are deleted per-file, so the
                # v0.2.8 anti-hang guarantee (makemkvcon's interactive
                # overwrite prompt never has anyone to answer it) holds.
                reusable = False
                if os.path.isfile(job.input_path):
                    stage(
                        f"Verifying existing {os.path.basename(job.input_path)} ..."
                    )
                    probed = None
                    try:
                        probed = probe_duration_seconds(job.input_path)
                    except Exception:
                        probed = None  # corrupt/unreadable -> re-rip
                    reusable = (
                        probed is not None
                        and title.duration_seconds > 0
                        and abs(probed - title.duration_seconds)
                        <= max(60.0, 0.05 * title.duration_seconds)
                    )
                    # v0.2.15 (design Gap 2): duration alone cannot tell a
                    # DVD pressing of a movie from its Blu-ray twin - same
                    # runtime, different master. Compare the existing rip's
                    # video width against what the current disc's scan says
                    # this title should be (tier 1), falling back to a
                    # media_type width class (tier 2); a rejected file is
                    # deleted and re-ripped - failing closed is correct for
                    # a destructive-reuse decision. One extra probe_streams
                    # call per candidate, only after duration passes.
                    scanned_width = None
                    existing_width = None
                    width_checked = False
                    if reusable:
                        scanned_width = _scanned_video_width(title)
                        try:
                            existing_width = first_video_width(
                                probe_streams(job.input_path))
                        except Exception:
                            existing_width = None  # probe failure -> reject
                        width_checked = True
                        if scanned_width is not None:
                            reusable = existing_width == scanned_width
                        elif disc.media_type == "dvd":
                            reusable = (
                                existing_width is not None
                                and existing_width <= _DVD_WIDTH_MAX
                            )
                        elif disc.media_type == "bluray":
                            reusable = (
                                existing_width is not None
                                and existing_width >= _BLURAY_WIDTH_MIN
                            )
                        # else: neither source known - duration check alone
                        # decides (v0.2.12 behavior, deliberate tier 3).
                    if reusable:
                        stage(
                            f"Reusing existing rip of {job.output_name}"
                            " - skipping re-rip"
                        )
                    else:
                        if width_checked:
                            # A different master, not a partial file - the
                            # scan (or media class) says this rip is not
                            # the one this disc would produce.
                            stage(
                                f"Existing rip of {job.output_name} is a "
                                f"different master (width {existing_width or '?'} "
                                f"vs expected "
                                f"{scanned_width or disc.media_type or '?'}) "
                                "- re-ripping"
                            )
                        try:
                            os.remove(job.input_path)
                        except OSError:
                            pass  # locked file: makemkv surfaces its own error
                if not reusable:
                    stage(f"Ripping title {job.tid} ({job.output_name}) ...")
                    _rip_t0 = time.time()
                    _rip_last_frac = 0.0
                    _rip_last_eta: float | None = None

                    def _rip_progress(frac, prgv, *, _job=job):
                        # Rip ETA from elapsed/fraction once data actually moves;
                        # before the BD+ open finishes PRGV stays 0 and no honest
                        # estimate exists yet (eta stays None). Once an estimate
                        # exists it is CARRIED FORWARD on every update, including
                        # duplicate-frac PRGV lines - consumers must never see
                        # the estimate vanish between real ticks (validation run
                        # 5: the UI's Remaining label flickered back to
                        # "estimating ..." on duplicates and looked stuck).
                        nonlocal _rip_t0, _rip_last_frac, _rip_last_eta
                        if frac <= 0.002:
                            on_progress(("rip", _job.tid, frac))
                            return
                        elapsed = time.time() - _rip_t0
                        if frac > _rip_last_frac:
                            _rip_last_eta = max(0.0, elapsed / frac - elapsed)
                        _rip_last_frac = frac
                        on_progress(("rip", _job.tid, frac, _rip_last_eta))

                    def _rip_info(text):
                        # Live MakeMKV status (BD+ open etc.) so the rip phase
                        # never looks frozen while PRGV sits at 0.
                        on_stage(f"{text} - ripping {job.output_name}")

                    saved = rip_titles(
                        index, [job.tid], rip_dir, runner=runner,
                        on_progress=_rip_progress if on_progress else None,
                        on_info=_rip_info if on_stage else None,
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
                video_stream = first_video_stream(streams)

                sub_ord = (
                    _subtitle_ordinal(title, job.selection.subtitle_sid)
                    if want_subtitles else None
                )
                sub = nth_of_type(streams, "subtitle", sub_ord) if sub_ord is not None else None
                if sub is None and want_subtitles and not (
                    job.selection.subtitle_user_declined
                    or _was_overridden(job, stream_overrides)
                ):
                    # Fallback (validation run 7): the disc-scan sid and the
                    # ripped MKV's stream list can disagree; rather than
                    # shipping no subtitles, use the FIRST subtitle stream
                    # (MakeMKV preserves scan order in the rip) so English
                    # subs are included by default - players can turn them
                    # off per Eric's preference.
                    # v0.2.15: only an UNTOUCHED recommendation keeps the
                    # fallback - a Review-page pick (or an explicit
                    # "(no subtitles)") is the user's call, not ours.
                    subs_in_rip = [s for s in streams if s.codec_type == "subtitle"]
                    if subs_in_rip:
                        sub = subs_in_rip[0]
                        result.warnings.append(
                            f"{job.output_name}: selected subtitle stream missing from "
                            f"ripped MKV; fell back to subtitle stream {sub.index}."
                        )
                job.subtitle_stream_index = sub.index if sub else None
                # G3: don't silently drop - the recommendation promised a track
                if sub_ord is not None and sub is None:
                    result.warnings.append(
                        f"{job.output_name}: selected subtitle stream missing from "
                        "ripped MKV; the MP4 ships without subtitles."
                    )

                video_input_path = None
                if job.upscale_mode != "off":
                    stage(f"Upscaling {job.output_name} ({job.upscale_mode}) ...")
                    # v0.2.33 pre-flight (design §2.1/§2.2): the authoritative
                    # decode-counted frame count and the CFR verdict run
                    # once, BEFORE run_dvd_upscale, so a bad rip costs ~30 s
                    # of probing instead of GPU hours on a doomed run. A VFR
                    # or UNKNOWN verdict raises UpscaleError INSIDE this try
                    # so it hits the same fail-open handler - the pipeline
                    # never enters the GPU stage for a refused rip.
                    #
                    # v0.2.34 (design §1.3/§1.4): plan.fps is now DERIVED
                    # from decode truth - rip_total_frames divided by the
                    # selected audio stream's measured duration, snapped
                    # to a canonical rate - instead of the container's
                    # r_frame_rate/avg_frame_rate metadata (both fields
                    # lied uniformly on the 2026-10-18 Mamma Mia HWA
                    # probe: 30000/1001 on a soft-telecined 23.976 film).
                    # The .33 VFR/UNKNOWN refusal runs first (unchanged);
                    # the snap then runs on CFR verdicts only. The metadata
                    # tokens survive solely as diagnostic fields. An
                    # unsnappable rate refuses here, inside the same try,
                    # so it hits the same fail-open handler: honest no in
                    # ~30 s of probes, no GPU burn.
                    try:
                        try:
                            frame_probe = probe_frame_count(job.input_path)
                        except FfprobeError as exc:
                            # §2.1: a rip whose count cannot be probed is
                            # refused BEFORE any chunk runs - honest no in
                            # ~30 s, no GPU time burned. Fail open via the
                            # same UpscaleError handler as every other
                            # upscale-stage refusal.
                            raise UpscaleError(str(exc)) from exc
                        rip_total_frames = int(frame_probe.total_frames)
                        # v0.2.35 C.2 (Karen's finding): probe_cfr_check
                        # was the ONE unwrapped probe in the pre-flight -
                        # a raw FfprobeError (missing binary, nonzero
                        # exit, malformed JSON) escaped the inner try's
                        # UpscaleError-only handler and hit the per-job
                        # except Exception, failing the whole job over
                        # a tool failure on a healthy rip. Wrapped like
                        # its siblings: same handler, fail OPEN to the
                        # original rip.
                        try:
                            cfr = probe_cfr_check(job.input_path)
                        except FfprobeError as exc:
                            raise UpscaleError(
                                f"CFR pre-flight could not probe the rip "
                                f"({exc}); refusing to upscale a rip whose "
                                "clock cannot be verified"
                            ) from exc
                        if cfr.verdict != "CFR":
                            reason = (
                                "variable frame rate"
                                if cfr.verdict == "VFR"
                                else "frame rate could not be determined"
                            )
                            raise UpscaleError(
                                f"rip has a {reason} (r_frame_rate="
                                f"{cfr.r_frame_rate!r}, avg_frame_rate="
                                f"{cfr.avg_frame_rate!r}); a variable-clock rip "
                                "cannot be reassembled at a constant frame rate "
                                "without re-creating drift"
                            )
                        # §1.1: the denominator is the exact audio ordinal
                        # the encode maps from (nth_ordinal of the selected
                        # stream); encode's own None fallback maps a:0?, so
                        # a None ordinal probes a:0 the same way.
                        audio_ordinal = nth_ordinal(streams, audio)
                        try:
                            audio_probe = probe_audio_duration_seconds(
                                job.input_path, audio_ordinal)
                        except FfprobeError as exc:
                            # §1.1 sanity refusals (no usable figure, <10 s,
                            # >25% stream-vs-format skew) must hit the same
                            # fail-open handler as every other pre-flight
                            # refusal - encode the original rip, no GPU burn.
                            raise UpscaleError(str(exc)) from exc
                        derived_rate = rip_total_frames / audio_probe.seconds
                        try:
                            rip_fps, rate_snapped = snap_to_canonical_rate(
                                derived_rate)
                        except FfprobeError as exc:
                            raise UpscaleError(
                                f"derived stamp rate {derived_rate:.4f} fps "
                                f"(from {rip_total_frames} decoded frames / "
                                f"{audio_probe.seconds:.1f} s of "
                                + ("container" if audio_probe.from_format
                                   else "audio stream")
                                + " duration) does not match a canonical "
                                "rate; refusing to guess the mezzanine "
                                f"clock ({exc})"
                            ) from exc
                        # v0.2.35 C.3: cross-provenance guard. The snap's
                        # denominator (the audio STREAM extent) and the
                        # clock gate's reference are the same figure, so
                        # a mis-measured extent co-moves through both -
                        # a true 23.976 film whose stream extent under-
                        # reports by ~11 s derives 24.02, snaps to 24,
                        # and still passes the clock gate against the same
                        # short figure. The container FORMAT duration is
                        # an independent second provenance: derive the rate
                        # from it too and compare the SNAP TOKENS. Same
                        # token -> proceed (both truths agree on the rate
        # class). Different tokens -> measurable disagreement between
                        # two truths; refuse before GPU work (fail open).
                        # One side unsnappable -> noise, not signal:
                        # proceed on the audio-stream derivation of record
                        # with a warning. Zero new probes: the 25%-skew
                        # sanity already resolved the format figure; it
                        # rides on AudioDurationProbe.format_seconds.
                        format_seconds = getattr(
                            audio_probe, "format_seconds", None)
                        cross_rate_warning = None
                        format_rate = None
                        if format_seconds:
                            format_rate = rip_total_frames / format_seconds
                            try:
                                format_token, _format_snapped = (
                                    snap_to_canonical_rate(format_rate))
                                if format_token != rip_fps:
                                    raise UpscaleError(
                                        f"stamp-rate provenance split: the "
                                        f"audio-stream extent derives "
                                        f"{derived_rate:.4f} fps "
                                        f"(snaps {rip_fps}) but the "
                                        f"container format duration "
                                        f"({format_seconds:.1f} s) derives "
                                        f"{format_rate:.4f} fps (snaps "
                                        f"{format_token}) for "
                                        f"{rip_total_frames} frames; "
                                        "refusing to pick between two "
                                        "clock classes - encoding the "
                                        "original rip"
                                    )
                            except FfprobeError:
                                # One-sided noise (odd attachment streams
                                # pollute format durations): warn and
                                # proceed on the audio-stream derivation.
                                cross_rate_warning = (
                                    f"{job.output_name}: stamp-rate "
                                    f"cross-check unsnappable - derived "
                                    f"{derived_rate:.4f} fps from the "
                                    f"audio stream ({audio_probe.seconds:.1f} s) "
                                    f"but {format_rate:.4f} fps from the "
                                    f"container duration ({format_seconds:.1f} s) "
                                    "matches no canonical rate; proceeded "
                                    "on the audio-stream figure"
                                )
                        if cross_rate_warning:
                            result.warnings.append(cross_rate_warning)
                        mezz_path, _mezz_stage_frames = run_dvd_upscale(
                            UpscalePlan(
                                input_path=job.input_path,
                                output_path=job.upscale_mezzanine_path
                                or _upscale_mezzanine_path(rip_dir, job.tid),
                                workdir=os.path.join(
                                    rip_dir, ".upscale", f"title_{job.tid:02d}_work"),
                                mode=job.upscale_mode,
                                duration_seconds=title.duration_seconds,
                                deinterlace=deinterlace,
                                fps=rip_fps,
                                total_frames=rip_total_frames,
                                frame_count_from_metadata=frame_probe.from_metadata,
                                source_width=video_stream.width if video_stream else 0,
                                source_height=video_stream.height if video_stream else 0,
                                display_aspect_ratio=(
                                    video_stream.display_aspect_ratio
                                    if video_stream else ""
                                ),
                                sample_aspect_ratio=(
                                    video_stream.sample_aspect_ratio
                                    if video_stream else ""
                                ),
                            ),
                            on_progress=lambda frac, eta, detail=None: on_progress(
                                ("upscale", job.tid, frac, eta, detail)
                            ) if on_progress else None,
                            cancel=cancel,
                        )
                        video_input_path = None
                        # v0.2.33: the mezzanine path is on disk now
                        # (run_dvd_upscale returned); retention (§4) needs
                        # it even when the gate below rejects the file.
                        job.upscale_mezzanine_path = mezz_path
                        # v0.2.35 §2.1/§2.3 scope 2: the two gates emit a
                        # named finalize step each, and a 15 s heartbeat
                        # wraps BOTH calls - verify_mezzanine_frames is a
                        # full -count_frames FFV1 decode (20-60 min on a
                        # film) that emits no progress of its own; the
                        # 2026-10-19 run sat silent ~65 min and read as
                        # five false STALLs. The step tuples reuse the
                        # SAME on_progress lambda the stage used (chunk N
                        # of N + the finalize step name); the heartbeat
                        # pings through it too (single-writer discipline:
                        # no on_stage, no second UI writer).
                        gate_heartbeat = ProgressHeartbeat(
                            on_progress if on_progress else None,
                            interval=_FINALIZE_HEARTBEAT_SECONDS)
                        try:
                            if on_progress:
                                on_progress((
                                    "upscale", job.tid, _FINALIZE_FRAC_GATE,
                                    None,
                                    {"chunk": _mezz_stage_frames or 1,
                                     "chunks": _mezz_stage_frames or 1,
                                     "step": FINALIZE_STEP_VERIFY_FRAMES},
                                ))
                            gate_heartbeat.start(
                                _FINALIZE_FRAC_GATE,
                                FINALIZE_STEP_VERIFY_FRAMES,
                                int(_mezz_stage_frames or 1) - 1,
                                int(_mezz_stage_frames or 1),
                            )
                            # v0.2.33 gate (design §3): exact integer frame-count
                            # equality between mezzanine and rip - no tolerance
                            # window. Under frame-exact chunking the mezzanine
                            # contains the rip's frames by construction, so any
                            # deviation is a real defect and must fail open to
                            # the original rip with an honest warning. The
                            # mezzanine count is probed fresh (the stage's own
                            # return only echoes the plan total; the gate must
                            # measure the file on disk).
                            verify_mezzanine_frames(
                                mezz_path, rip_total_frames,
                                probe_frames=probe_frame_count,
                            )
                            if on_progress:
                                on_progress((
                                    "upscale", job.tid, _FINALIZE_FRAC_GATE,
                                    None,
                                    {"chunk": _mezz_stage_frames or 1,
                                     "chunks": _mezz_stage_frames or 1,
                                     "step": FINALIZE_STEP_VERIFY_CLOCK},
                                ))
                            gate_heartbeat.stop()
                            gate_heartbeat = ProgressHeartbeat(
                                on_progress if on_progress else None,
                                interval=_FINALIZE_HEARTBEAT_SECONDS)
                            gate_heartbeat.start(
                                _FINALIZE_FRAC_GATE,
                                FINALIZE_STEP_VERIFY_CLOCK,
                                int(_mezz_stage_frames or 1) - 1,
                                int(_mezz_stage_frames or 1),
                            )
                            # v0.2.34 clock gate (design §3), composed on the
                            # orthogonal axis: the frames gate proved the COUNT;
                            # this proves the CLOCK - the mezzanine's container
                            # duration must match the selected audio stream's
                            # measured extent within max(2 s, 0.1%). Same try,
                            # same fail-open handler; the diagnostic (§4) gains
                            # the clock fields via the probe_ctx locals below.
                            verify_mezzanine_clock(
                                mezz_path, audio_probe.seconds,
                                probe_duration=probe_duration_seconds,
                            )
                            gate_heartbeat.stop()
                            gate_heartbeat = None
                            # v0.2.35 C.4 (Karen's parity ask): the SUCCESS path
                            # records the provenance figures that justified the
                            # stamp - same figures the rejection diagnostic
                            # carries, in a run reason (no diagnostic JSON on
                            # success; the retention artifact family stays
                            # rejection-only per v0.2.33 §4).
                            reason_bits = [
                                f"{job.output_name}: upscale stamp {rip_fps} "
                                f"(derived {derived_rate:.4f} from "
                                f"{rip_total_frames} frames / "
                                f"{audio_probe.seconds:.1f} s "
                                + ("container" if audio_probe.from_format
                                   else "audio stream")
                            ]
                            if format_seconds:
                                reason_bits.append(
                                    f"; format-duration cross-check "
                                    f"{format_rate:.4f} agrees")
                            reason_bits.append(")")
                            result.reasons.append("".join(reason_bits))
                        finally:
                            if gate_heartbeat is not None:
                                gate_heartbeat.stop()
                        video_input_path = mezz_path
                    except UpscaleError as exc:
                        stage(
                            f"AI upscale failed for {job.output_name}; "
                            "encoding original DVD rip ...")
                        result.warnings.append(
                            f"{job.output_name}: DVD AI upscale failed ({exc}); "
                            "encoded the original DVD rip instead."
                        )
                        # v0.2.33 retention (design §4): a rejected mezzanine
                        # is NEVER deleted - it is renamed next to the
                        # original with a written diagnostic (expected vs
                        # actual counts, fps probe values, per-chunk plan)
                        # so drift is measurable from artifacts, not logs.
                        retained = _retain_rejected_mezzanine(
                            job, rip_dir, locals(), str(exc)
                        )
                        if retained:
                            result.warnings.append(
                                f"{job.output_name}: rejected mezzanine "
                                f"retained at {retained[0]}; diagnostic at "
                                f"{retained[1]}"
                            )
                        video_input_path = None

                stage(f"Encoding {job.output_name} ({result.encoder}) ...")
                job.encode_args = build_encode_args(
                    EncodePlan(
                        input_path=job.input_path,
                        video_input_path=video_input_path,
                        output_path=os.path.join(outdir, job.output_name),
                        duration_seconds=title.duration_seconds,
                        media_type=disc.media_type or "bluray",
                        encoder=result.encoder,
                        audio_sid=nth_ordinal(streams, audio),
                        deinterlace=deinterlace,
                        target_gb_per_hour=_resolve_gbph(
                            target_gb_per_hour, disc.media_type),
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
                            codec_name=sub.codec_name,  # v0.2.16: VobSub -> .mkv
                        )
                        job.srt_path = ocr.srt_path
                        # v0.2.16: embed the OCR'd subtitles INSIDE the MP4
                        # as a soft mov_text track (sidecar .srt stays on
                        # disk for editing). The mux temp-then-replaces, so
                        # a failure leaves the finished encode bit-identical.
                        mux_srt_into_mp4(
                            video_out, ocr.srt_path,
                            language=mux_language(sub.lang),
                        )
                        result.warnings.extend(f"{job.output_name}: {n}" for n in ocr.notes)
                    except SubtitleOcrError as exc:
                        # job.srt_path is set before the mux runs, so it is
                        # the exact discriminator of which stage failed
                        # (OCR vs embed) - the History view names it right.
                        if job.srt_path:
                            result.warnings.append(
                                f"{job.output_name}: subtitle embed failed "
                                f"({exc}); MP4 ships without embedded "
                                "subtitles; sidecar SRT kept."
                            )
                        else:
                            result.warnings.append(
                                f"{job.output_name}: subtitle OCR failed ({exc}); "
                                "MP4 was produced without subtitles."
                            )

                stage(f"Done: {job.output_name}")
            except EncodeCancelled:
                raise
            except Exception as exc:
                job.error = str(exc)
                result.errors.append(f"{job.output_name}: {exc}")
                stage(f"Failed: {job.output_name} ({exc}); continuing queue")
                continue

        # Full success + default workdir: the .rip intermediates are
        # reproducible junk, so reclaim the disk. A caller-supplied
        # workdir is never touched (it may be shared/persistent).
        # v0.2.33: a run that retained a rejected mezzanine (§4) is NOT
        # junk - the operator must decide - so retention suppresses
        # the reclaim for this run.
        retained_mezzanines = any(
            ".rejected-" in w and "retained at" in w
            for w in result.warnings
        )
        if (
            rip_dir == os.path.join(outdir, ".rip")
            and all(job.completed for job in result.jobs)
            and not retained_mezzanines
        ):
            shutil.rmtree(rip_dir, ignore_errors=True)
    except Exception as exc:
        # Cancel: keep the intermediates for debugging and surface the
        # partial result (with this note) on the exception.
        result.warnings.append(f"Kept intermediates in {rip_dir} for debugging")
        if getattr(exc, "result", None) is None:
            exc.result = result
        raise

    # Per-job failures (partial success): same retention note, attached
    # to the returned result instead of an exception.
    if result.errors:
        result.warnings.append(f"Kept intermediates in {rip_dir} for debugging")
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