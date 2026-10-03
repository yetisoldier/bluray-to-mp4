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

from PySide6.QtWidgets import QApplication, QMessageBox, QComboBox  # noqa: E402

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
    _pump(qapp, w.scan_worker)
    assert w.stack.count() == 5
    assert w.nav_list.count() == 5
    w.close()




def test_setup_continue_always_reaches_source(qapp, monkeypatch):
    """Regression (2026-10-02): Continue was a silent no-op when the nav
    row already pointed at Source (stack stayed on Setup). It must
    always move the stack to the Source page."""
    from bluray_to_mp4.ui.main_window import MainWindow
    from bluray_to_mp4 import makemkv as mm
    monkeypatch.setattr(mm, "find_drives", lambda **kw: [])
    import bluray_to_mp4.ui.workers as workers_mod
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [])

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    # Simulate the worst case: sidebar row already 1 while the stack
    # somehow sits on Setup (the exact pre-fix mismatch state).
    w.stack.setCurrentIndex(0)
    w.nav_list.blockSignals(True)
    w.nav_list.setCurrentRow(1)
    w.nav_list.blockSignals(False)
    assert w.stack.currentIndex() == 0
    w.btn_setup_continue.click()
    assert w.stack.currentIndex() == 1, "Continue must land on Source"
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
    # Movie naming group content check replaces a vacuous visibility check:
    # group_movie is not visible-only-on-show() in a stack; the title field
    # carries the disc label is the meaningful assertion.
    assert w2.edit_movie_title.text() == "Blade Runner 2049"
    assert w2.tracks_table.rowCount() == 1
    # v0.2.15: the recommendation cells are now combo cell widgets; the
    # default selection still reads like the old static text did.
    audio_combo = w2.tracks_table.cellWidget(0, 2)
    sub_combo = w2.tracks_table.cellWidget(0, 3)
    # Recommended audio names the DTS-HD track, not the commentary.
    assert "Master Audio" in audio_combo.currentText()
    assert "English" in sub_combo.currentText()
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
    # D2: season spin starts at 1 (fixed); no manual workaround anymore.
    # D3: naming edits auto-refresh previews; explicit call not needed.
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


def test_run_worker_partial_failure_names_outputs(qapp, monkeypatch, tmp_path):
    """C6: partial result -> job_done for completed jobs, monitor fail
    message naming the failed output, done signal still fires."""
    from bluray_to_mp4.monitor import MonitorState
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.workers import RunWorker

    class _Job:
        def __init__(self, name, completed, error=None, duration_seconds=100):
            self.output_name = name
            self.completed = completed
            self.srt_path = None
            self.error = error
            self.duration_seconds = duration_seconds

    class _Result:
        is_series = True
        encoder = "x265"
        jobs = [_Job("e1.mp4", True),
                _Job("e2.mp4", False, "rip failed"),
                _Job("e3.mp4", True)]
        errors = ["e2.mp4: rip failed"]

    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    monkeypatch.setattr(workers_mod, "run_pipeline", lambda *a, **k: _Result())

    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": True,
        "encoder": "x265", "outdir": str(tmp_path),
        "jobs": [{"tid": i, "output_name": f"e{i}.mp4",
                   "duration_seconds": 100} for i in range(3)],
        "tid_names": {},
    }
    done_results = []
    worker = RunWorker(0, plan, Settings())
    worker.done.connect(lambda r: done_results.append(r))
    worker.start()
    _pump(qapp, worker)

    state = workers_mod.monitor_state
    assert state.jobs_done == 2            # records for e1, e3 only
    assert not state.busy                  # fail() ended the run
    assert state.last_error and "e2.mp4" in state.last_error
    assert done_results, "done must still fire with the partial result"


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


# ----------------------------------------------------------------------
# Scan addressing (C7): table row != MakeMKV disc index once
# status-256 drives are filtered out of the list.

def test_scan_selected_uses_drive_index_not_row(qapp, monkeypatch):
    """A filtered drive list [0, 2]: scanning row 1 must address disc 2."""
    from bluray_to_mp4.makemkv import DriveEntry
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.main_window import MainWindow

    # find_drives output with a gap: status-256 slot at index 1 was
    # filtered out, so row 1 is DriveEntry(index=2).
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [
        DriveEntry(0, 2, "bluray", "BD-RE Pioneer", "MOVIE", "D:"),
        DriveEntry(2, 2, "bluray", "BD-RE ASUS", "BLADE2049", "E:"),
    ])

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    assert w.drives_table.rowCount() == 2
    assert [d.index for d in w._drives] == [0, 2]

    launched = []

    class _RecordingScanWorker:
        def __init__(self, mode="drives", index=0, minlength=120):
            self.mode = mode
            self.index = index
            self.scanned = type("S", (), {"connect": lambda s, x: None})()
            self.failed = type("S", (), {"connect": lambda s, x: None})()
            self.progress = type("S", (), {"connect": lambda s, x: None})()

        def start(self):
            launched.append(self.index)

    import bluray_to_mp4.ui.main_window as mw_mod
    monkeypatch.setattr(mw_mod, "ScanWorker", _RecordingScanWorker)

    w.drives_table.selectRow(1)  # second row = DriveEntry(index=2)
    w._scan_selected()

    assert launched == [2], "scan must use the DriveEntry index, not the row"

def test_series_defaults_start_at_season_1(qapp, monkeypatch):
    """D2: a fresh series disc must preview S01E01 (spin defaults to 1).
    Karen's probe proved the old default rendered AND ripped S00E01."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    assert w.spin_season.value() == 1
    assert w.spin_start_ep.value() == 1
    first = w.outputs_table.item(0, 1).text()
    assert "S01E01" in first and "S00E00" not in first
    w.close()


def test_naming_edits_refresh_previews(qapp, monkeypatch):
    """D3: season/episode/name/year edits re-render the outputs table
    without any manual refresh call (Karen: no signals were wired)."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    qapp.processEvents()

    first = w.outputs_table.item(0, 1).text()
    assert "S01E01" in first
    w.spin_season.setValue(2)   # signal-driven refresh
    qapp.processEvents()
    first = w.outputs_table.item(0, 1).text()
    assert "S02E01" in first
    w.edit_series_name.setText("Better Name")
    qapp.processEvents()
    first = w.outputs_table.item(0, 1).text()
    assert first.startswith("Better Name - S02E01")
    w.close()


def test_episode_title_edit_overrides_name(qapp, monkeypatch):
    """G13: editing an output-name cell updates _title_overrides; the
    regenerated name keeps the SxxEyy prefix but swaps the title."""
    from bluray_to_mp4.ui.main_window import MainWindow
    from PySide6.QtWidgets import QTableWidgetItem

    disc = scan_disc(0, runner=fake_runner(SERIES_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    qapp.processEvents()

    # Simulate a user editing episode 2's title
    row1 = w.outputs_table.item(1, 1)
    row1.setText("Breaking Bad - S01E02 - Cat in the Bag!.mp4")
    qapp.processEvents()
    # The override is stored against the real tid
    assert 0 not in w._title_overrides  # row 0 untouched
    tids = getattr(w, "_preview_tids", [])
    assert w._title_overrides.get(tids[1]) == "Cat in the Bag!"
    # And the canonical output name regenerated accordingly
    # The series name field auto-filled from the disc label (colons dropped
    # by sanitize); the edited title cell keeps the SxxEyy prefix and swaps
    # only the title portion.
    assert w.outputs_table.item(1, 1).text() == \
        "Breaking Bad Season 1 Disc 1 - S01E02 - Cat in the Bag!.mp4"
    first = w.outputs_table.item(0, 1).text()
    assert "S01E01" in first  # untouched row unchanged
    w.close()


def test_scan_streams_progress_lines(qapp, monkeypatch):
    """Frozen-scan fix: the scan worker emits readable progress lines while
    MakeMKV reads the disc table (MSG texts + per-title reads)."""
    from bluray_to_mp4.ui.workers import ScanWorker

    seen = []
    w = ScanWorker(mode="scan", index=0)
    w.progress.connect(lambda t: seen.append(t))

    lines = [
        'MSG:5004,0,1,"Using direct disc access mode"',
        'TINFO:0,2,2,"3456"',
        'TINFO:0,8,0,"Mamma Mia! Here We Go Again"',
        'TINFO:1,2,2,"1800"',
        'MSG:5006,0,1,"Loaded content"',
    ]
    def fake_run(binary, args):
        return iter(lines)

    import bluray_to_mp4.makemkv as mkv
    monkeypatch.setattr(mkv, "make_runner", lambda timeout=None: fake_run)
    # drive the scan synchronously through the worker's run body
    w.run()
    assert any("direct disc access" in t for t in seen), seen
    assert any("title" in t.lower() for t in seen), seen

def test_activity_dots_cycle(qapp):
    """Validation-day ask: the screen must always show life. The dots
    must visibly move across frames even with no progress data."""
    from bluray_to_mp4.ui.widgets import ActivityDots
    dots = ActivityDots()
    seen = {dots.text()}
    for _ in range(4):
        dots._tick()
        seen.add(dots.text())
    assert len(seen) >= 3  # frames differ; the animation visibly moves


def test_eta_progress_bar_busy_mode(qapp):
    """Busy mode pulses (Qt indeterminate 0..0) with an 'estimating ...'
    label and auto-exits when real progress data arrives."""
    from bluray_to_mp4.ui.widgets import EtaProgressBar
    bar = EtaProgressBar()
    bar.set_busy(True)
    assert bar.eta_label.text() == "estimating ..."
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 0)
    # Real data arrives: busy mode must exit and show an ETA.
    bar.set_value(0.5, 100)
    assert (bar.bar.minimum(), bar.bar.maximum()) == (0, 1000)
    assert bar.eta_label.text() == "ETA 1:40"
    bar.reset()
    assert bar.eta_label.text() == ""

def test_total_remaining_forecast_includes_all_steps(qapp, monkeypatch):
    """Eric's validation-day request: the Remaining area must estimate the
    WHOLE run (all steps, all jobs), swapping in each stage's live ETA as
    it starts. A series plan with three jobs must forecast rips + encodes
    + OCR for later jobs, not just the current stage."""
    w = _series_window(qapp, monkeypatch)
    from bluray_to_mp4.ui.workers import RunWorker
    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": True,
        "encoder": "nvenc", "outdir": "/tmp/x", "want_subtitles": True,
        "jobs": [
            {"tid": 0, "output_name": "e0.mp4", "duration_seconds": 2700},
            {"tid": 1, "output_name": "e1.mp4", "duration_seconds": 2700},
        ],
        "tid_names": {},
    }
    w.run_worker = RunWorker(0, plan, Settings())
    w._current_job_index = 0
    w._stage_now = "encode"
    w._stage_eta = 100.0
    secs = w._forecast_total_seconds()
    # Current job: live 100s encode + OCR ahead (2700/20 = 135).
    # Next job: rip (300 + 67.5) + encode (450) + OCR (135).
    assert secs is not None
    assert abs(secs - (100 + 135 + 300 + 67.5 + 450 + 135)) < 0.01
    w._stage_now = "ocr"
    secs2 = w._forecast_total_seconds()
    assert secs2 < secs  # fewer steps ahead once OCR is the current stage
    w.close()



# ----------------------------------------------------------------------
# v0.2.15 (design Gap 1): Review-page track override combos.

def _movie_window(qapp, monkeypatch):
    """Movie-disc window with the review page filled (shared helper)."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    return w, disc


def test_review_combos_default_to_recommendation(qapp, monkeypatch):
    """Every row's audio/subtitle cell is a QComboBox; the audio combo
    lists ALL scanned audio streams and the subtitle combo leads with
    '(no subtitles)' (userData None); defaults match recommend_title."""
    from PySide6.QtWidgets import QComboBox

    w, disc = _movie_window(qapp, monkeypatch)
    title = disc.title(0)  # main feature, movie mode
    sel = recommend_title(title)
    audio_combo = w.tracks_table.cellWidget(0, 2)
    sub_combo = w.tracks_table.cellWidget(0, 3)
    assert isinstance(audio_combo, QComboBox)
    assert isinstance(sub_combo, QComboBox)
    # One item per scanned audio stream with the sid as item userData;
    # the recommendation is the default selection.
    assert audio_combo.count() == len(title.streams_of("audio")) == 2
    sids = sorted(audio_combo.itemData(i) for i in range(audio_combo.count()))
    assert sids == sorted(s.sid for s in title.streams_of("audio"))
    assert audio_combo.currentData() == sel.audio_sid
    assert "Master Audio" in audio_combo.currentText()  # DTS-HD, not AC3
    assert "[commentary]" in audio_combo.itemText(1)  # commentary flagged
    # Subtitle combo: leading None entry, then every scanned sub stream.
    assert sub_combo.count() == len(title.streams_of("subtitle")) + 1 == 4
    assert sub_combo.itemText(0) == "(no subtitles)"
    assert sub_combo.itemData(0) is None
    assert sub_combo.currentData() == sel.subtitle_sid
    assert "English" in sub_combo.currentText()
    # Fill wrote nothing into the override dict (defaults, no user edit).
    assert w._stream_overrides == {}
    w.close()


def test_review_subtitle_none_override_reaches_plan(qapp, monkeypatch, tmp_path):
    """Picking '(no subtitles)' records {'subtitle_sid': None} for that
    tid in _stream_overrides, and _start_run copies the dict into
    plan['stream_overrides'] (RecordingRunWorker pattern)."""
    import bluray_to_mp4.ui.main_window as mw_mod

    monkeypatch.setattr(mw_mod, "RunWorker", _RecordingRunWorker)
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w, disc = _movie_window(qapp, monkeypatch)
    sub_combo = w.tracks_table.cellWidget(0, 3)
    sub_combo.setCurrentIndex(0)  # '(no subtitles)'
    qapp.processEvents()

    tid = disc.title(0).tid
    # Both keys always present; subtitle None is the explicit decline.
    assert set(w._stream_overrides[tid]) == {"audio_sid", "subtitle_sid"}
    assert w._stream_overrides[tid]["subtitle_sid"] is None
    assert w._stream_overrides[tid]["audio_sid"] == recommend_title(
        disc.title(0)).audio_sid

    w.edit_outdir.setText(str(tmp_path))
    w._start_run()
    plan = w.run_worker.plan
    assert plan["stream_overrides"] == w._stream_overrides
    assert plan["stream_overrides"][tid]["subtitle_sid"] is None
    assert plan["stream_overrides"] is not w._stream_overrides  # copied
    w.close()


def test_review_audio_combo_override_records_sid(qapp, monkeypatch):
    """Changing the audio combo records the picked sid for that tid
    (and nothing else lands in _stream_overrides)."""
    w, disc = _movie_window(qapp, monkeypatch)
    audio_combo = w.tracks_table.cellWidget(0, 2)
    tid = disc.title(0).tid
    # Pick the commentary AC3 (sid 2): the combo must record it.
    idx = audio_combo.findData(2)
    assert idx >= 0, "fixture disc must expose audio sid 2"
    audio_combo.setCurrentIndex(idx)
    qapp.processEvents()
    assert w._stream_overrides[tid]["audio_sid"] == 2
    assert w._stream_overrides[tid]["subtitle_sid"] == recommend_title(
        disc.title(0)).subtitle_sid
    # Only the touched tid is present.
    assert list(w._stream_overrides) == [tid]
    w.close()


def test_unconfirmed_english_caveat_surfaces(qapp, monkeypatch):
    """A subtitle recommendation with the 'English not confirmed'
    caveat gets the warning marker + tooltip, and the muted caveat line
    under the table is visible; without the caveat it stays hidden."""
    w, disc = _movie_window(qapp, monkeypatch)

    # Fixture subs are positively-labeled English: caveat hidden.
    # (isHidden(), not isVisible(): the Review page need not be the
    # active stack page for the label's own state to be correct.)
    assert w.sub_caveat_label.isHidden()
    sub_combo = w.tracks_table.cellWidget(0, 3)
    assert not sub_combo.currentText().startswith("\u26a0")

    # Same disc, subtitle language stripped from the scan: the
    # recommender can no longer confirm English (v0.2.14 caveat) and
    # the Review page must surface it.
    for stream in disc.title(0).streams_of("subtitle"):
        for attr_id in (28, 29):  # lang code + lang name
            stream.attrs.pop(attr_id, None)

    w._fill_review_page()
    assert not w.sub_caveat_label.isHidden()
    sub_combo = w.tracks_table.cellWidget(0, 3)
    assert sub_combo.currentText().startswith("\u26a0")
    assert "not confirmed" in sub_combo.toolTip()
    w.close()


def test_history_table_stays_inside_card(qapp, monkeypatch):
    """QW-1 regression: the History table's parent chain must keep it
    inside its card frame (the old second layout.addWidget re-mothered
    the table out of the card)."""
    from bluray_to_mp4.ui.main_window import MainWindow

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)

    table = w.history_table
    # The table's direct parent is the card frame (not the page).
    parent = table.parentWidget()
    assert parent is not None and parent.objectName() == "card", (
        "history table must sit inside the card frame")
    # The card itself is a direct child of the page layout.
    page = w.stack.widget(4)  # History page
    page_layout = page.layout()
    widgets = [page_layout.itemAt(i).widget()
               for i in range(page_layout.count())]
    assert parent in widgets
    w.close()


# ----------------------------------------------------------------------
# v0.2.16 polish: total-remaining header pill, ScanWorker drive-scan
# timeout (QW-3), and History size + GB/hr columns (QW-6).

def test_total_pill_shows_forecast_during_run_and_clears_on_stop(
        qapp, monkeypatch):
    """The header pill (statusPill style) shows the all-steps forecast
    only while a run is live; idle and every stop path leave it empty."""
    import time

    from bluray_to_mp4 import makemkv as mm
    import bluray_to_mp4.ui.workers as workers_mod
    from bluray_to_mp4.ui.main_window import MainWindow
    from bluray_to_mp4.ui.workers import RunWorker

    monkeypatch.setattr(mm, "find_drives", lambda **kw: [])
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [])
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)

    # Idle: no live run -> the pill must be empty, even after a tick.
    assert w.total_pill.objectName() == "statusPill"
    assert w.total_pill.text() == ""
    w._forecast_total()
    assert w.total_pill.text() == ""

    # Stubbed live run: worker + plan exist, clock started.
    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": True,
        "encoder": "nvenc", "outdir": "/tmp/x", "want_subtitles": True,
        "jobs": [
            {"tid": 0, "output_name": "e0.mp4", "duration_seconds": 2700},
            {"tid": 1, "output_name": "e1.mp4", "duration_seconds": 2700},
        ],
        "tid_names": {},
    }
    w.run_worker = RunWorker(0, plan, Settings())
    w._run_started_at = time.time()
    w._stage_now = "encode"
    w._stage_eta = 100.0
    w._forecast_total()
    assert "Total remaining" in w.total_pill.text()
    # The Encode card's label mirrors the same all-steps total.
    assert "Total remaining" in w.time_remaining.text()

    # Every stop path (done/failed/cancel) passes through here: empty,
    # both the header pill and the Encode card's label.
    w._stop_liveness()
    assert w.total_pill.text() == ""
    assert w.time_remaining.text() == ""
    w.close()


def test_total_label_has_single_writer_no_stage_overwrite(qapp, monkeypatch):
    """Live run 12 (Eric report): the all-steps total on the Encode card
    only flashed briefly every couple of seconds. Cause: two writers
    fought over time_remaining - _run_progress (2+/s: MakeMKV PRGV
    duplicates + ffmpeg -progress lines) stomped the once-a-second
    'Total remaining' from _forecast_total with the per-stage 'Remaining
    ~M:SS'. Single-writer fix: _forecast_total owns time_remaining;
    the stage ETA lives only on stage_bar's eta_label."""
    import time

    from bluray_to_mp4 import makemkv as mm
    import bluray_to_mp4.ui.workers as workers_mod
    from bluray_to_mp4.ui.main_window import MainWindow
    from bluray_to_mp4.ui.workers import RunWorker

    monkeypatch.setattr(mm, "find_drives", lambda **kw: [])
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [])
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)

    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": True,
        "encoder": "nvenc", "outdir": "/tmp/x", "want_subtitles": True,
        "jobs": [
            {"tid": 0, "output_name": "e0.mp4", "duration_seconds": 2700},
            {"tid": 1, "output_name": "e1.mp4", "duration_seconds": 2700},
        ],
        "tid_names": {},
    }
    w.run_worker = RunWorker(0, plan, Settings())
    w._run_started_at = time.time()
    w._stage_now = "encode"
    w._stage_eta = 100.0

    # The 1 s tick (_tick_elapsed) writes the all-steps total on the
    # card label and the header pill - the only writer of both.
    w._forecast_total()
    assert "Total remaining" in w.time_remaining.text()
    assert "Total remaining" in w.total_pill.text()

    # Progress storm: real ETAs, a MakeMKV PRGV duplicate (eta=None), and
    # a sub-second 'Finishing ...' ETA. None may stomp the card label
    # back to stage text - that is exactly the flicker Eric saw.
    for frac, eta in [(0.25, 90.0), (0.25, None), (0.30, 85.0),
                      (0.35, 0.5), (0.40, 60.0), (0.50, 300.0)]:
        w._run_progress("Encode: e0", 0, frac, eta)
        assert "Total remaining" in w.time_remaining.text(), (
            "progress events must never overwrite the all-steps total")
        assert "Remaining ~" not in w.time_remaining.text()
        assert "Finishing" not in w.time_remaining.text()

    # The stage ETA lives on the bar, not on the card: last event was
    # frac 0.50 with a 5:00 estimate.
    assert w.stage_bar.bar.value() == 500
    assert w.stage_bar.eta_label.text() == "ETA 5:00"
    # The status bar keeps its per-stage detail (unchanged behavior).
    assert "Encode: e0" in w.statusBar().currentMessage()

    # Stop path clears the card label too, not just the pill.
    w._stop_liveness()
    assert w.time_remaining.text() == ""
    assert w.total_pill.text() == ""
    w.close()


def test_scan_worker_drives_mode_uses_60s_timeout_runner(qapp, monkeypatch):
    """QW-3: the drives listing must go through a runner built with
    timeout=60.0 (make_runner kills on expiry) so an orphan makemkvcon
    can no longer hang 'Looking for drives ...' forever."""
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.workers import ScanWorker

    captured = {}

    def fake_runner(binary, args):
        return iter([])

    def fake_make_runner(timeout=None, cancel_state=None):
        captured["timeout"] = timeout
        return fake_runner

    def fake_find_drives(binary="makemkvcon", *, runner=None, timeout=None):
        captured["runner"] = runner
        return []

    monkeypatch.setattr(workers_mod, "make_runner", fake_make_runner)
    monkeypatch.setattr(workers_mod, "find_drives", fake_find_drives)

    listed, failed = [], []
    w = ScanWorker(mode="drives")
    w.drives_listed.connect(listed.append)
    w.failed.connect(failed.append)
    w.run()  # drive synchronously; direct slots need no event loop

    assert captured["timeout"] == 60.0
    assert captured["runner"] is fake_runner
    assert listed == [[]]   # emitted the (empty) drive list
    assert failed == []     # and never took the error path


def test_history_table_renders_size_and_gb_per_hour(qapp, monkeypatch):
    """QW-6: History gains Size (humanized MB/GB) + GB/hr columns; the
    achieved rate reads directly against the encode budgets. Unknown
    size or duration renders as '-'."""
    from bluray_to_mp4 import makemkv as mm
    import bluray_to_mp4.ui.workers as workers_mod
    from bluray_to_mp4.ui.main_window import MainWindow
    from bluray_to_mp4.monitor import JobRecord, state as monitor_state

    monkeypatch.setattr(mm, "find_drives", lambda **kw: [])
    monkeypatch.setattr(workers_mod, "find_drives", lambda **kw: [])
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)

    monkeypatch.setattr(monitor_state, "recent", [
        JobRecord(name="Movie (2019).mp4", kind="movie",
                  duration_seconds=7200, encoder="x265",
                  size_bytes=2_000_000_000, target_gb_per_hour=2.25),
        JobRecord(name="Show S01E01.mp4", kind="series",
                  duration_seconds=3600, encoder="x265",
                  size_bytes=500_000_000),
        JobRecord(name="Legacy.mp4", kind="movie",
                  duration_seconds=3600, encoder="x265"),  # size unknown
    ])
    w._refresh_history()

    headers = [w.history_table.horizontalHeaderItem(c).text()
               for c in range(w.history_table.columnCount())]
    assert headers == ["Output", "Type", "Duration", "Size", "GB/hr",
                      "Target GB/hr", "Notes"]
    assert w.history_table.item(0, 3).text() == "2.0 GB"
    assert w.history_table.item(0, 4).text() == "1.0"
    # v0.2.18: the run's Target GB/hr beside the achieved rate; a
    # pre-slider record (0.0) renders as an empty cell, never junk.
    assert w.history_table.item(0, 5).text() == "2.25"
    assert w.history_table.item(1, 5).text() == ""
    assert w.history_table.item(1, 3).text() == "500 MB"
    assert w.history_table.item(1, 4).text() == "0.5"
    assert w.history_table.item(2, 3).text() == "-"
    assert w.history_table.item(2, 4).text() == "-"
    assert w.history_table.item(2, 5).text() == ""
    w.close()


def test_run_worker_records_output_size(qapp, monkeypatch, tmp_path):
    """QW-6: after each completed job the worker stats the output file
    and stores size_bytes on the JobRecord (0 when the file is gone)."""
    from bluray_to_mp4.monitor import MonitorState
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.workers import RunWorker

    class _Job:
        def __init__(self, name):
            self.output_name = name
            self.completed = True
            self.srt_path = None
            self.duration_seconds = 7200

    class _Result:
        is_series = False
        encoder = "x265"
        jobs = [_Job("movie.mp4")]
        errors = []
        warnings = []

    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    monkeypatch.setattr(workers_mod, "run_pipeline",
                        lambda *a, **k: _Result())

    (tmp_path / "movie.mp4").write_bytes(b"x" * 4096)
    plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": False,
        "encoder": "x265", "outdir": str(tmp_path),
        "jobs": [{"tid": 0, "output_name": "movie.mp4",
                  "duration_seconds": 7200}],
        "tid_names": {},
    }
    worker = RunWorker(0, plan, Settings())
    worker.start()
    _pump(qapp, worker)

    state = workers_mod.monitor_state
    assert state.jobs_done == 1
    rec = state.recent[0]
    assert rec.size_bytes == 4096
    # Same pinned formula as the monitor unit test: bytes/1e9 per hour.
    assert abs(rec.gb_per_hour - (4096 / 1e9 / 2.0)) < 1e-12


def test_run_worker_records_target_gbph(qapp, monkeypatch, tmp_path):
    """v0.2.18: the plan's size target lands on the JobRecord so
    History can show target vs actual; an absent key (CLI / pre-slider
    plan) resolves to the media type's built-in default."""
    from bluray_to_mp4.monitor import MonitorState
    from bluray_to_mp4.ui import workers as workers_mod
    from bluray_to_mp4.ui.workers import RunWorker

    class _Job:
        def __init__(self, name):
            self.output_name = name
            self.completed = True
            self.srt_path = None
            self.duration_seconds = 3600

    class _Result:
        is_series = False
        encoder = "x265"
        jobs = [_Job("movie.mp4")]
        errors = []
        warnings = []

    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    monkeypatch.setattr(workers_mod, "run_pipeline",
                        lambda *a, **k: _Result())

    base_plan = {
        "disc_name": "Disc", "media_type": "bluray", "is_series": False,
        "encoder": "x265", "outdir": str(tmp_path),
        "jobs": [{"tid": 0, "output_name": "movie.mp4",
                  "duration_seconds": 3600}],
        "tid_names": {},
    }

    # Explicit Review-page value: recorded verbatim.
    worker = RunWorker(0, dict(base_plan, target_gb_per_hour=2.75),
                       Settings())
    worker.start()
    _pump(qapp, worker)
    assert workers_mod.monitor_state.recent[0].target_gb_per_hour == 2.75

    # Absent key, Blu-ray plan: resolved to the 2.0 default.
    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    worker2 = RunWorker(0, dict(base_plan), Settings())
    worker2.start()
    _pump(qapp, worker2)
    assert workers_mod.monitor_state.recent[0].target_gb_per_hour == 2.0

    # Absent key, DVD plan: resolved to the 1.0 default.
    monkeypatch.setattr(workers_mod, "monitor_state", MonitorState())
    worker3 = RunWorker(0, dict(base_plan, media_type="dvd"), Settings())
    worker3.start()
    _pump(qapp, worker3)
    assert workers_mod.monitor_state.recent[0].target_gb_per_hour == 1.0


# ----------------------------------------------------------------------
# v0.2.18: GB/hour size target (Review spin, per-type persistence,
# live projection, History Target GB/hr).

# DVD pressing of the movie fixture: same content, DRV disc-type code 1.
_DVD_MOVIE_OUTPUT = MOVIE_DISC_OUTPUT.replace(
    "DRV:0,2,999,12,", "DRV:0,2,999,1,")


def _dvd_window(qapp, monkeypatch):
    """DVD-variant movie window with the Review page filled."""
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(_DVD_MOVIE_OUTPUT.splitlines()))
    w = MainWindow(Settings())
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    return w, disc


def test_gbph_spin_defaults_by_media_type(qapp, monkeypatch):
    """Design 2.1: a fresh scan seeds the spin from the type default -
    2.00 GB/hour for the Blu-ray fixture, 1.00 for a DVD."""
    w, _ = _movie_window(qapp, monkeypatch)
    assert w.spin_gbph.value() == 2.00
    assert w.spin_gbph.suffix() == " GB/hour"
    assert w.spin_gbph.decimals() == 2
    w.close()

    dw, _ = _dvd_window(qapp, monkeypatch)
    assert dw.spin_gbph.value() == 1.00
    dw.close()


def test_gbph_spin_step_and_range(qapp, monkeypatch):
    """Design 2.2: the step is exactly 0.25 (Eric's 250 MB increments)
    and the value clamps at the 0.50/4.00 range bounds."""
    w, _ = _movie_window(qapp, monkeypatch)
    assert w.spin_gbph.singleStep() == 0.25
    assert (w.spin_gbph.minimum(), w.spin_gbph.maximum()) == (0.5, 4.0)
    w.spin_gbph.stepUp()
    assert w.spin_gbph.value() == 2.25
    w.spin_gbph.setValue(0.1)   # below the floor: clamps to 0.50
    assert w.spin_gbph.value() == 0.5
    w.spin_gbph.stepDown()
    assert w.spin_gbph.value() == 0.5   # the floor holds
    w.spin_gbph.setValue(9.0)   # above the ceiling: clamps to 4.00
    assert w.spin_gbph.value() == 4.0
    w.close()


def test_gbph_reset_button_restores_default(qapp, monkeypatch):
    """Design 1.3: Reset restores the disc type's built-in default and
    doubles as the discoverable answer to 'what was the normal value?'."""
    w, _ = _movie_window(qapp, monkeypatch)
    w.spin_gbph.setValue(3.5)
    w.btn_gbph_reset.click()
    assert w.spin_gbph.value() == 2.0
    w.close()

    dw, _ = _dvd_window(qapp, monkeypatch)
    dw.spin_gbph.setValue(2.0)
    dw.btn_gbph_reset.click()
    assert dw.spin_gbph.value() == 1.0
    dw.close()


def test_gbph_projection_label_movie(qapp, monkeypatch):
    """Design 1.4/5: with the main feature selected, the muted label
    shows the projected MP4 size at the current GB/hour (fixture main
    feature 2:06:30 at 2.0 GB/hour -> 4.2 GB total)."""
    w, _ = _movie_window(qapp, monkeypatch)
    assert w.label_gbph_projection.text() == "126 min -> about 4.2 GB total"
    w.close()


def test_gbph_projection_label_series(qapp, monkeypatch):
    """Series format: the sum over the planned episodes (fixture: four
    episodes, 201 min total at 2.0 GB/hour -> about 6.7 GB)."""
    w = _series_window(qapp, monkeypatch)
    assert w.label_gbph_projection.text() == (
        "4 episodes, 201 min total -> about 6.7 GB total")
    w.close()


def test_gbph_projection_label_updates_on_change(qapp, monkeypatch):
    """The projection re-renders live on every spin change."""
    w, _ = _movie_window(qapp, monkeypatch)
    w.spin_gbph.setValue(2.25)
    assert w.label_gbph_projection.text() == "126 min -> about 4.7 GB total"
    w.spin_gbph.setValue(4.0)
    assert w.label_gbph_projection.text() == "126 min -> about 8.4 GB total"
    w.close()


def test_start_run_writes_gbph_into_plan(qapp, monkeypatch, tmp_path):
    """Design 3 step 1: _start_run reads the spin at Start time into
    plan['target_gb_per_hour'] (same read-at-Start contract as
    stream_overrides; always written, pipeline normalizes)."""
    import bluray_to_mp4.ui.main_window as mw_mod

    monkeypatch.setattr(mw_mod, "RunWorker", _RecordingRunWorker)
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w, _ = _movie_window(qapp, monkeypatch)
    w.spin_gbph.setValue(2.75)
    w.edit_outdir.setText(str(tmp_path))
    w._start_run()
    assert w.run_worker.plan["target_gb_per_hour"] == 2.75
    w.close()


def test_gbph_persists_per_media_type(qapp, monkeypatch, tmp_path):
    """Design 4: Start persists the value per media type in
    settings.extras; the other type's key is never touched (a DVD never
    inherits a remembered Blu-ray target)."""
    import bluray_to_mp4.ui.main_window as mw_mod

    monkeypatch.setattr(mw_mod, "RunWorker", _RecordingRunWorker)
    monkeypatch.setattr(Settings, "save", lambda self: None)

    w, _ = _movie_window(qapp, monkeypatch)
    w.spin_gbph.setValue(2.25)
    w.edit_outdir.setText(str(tmp_path))
    w._start_run()
    assert w.settings.extras["gbph_bluray"] == 2.25
    assert "gbph_dvd" not in w.settings.extras
    w.close()


def test_gbph_rescan_uses_saved_per_type_value(qapp, monkeypatch):
    """Design 2.1 + 4: a saved per-type value seeds the spin on a
    fresh scan; the other type stays at its built-in default."""
    from bluray_to_mp4.encode import BLURAY_GB_PER_HOUR
    from bluray_to_mp4.ui.main_window import MainWindow

    disc = scan_disc(0, runner=fake_runner(_DVD_MOVIE_OUTPUT.splitlines()))
    s = Settings()
    s.extras["gbph_dvd"] = 1.25
    w = MainWindow(s)
    w.show()
    _pump(qapp, w.scan_worker)
    w.disc = disc
    w.guess = detect_series(disc)
    w._fill_review_page()
    assert w.spin_gbph.value() == 1.25   # saved DVD value wins

    # Rescan a Blu-ray: no saved bluray value -> built-in default.
    bd = scan_disc(0, runner=fake_runner(MOVIE_DISC_OUTPUT.splitlines()))
    w.disc = bd
    w.guess = detect_series(bd)
    w._fill_review_page()
    assert w.spin_gbph.value() == BLURAY_GB_PER_HOUR
    w.close()


def test_gbph_saved_value_clamped_on_read(qapp, monkeypatch):
    """Design 4 validation: a hand-edited config.json value is
    clamped into [0.5, 4.0] and snapped to the 0.25 step on read."""
    from bluray_to_mp4.ui.main_window import MainWindow

    def _filled_window(extras_value):
        disc = scan_disc(
            0, runner=fake_runner(_DVD_MOVIE_OUTPUT.splitlines()))
        s = Settings()
        s.extras["gbph_dvd"] = extras_value
        w = MainWindow(s)
        w.show()
        _pump(qapp, w.scan_worker)
        w.disc = disc
        w.guess = detect_series(disc)
        w._fill_review_page()
        return w

    w = _filled_window(9.0)   # out of range: clamps to the 4.00 ceiling
    assert w.spin_gbph.value() == 4.0
    w.close()

    w2 = _filled_window(1.13)  # not a step multiple: snaps to 1.25
    assert w2.spin_gbph.value() == 1.25
    w2.close()
