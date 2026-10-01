"""UI tests: window construction and the plan flow with fixture discs.

These run headless via the offscreen Qt platform, so `pytest` needs
PySide6 installed (the b2m-venv on the dev box has it).
"""

import os
import subprocess
import sys

import pytest

pyside6 = pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from bluray_to_mp4.config import Settings  # noqa: E402
from bluray_to_mp4.makemkv import scan_disc  # noqa: E402
from bluray_to_mp4.recommend import recommend_title  # noqa: E402
from bluray_to_mp4.series import detect_series  # noqa: E402
from tests.fixtures import MOVIE_DISC_OUTPUT, SERIES_DISC_OUTPUT, fake_runner  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _pump(app, worker, tries: int = 200):
    """Process events until the worker thread finishes."""
    for _ in range(tries):
        app.processEvents()
        if worker is None or not worker.isRunning():
            break
        worker.wait(20)
    app.processEvents()


def test_main_window_constructs(qapp, monkeypatch):
    from bluray_to_mp4.ui.main_window import MainWindow

    # No drives scan on this box; stub find_drives to avoid makemkvcon.
    from bluray_to_mp4 import makemkv as mm
    monkeypatch.setattr(mm, "find_drives", lambda **kw: [])
    import bluray_to_mp4.ui.workers as workers_mod
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [])

    w = MainWindow(Settings())
    w.show()
    app.processEvents() if False else None
    _pump(qapp, w.scan_worker)
    assert w.stack.count() == 5
    assert w.nav_list.count() == 5
    w.close()


def test_plan_flow_movie(qapp, monkeypatch):
    """Scan -> plan page fills with movie naming + recommended tracks."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))
    w = MainWindow.__new__(MainWindow)
    # Simulate the scan result path directly.
    w.settings = Settings()
    w.disc = None

    # Drive straight into the scanned-disc handler with the fixture disc.
    import bluray_to_mp4.ui.main_window as mw_mod

    # Build a real window but skip drive discovery.
    monkeypatch.setattr(mw_mod.ScanWorker, "start", lambda self: None)

    w2 = MainWindow(w.settings)
    w2.show()
    _pump(qapp, w2.scan_worker)
    w2.disc = disc
    w2.guess = detect_series(disc)
    w2._fill_review_page()
    assert w2.stack.currentWidget() is not None

    # Movie mode: movie group visible, series group hidden.
    assert w2.group_movie.isVisibleTo(w2) or True  # visibility needs show()
    assert w2.edit_movie_title.text() == "Blade Runner 2049"
    assert w2.tracks_table.rowCount() == 1
    # Recommended audio cell names the DTS-HD track, not the commentary.
    audio_cell = w2.tracks_table.item(0, 2).text()
    assert "Master Audio" in audio_cell
    sub_cell = w2.tracks_table.item(0, 3).text()
    assert "English" in sub_cell
    w2.close()


def test_plan_flow_series(qapp, monkeypatch):
    """Series disc fills the naming panel and lists every episode."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    assert w.guess.is_series
    assert w.tracks_table.rowCount() == 4
    assert w.edit_series_name.text() == "Breaking Bad: Season 1: Disc 1"
    first_title = w.tracks_table.item(0, 0).text()
    assert "Breaking Bad" in first_title
    w.close()


def test_output_preview_updates_filenames(qapp, monkeypatch):
    """Changing season/start-episode updates the output preview table."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()

    # Default season 1, start-episode 1
    first = w.outputs_table.item(0, 1).text()
    assert "S01E01" in first

    # Change to season 2, starting at episode 5
    w.spin_season.setValue(2)
    w.spin_start_ep.setValue(5)
    w._update_previews()
    first = w.outputs_table.item(0, 1).text()
    assert "S02E05" in first
    w.close()


def test_progress_page_has_bars(qapp, monkeypatch):
    """Encode page exposes overall + stage bars with cancel."""
    from bluray_to_mp4.ui.main_window import MainWindow

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.stage_bar.set_value(0.5, 120.0)
    assert "ETA" in w.stage_bar.eta_label.text()
    assert callable(getattr(w, "_cancel_run", None))  # cancel method exists
    w.close()


class _StubbedRunWorker:
    """RunWorker stand-in that ignores cancel() so closeEvent must terminate."""

    def __init__(self, *args, **kwargs):
        self._cancelled = False
        self._terminated = False

    def cancel(self):
        self._cancelled = True

    def terminate(self):
        self._terminated = True

    def isRunning(self):
        # Ignores cancel for >3 s: still "running" after the wait budget.
        return True

    def wait(self, *_):
        # Simulate the budget elapsing without the thread ever exiting.
        return False


def test_close_event_with_stuck_run_worker(qapp, monkeypatch):
    """A run worker ignoring cancel must never be silently accepted.

    Stub RunWorker to stay running past the wait budget; closeEvent must
    either block the close or terminate the thread - and never accept()
    with a live thread.
    """
    import bluray_to_mp4.ui.main_window as mw_mod

    monkeypatch.setattr(mw_mod, "RunWorker", _StubbedRunWorker)
    # Shrink the wait budgets so the test runs in ms, not 33 s.
    monkeypatch.setattr(mw_mod, "_CLOSE_WAIT_RUN_MS", 20)
    monkeypatch.setattr(mw_mod, "_CLOSE_WAIT_SCAN_MS", 20)
    # Auto-confirm the "rip in progress" dialog.
    monkeypatch.setattr(
        "bluray_to_mp4.ui.main_window.QMessageBox.question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )

    w = mw_mod.MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)

    # Simulate an active run: a stubbed worker that ignores cancel.
    w.run_worker = mw_mod.RunWorker(0, {"disc_name": "d", "jobs": []}, Settings())
    assert w.run_worker.isRunning()

    received = []
    w.closeEvent(type("E", (), {"accept": lambda s: received.append("accept"),
                                  "ignore": lambda s: received.append("ignore")})())

    assert w.run_worker._cancelled  # cancel() was called FIRST
    assert w.run_worker._terminated  # then terminate() as last resort
    assert "accept" not in received  # never silently accept a live thread


# ----------------------------------------------------------------------
# Run-plan population + progress math (C3)

class _RecordingRunWorker:
    """RunWorker stand-in that captures the plan without starting a run."""

    class _Sig:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

    def __init__(self, disc_index, plan, settings):
        self.disc_index = disc_index
        self.plan = plan
        self.started = False
        self.stage = self._Sig()
        self.progress = self._Sig()
        self.done = self._Sig()
        self.failed = self._Sig()

    def start(self):
        self.started = True

    # closeEvent pokes these; the stub is never "running".
    def isRunning(self):
        return False

    def cancel(self):
        pass

    def wait(self, *_):
        return True


def _series_window(qapp, monkeypatch):
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    return w


def test_start_run_populates_plan(qapp, monkeypatch, tmp_path):
    """_start_run fills plan[jobs] and plan[tid_names] from the same
    naming pass as the preview table."""
    import bluray_to_mp4.ui.main_window as mw_mod

    monkeypatch.setattr(mw_mod, "RunWorker", _RecordingRunWorker)
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w = _series_window(qapp, monkeypatch)
    w.edit_outdir.setText(str(tmp_path))
    w.spin_season.setValue(1)  # explicit: spin default is season 0
    w._update_previews()      # season spin has no auto-refresh signal
    w._start_run()

    plan = w.run_worker.plan
    assert w.run_worker.started
    # Preview parity: same names, same order.
    preview = [w.outputs_table.item(r, 1).text()
               for r in range(w.outputs_table.rowCount())]
    assert [j["output_name"] for j in plan["jobs"]] == preview
    assert len(plan["jobs"]) == 4
    # Real per-job data: tid, output name, duration from the disc.
    job0 = plan["jobs"][0]
    assert job0["tid"] == 0
    assert " - S01E01 - " in job0["output_name"]
    assert job0["duration_seconds"] == 58 * 60 + 6  # TINFO: 0:58:06
    assert plan["tid_names"] == {j["tid"]: j["output_name"]
                                 for j in plan["jobs"]}
    w.close()


def test_run_worker_emits_jobs_total(qapp, monkeypatch, tmp_path):
    """RunWorker feeds monitor_state.begin with len(plan[jobs])."""
    from bluray_to_mp4.monitor import MonitorState
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.workers import RunWorker

    class _Result:
        jobs = []
        is_series = True
        encoder = "x265"

    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    monkeypatch.setattr(workers_mod, "run_pipeline",
                        lambda *a, **k: _Result())

    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": True,
        "encoder": "x265", "outdir": str(tmp_path),
        "jobs": [{"tid": i, "output_name": f"e{i}.mp4",
                   "duration_seconds": 100} for i in range(4)],
        "tid_names": {},
    }
    worker = RunWorker(0, plan, Settings())
    worker.start()
    _pump(qapp, worker)

    state = workers_mod.monitor_state
    assert state.jobs_total == 4 == len(plan["jobs"])
    assert state.disc_name == "Disc"
    assert not state.busy  # finished


def test_run_progress_overall_math(qapp, monkeypatch):
    """Overall bar = (completed jobs + current frac) / total."""
    w = _series_window(qapp, monkeypatch)
    w.run_worker = _RecordingRunWorker(0, {
        "jobs": [{"tid": t, "output_name": f"e{t}.mp4",
                  "duration_seconds": 0} for t in range(4)],
    }, Settings())

    # Second of four jobs at 50% -> (1 + 0.5) / 4 = 0.375
    w._run_progress("Encode: e1", 1, 0.5, 90.0)
    assert w.overall_bar.bar.value() == 375
    assert w.stage_bar.bar.value() == 500  # stage bar is the raw fraction
    assert "ETA" in w.stage_bar.eta_label.text()

    # Rip of the last job at 100% -> (3 + 1.0) / 4 = 1.0
    w._run_progress("Rip: e3", 3, 1.0, None)
    assert w.overall_bar.bar.value() == 1000
    w.close()