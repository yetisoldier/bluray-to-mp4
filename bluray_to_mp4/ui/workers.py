"""Worker threads: drive the pipeline without freezing the UI.

ScanWorker lists drives and scans discs; RunWorker executes the full
pipeline, forwarding stage/progress messages to the UI thread via Qt
signals. The monitor store is updated from here too, so CLI runs,
UI runs, and remote /status all share one picture.
"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from ..config import Settings
from ..makemkv import find_drives, scan_disc
from ..models import Disc
from ..monitor import JobRecord, state as monitor_state
from ..pipeline import run_pipeline
from ..series import detect_series


class ScanWorker(QThread):
    """Scan one disc (or list drives) and emit the result."""

    scanned = Signal(object)     # Disc
    drives_listed = Signal(list)
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
                self.drives_listed.emit(find_drives())
            else:
                self.scanned.emit(
                    scan_disc(self.index, minlength=self.minlength)
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
                    f"{kind.capitalize()}: {job_name}", tid, frac, eta)
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
                tmdb_key=(self.settings.tmdb_key or "").strip(),
                cancel=lambda: self._cancelled,
                on_stage=on_stage,
                on_progress=on_progress,
            )

            for job in result.jobs:
                if job.completed:
                    monitor_state.job_done(JobRecord(
                        name=job.output_name,
                        kind="series" if result.is_series else "movie",
                        duration_seconds=0,
                        encoder=result.encoder,
                        srt=bool(job.srt_path),
                        warnings=[],
                    ))
            monitor_state.finish()
            self.done.emit(result)
        except Exception as exc:
            monitor_state.fail(str(exc))
            self.failed.emit(str(exc))
            traceback.print_exc()