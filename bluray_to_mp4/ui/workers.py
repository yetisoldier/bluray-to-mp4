"""Worker threads: drive the pipeline without freezing the UI.

ScanWorker lists drives and scans discs; RunWorker executes the full
pipeline, forwarding stage/progress messages to the UI thread via Qt
signals. The monitor store is updated from here too, so CLI runs,
UI runs, and remote /status all share one picture.
"""

from __future__ import annotations

import os
import traceback

from PySide6.QtCore import QThread, Signal

from ..config import Settings
from ..encode import BLURAY_GB_PER_HOUR, DVD_GB_PER_HOUR
from ..makemkv import find_drives, make_runner, scan_disc
from ..models import Disc
from ..monitor import JobRecord, state as monitor_state
from ..pipeline import run_pipeline
from ..series import detect_series


def _job_warnings(result, job) -> list[str]:
    """Warnings that belong to this completed output.

    OCR/mux warnings are job-prefixed by the pipeline. If subtitles were
    requested but no sidecar exists, synthesize a visible warning so both
    live status and History stay honest after a killed OCR child.
    """
    if job.srt_path:
        return []
    warnings = [
        w for w in getattr(result, "warnings", [])
        if w.startswith(job.output_name)
    ]
    return warnings or ["subtitle missing (unknown reason)"]


class ScanWorker(QThread):
    """Scan one disc (or list drives) and emit the result."""

    scanned = Signal(object)     # Disc
    drives_listed = Signal(list)
    progress = Signal(str)       # human-readable scan progress line
    failed = Signal(str)

    def __init__(self, mode: str = "drives", index: int = 0,
                 minlength: int = 120):
        super().__init__()
        self.mode = mode
        self.index = index
        self.minlength = minlength

    def run(self) -> None:  # QThread override
        try:
            if self.mode == "drives":
                # QW-3 (Bob audit): an orphan makemkvcon holding the drive
                # used to hang this call forever - "Looking for drives ..."
                # spun eternally and the v0.2.7 "timed out" guidance in
                # _scan_failed could never fire. The 60 s runner timeout
                # (with make_runner's kill-on-expiry) turns the hang into
                # the reachable orphan-hint path. Scan/rip stay timeout=None.
                self.drives_listed.emit(
                    find_drives(runner=make_runner(timeout=60.0)))
            else:
                progress = self.progress

                def _on_line(kind: str, text: str) -> None:
                    progress.emit(text)

                self.scanned.emit(
                    scan_disc(self.index, minlength=self.minlength,
                              on_line=_on_line)
                )
        except Exception as exc:
            self.failed.emit(str(exc))
            traceback.print_exc()


class FetchWorker(QThread):
    """Run a single callable off the UI thread; emit its result."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:  # QThread override
        try:
            self.done.emit(self._fn())
        except Exception as exc:
            self.failed.emit(str(exc))


class RunWorker(QThread):
    """Execute the whole pipeline for the confirmed plan."""

    stage = Signal(str)
    # (stage_name, tid, fraction 0..1, eta_seconds-or-None) for the current job
    progress = Signal(str, int, float, object)
    done = Signal(object)          # PipelineResult
    failed = Signal(str)

    def __init__(self, disc_index: int, plan: dict, settings: Settings):
        super().__init__()
        self.disc_index = disc_index
        self.plan = plan
        self.settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        """Request a graceful stop (current step finishes or ffmpeg halts)."""
        self._cancelled = True

    def run(self) -> None:  # QThread override
        try:
            monitor_state.begin(
                self.plan.get("disc_name", "unknown"),
                self.plan.get("media_type", "unknown"),
                series_mode=bool(self.plan.get("is_series")),
                encoder=self.plan.get("encoder", "auto"),
                jobs_total=len(self.plan.get("jobs", [])),
            )
            self.stage.emit("Starting ...")

            def on_stage(msg: str) -> None:
                self.stage.emit(msg)
                monitor_state.update(stage=msg)

            def on_progress(update) -> None:
                kind, tid, frac = update[:3]
                job_name = self.plan.get("tid_names", {}).get(tid, f"title {tid}")
                eta = None
                if len(update) > 3:  # encode progress carries an ETA
                    eta = update[3]
                self.progress.emit(
                    f"Ripping: {job_name}" if kind == "rip" else f"{kind.capitalize()}: {job_name}", tid, frac, eta)
                monitor_state.update(progress=frac, current_job=job_name,
                                     eta_seconds=eta)

            result = run_pipeline(
                self.disc_index,
                self.plan["outdir"],
                encoder=self.plan.get("encoder", "auto"),
                series_name=self.plan.get("series_name"),
                season=self.plan.get("season", 1),
                start_episode=self.plan.get("start_episode", 1),
                episode_titles=self.plan.get("episode_titles"),
                year=self.plan.get("year"),
                title_overrides=self.plan.get("title_overrides"),
                stream_overrides=self.plan.get("stream_overrides"),  # v0.2.15
                target_gb_per_hour=self.plan.get("target_gb_per_hour"),  # v0.2.18
                tmdb_key=(self.settings.tmdb_key or "").strip(),
                want_subtitles=bool(self.plan.get("want_subtitles", True)),
                cancel=lambda: self._cancelled,
                on_stage=on_stage,
                on_progress=on_progress,
            )

            # v0.2.18: the run's size target for History's Target GB/hr
            # column (target vs actual, the pair Eric checks by hand);
            # an absent key (CLI / pre-slider plans) resolves to the
            # disc type's built-in default so the number is always
            # meaningful. 0.0 is reserved for pre-v0.2.18 records.
            target_gbph = self.plan.get("target_gb_per_hour")
            if target_gbph is None:
                target_gbph = (
                    DVD_GB_PER_HOUR
                    if self.plan.get("media_type") == "dvd"
                    else BLURAY_GB_PER_HOUR
                )
            monitor_warnings: list[str] = []
            for job in result.jobs:
                if job.completed:
                    warnings = _job_warnings(result, job)
                    monitor_warnings.extend(warnings)
                    # QW-6 (Bob audit): the output is on disk now - record
                    # its size so History can show size + achieved GB/hour
                    # (the number Eric validates by hand every run).
                    try:
                        size_bytes = os.path.getsize(os.path.join(
                            self.plan.get("outdir", ""), job.output_name))
                    except (OSError, ValueError):
                        size_bytes = 0
                    monitor_state.job_done(JobRecord(
                        name=job.output_name,
                        kind="series" if result.is_series else "movie",
                        duration_seconds=job.duration_seconds,
                        encoder=result.encoder,
                        srt=bool(job.srt_path),
                        size_bytes=size_bytes,
                        target_gb_per_hour=float(target_gbph),
                        warnings=warnings,
                    ))
            for warning in monitor_warnings:
                monitor_state.update(warning=warning)
            # C6: per-job failures surface as a partial result - the
            # completed outputs keep their records, and the monitor
            # records which outputs failed (never a generic success).
            errors = getattr(result, "errors", [])
            if errors:
                # Include each failure's REASON, not just file names
                # (validation run 4: the remote monitor said which output
                # failed but hid why, so diagnosis needed a second run).
                monitor_state.fail("; ".join(str(e) for e in errors[:3]))
            else:
                monitor_state.finish()
            self.done.emit(result)
        except Exception as exc:
            monitor_state.fail(str(exc))
            self.failed.emit(str(exc))
            traceback.print_exc()