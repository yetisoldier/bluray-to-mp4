"""End-to-end pipeline: scan -> detect -> recommend -> rip -> encode.

Movie mode rips the main feature; series mode rips every detected episode
and names them Plex/Jellyfin style. Encoding uses NVENC when available,
x265 otherwise. A selected subtitle stream is OCR'd to a sidecar SRT
(shipped in v0.2.2); dry runs report the reserved SRT path instead of
running the OCR.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from .binaries import resolve_ffmpeg
from .encode import EncodePlan, build_encode_args
from .ffprobe import (
    ProbedStream,
    first_video_width,
    is_interlaced,
    nth_of_type,
    probe_duration_seconds,
    probe_streams,
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
    stream_overrides: dict[int, dict[str, int | None]] | None = None,  # v0.2.15
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
    _expected = {os.path.normcase(job.input_path) for job in result.jobs}
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
        if rip_dir == os.path.join(outdir, ".rip") and all(
            job.completed for job in result.jobs
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