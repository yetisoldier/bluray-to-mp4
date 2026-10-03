"""Main window: sidebar navigation, cards, browse-for-folder, previews,

cancel, ETA, history. Design system: apply_theme() from theme.py.
Pages are managed by the sidebar index (0=Setup, 1=Source, 2=Review,
3=Encode, 4=History).
"""

from __future__ import annotations

import os
import time
import webbrowser

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenuBar,
    QMenu,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..deps import check_all, missing_required
from ..encode import BLURAY_GB_PER_HOUR, DVD_GB_PER_HOUR
from ..makemkv import MakeMkvError
from ..models import Disc
from ..monitor import lan_ip, start_server, stop_server
from ..naming import movie_output_name
from ..recommend import (
    classify_sub_label,
    is_commentary,
    recommend_title,
)
from ..series import detect_series, episode_output_names, main_feature
from ..tmdb import lookup_movie, season_episode_titles
from .workers import FetchWorker, RunWorker, ScanWorker
from .widgets import ActivityDots, card_frame, muted, section, EtaProgressBar

# closeEvent wait budgets (ms) for background workers; the run worker
# gets the longer window because ffmpeg honors its terminate signal.
_CLOSE_WAIT_SCAN_MS = 3000
_CLOSE_WAIT_RUN_MS = 30000


class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.disc: Disc | None = None
        self.guess = None
        self._episode_titles: dict[int, str] = {}
        self._title_overrides: dict[int, str] = {}
        # v0.2.15 (Gap 1): Review-page track overrides. Only tids the
        # user actually touched appear here; a value of None for
        # subtitle_sid means the user picked "(no subtitles)". Read at
        # Start time like the filename overrides, cleared on every scan.
        self._stream_overrides: dict = {}
        # DriveEntry list backing the drives table rows; row != MakeMKV
        # disc index once status-256 drives are filtered out (C7).
        self._drives: list = []

        self.scan_worker: ScanWorker | None = None
        self.run_worker: RunWorker | None = None
        self.fetch_worker: FetchWorker | None = None

        # Encode-page liveness: elapsed clock + moving dots so a run in
        # progress is visibly alive even at 0% (BD+ open, 2026-10-02).
        self._run_started_at: float | None = None
        # Total-remaining model (Eric request, validation run 7): the run
        # is (verify? + rip? + encode + OCR?) per job. Stage estimates:
        #   rip    ~90 s/minute of title (BD+ ~5 min + 2x drive read)
        #   encode ~title/6  (NVENC ~6x realtime) or title/1.5 (x265)
        #   OCR    ~title/20 (measured SE rate, 2026-10-02)
        # Each stage replaces its forecast with the live ETA the moment
        # it starts (Eric: "properly adjusted when that step starts").
        self._stage_now = "scan"      # scan|rip|encode|ocr|done
        self._stage_eta: float | None = None  # live ETA of current stage
        self._current_job_index: int | None = None
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self.setWindowTitle("Ripper")
        self.setMinimumSize(980, 700)
        self.resize(1080, 760)
        self.setStyleSheet("")  # theme.py applies the real stylesheet

        self._build_ui()

    # ------------------------------------------------------------------
    # Build

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- left sidebar ------------------------------------------------
        navbar = QFrame()
        navbar.setObjectName("navBar")
        nav_layout = QVBoxLayout(navbar)
        nav_layout.setContentsMargins(6, 10, 6, 6)
        nav_layout.setSpacing(0)

        brand_row = QHBoxLayout()
        brand = QLabel("Ripper")
        brand.setObjectName("brand")
        accent = QLabel("•")
        accent.setObjectName("brandAccent")
        brand_row.addWidget(brand)
        brand_row.addWidget(accent)
        brand_row.addStretch()
        nav_layout.addLayout(brand_row)

        brand_sub = QLabel("Disc to MP4")
        brand_sub.setObjectName("brandSub")
        nav_layout.addWidget(brand_sub)

        self.nav_list = QListWidget()
        self.nav_list.setObjectName("navList")
        self.nav_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.nav_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for label in ("Setup", "Source", "Review", "Encode", "History"):
            item = QListWidgetItem(label)
            item.setSizeHint(QSize(0, 40))
            self.nav_list.addItem(item)
        # Connect the row->stack sync BEFORE setting the initial row so the
        # stack and sidebar can never disagree (the 2026-10-02 Continue bug:
        # row was set to 1 pre-connect, stack stayed on Setup, and Continue's
        # 1->1 was a no-op that left the page frozen on Setup).
        # NOTE: the initial row itself is set AFTER all pages are built
        # (see __init__ below) - _nav_changed touches self.stack, which
        # does not exist yet during sidebar construction; setting it here
        # crashed _nav_changed with a startup AttributeError traceback
        # (Eric's terminal, validation run 5, fixed v0.2.11).
        self.nav_list.currentRowChanged.connect(self._nav_changed)
        nav_layout.addWidget(self.nav_list)
        nav_layout.addStretch()
        layout.addWidget(navbar, stretch=0)

        # --- main column -------------------------------------------------
        column = QWidget()
        content_layout = QVBoxLayout(column)
        content_layout.setContentsMargins(12, 8, 12, 8)
        content_layout.setSpacing(10)

        # Header: current page title + total-remaining + monitor pills.
        # The total pill (design addendum §6) shows the all-steps
        # forecast in the header while a run is live, so the number is
        # visible from every page - the Encode card's time_remaining
        # shows the same total but only on the Encode page. Single
        # writer (live run 12 flicker fix): _forecast_total owns both,
        # refreshed once a second by _tick_elapsed -> _forecast_total
        # and cleared in _stop_liveness (done/failed/cancel all pass
        # through it); per-stage ETAs live on stage_bar's eta_label only.
        header_row = QHBoxLayout()
        self.page_title = QLabel("Source")
        self.page_title.setObjectName("h1")
        header_row.addWidget(self.page_title)
        header_row.addStretch()
        self.total_pill = QLabel("")
        self.total_pill.setObjectName("statusPill")
        header_row.addWidget(self.total_pill)
        self.monitor_pill = QLabel("")
        self.monitor_pill.setObjectName("statusPill")
        header_row.addWidget(self.monitor_pill)
        content_layout.addLayout(header_row)

        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack)
        layout.addWidget(column, stretch=1)

        self.setCentralWidget(central)

        # Build each page in order matching the sidebar index
        self.pages = []
        self._build_setup_page()   # index 0
        self._build_source_page()  # index 1
        self._build_review_page()  # index 2
        self._build_encode_page()  # index 3
        self._build_history_page() # index 4
        # Initial page: set only once the stack and all five pages exist,
        # so _nav_changed fires safely (v0.2.11 - setting it during
        # sidebar construction crashed with 'no attribute stack').
        self.nav_list.setCurrentRow(1)  # Source is the default page

        # Status bar + menubar
        self.statusBar().showMessage("Ready")
        self._build_menubars()

        # Initial population + first-run checks
        self._refresh_monitor_pill()
        self._show_deps_if_needed()
        self._refresh_drives()

    def _build_menubars(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        file_menu.addAction("E&xit", self.close)
        view_menu = menu.addMenu("&View")
        self.dark_action = view_menu.addAction("&Dark mode", self._toggle_theme)
        self.dark_action.setCheckable(True)
        self.dark_action.setChecked(self.settings.theme == "dark")
        help_menu = menu.addMenu("&Help")
        help_menu.addAction("&About", self._show_about)

    def _nav_changed(self, row: int) -> None:
        # Guard: the signal can fire during construction (before the
        # stack exists); never crash there (v0.2.11).
        if not hasattr(self, "stack"):
            return
        self.stack.setCurrentIndex(row)
        titles = ["Setup", "Source", "Review", "Encode", "History"]
        self.page_title.setText(titles[row] if 0 <= row < len(titles) else "")

    def _toggle_theme(self, checked: bool) -> None:
        from .theme import apply_theme

        app = self.window()  # QApplication cast
        # The QApplication instance is always reachable via QApplication.instance().
        from PySide6.QtWidgets import QApplication

        theme = "dark" if checked else "light"
        apply_theme(QApplication.instance(), theme)
        self.dark_action.setChecked(checked)
        self.settings.theme = theme
        self.settings.save()

    def _show_about(self) -> None:
        import bluray_to_mp4

        QMessageBox.about(
            self,
            "About Ripper",
            f"Ripper v{bluray_to_mp4.__version__}\n"
            "Package: bluray-to-mp4\n\n"
            "Local disc-to-MP4 ripper with recommendations and monitoring.\n"
            "ffmpeg/ffprobe and Subtitle Edit are bundled; only MakeMKV is a\n"
            "separate install.\n\n"
            "Published by YetiWurks LLC\n"
            "Private repo: https://github.com/yetisoldier/bluray-to-mp4",
        )

    def _refresh_monitor_pill(self) -> None:
        from ..monitor import lan_ip

        if self.settings.monitor_enabled:
            ip = lan_ip()
            self.monitor_pill.setText(
                f"Monitor on: {ip}:{self.settings.monitor_port}")
        else:
            self.monitor_pill.setText("Monitor off")

    # ------------------------------------------------------------------
    # 0. Setup page (dependency check)

    def _build_setup_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(section("Setup"))
        layout.addWidget(muted(
            "First-run check: install the listed external tools. "
            "The app discovers them even without PATH (and bundles ffmpeg/ffprobe "
            "when built via CI). Optional tools unlock extra features."))
        frame = card_frame()
        f_layout = QVBoxLayout(frame)
        self.deps_table = QTableWidget(0, 4)
        self.deps_table.setHorizontalHeaderLabels(
            ["Tool", "Status", "What to do", ""])
        self.deps_table.horizontalHeader().setStretchLastSection(True)
        f_layout.addWidget(self.deps_table)
        layout.addWidget(frame)
        row = QHBoxLayout()
        self.btn_setup_recheck = QPushButton("Re-check")
        self.btn_setup_recheck.clicked.connect(self._show_deps_if_needed)
        self.btn_setup_continue = QPushButton("Continue")
        self.btn_setup_continue.setObjectName("ghostBtn")

        def _continue():
            # Never a no-op: force the row change even if the sidebar
            # already says Source, so the stack always follows (the
            # 2026-10-02 bug: setCurrentRow(1) when row==1 fired no
            # signal and Continue looked dead).
            self.nav_list.blockSignals(True)
            self.nav_list.setCurrentRow(0)
            self.nav_list.blockSignals(False)
            self._nav_to(1)

        self.btn_setup_continue.clicked.connect(_continue)
        self.btn_setup_continue.setToolTip(
            "All required tools check out; optional gaps are listed above.")
        row.addWidget(self.btn_setup_recheck)
        row.addWidget(self.btn_setup_continue)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        self.stack.addWidget(page)
        self.pages.append(page)

    def _show_deps_if_needed(self) -> None:
        # QW-4: pass settings so check_nvenc can honor/store its
        # version-stamped cache (a repeat launch skips the hardware
        # probe for the same ffmpeg build).
        checks = check_all(self.settings)
        missing = missing_required(checks)
        self.deps_table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            ok = "Ready" if check.ok else "Missing"
            self.deps_table.setItem(row, 0, QTableWidgetItem(check.name))
            item_status = QTableWidgetItem(ok)
            from .theme import color as theme_color
            from PySide6.QtGui import QColor
            item_status.setForeground(
                QColor(theme_color(self.settings.theme,
                                   "ok" if check.ok else "err")))
            self.deps_table.setItem(row, 1, item_status)
            self.deps_table.setItem(row, 2, QTableWidgetItem(check.detail))
            if check.url:
                btn = QPushButton("Open")
                btn.clicked.connect(
                    lambda checked=False, u=check.url: webbrowser.open(u))
                self.deps_table.setCellWidget(row, 3, btn)
            else:
                self.deps_table.setCellWidget(row, 3, QLabel(""))
        self.deps_table.resizeColumnsToContents()
        # "All green" means no missing REQUIRED tools; optional gaps
        # (e.g. NVENC absent) are informational, never a blocker. When
        # everything is ready, say so plainly and drop the ominous tone.
        if missing:
            self.btn_setup_continue.setText("Continue anyway")
            self.btn_setup_continue.setToolTip(
                "Some REQUIRED tools are missing - outputs may fail.")
            self._nav_to(0)
        else:
            missing_optional = [
                c for c in checks if not c.ok and not getattr(c, "required", True)
            ]
            self.btn_setup_continue.setText("Continue")
            if not missing_optional:
                self.btn_setup_continue.setToolTip("All tools ready.")
            else:
                self.btn_setup_continue.setToolTip(
                    "All required tools ready; some optional features "
                    "are unavailable (listed above).")
            if self.stack.currentIndex() == 0:
                self._nav_to(1)
        self._refresh_monitor_pill()

    # ------------------------------------------------------------------
    # 1. Source page (drive list + scan)

    def _build_source_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(section("Source"))
        layout.addWidget(muted("Insert a disc, choose a drive, and scan."))

        card = card_frame()
        cl = QVBoxLayout(card)
        cl.setSpacing(10)

        # Drive list
        self.drives_table = QTableWidget(0, 4)
        self.drives_table.setHorizontalHeaderLabels(["Drive", "Type", "Disc", "Path"])
        self.drives_table.horizontalHeader().setStretchLastSection(False)
        self.drives_table.horizontalHeader().setSectionResizeMode(2, __import__("PySide6.QtWidgets", fromlist=["QHeaderView"]).QHeaderView.ResizeMode.Stretch)
        self.drives_table.verticalHeader().setVisible(False)
        self.drives_table.setEditTriggers(
            QTableWidget.NoEditTriggers)
        self.drives_table.itemSelectionChanged.connect(self._drive_selected)
        cl.addWidget(self.drives_table)

        btn_row = QHBoxLayout()
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_drives)
        self.btn_scan = QPushButton("Scan disc")
        self.btn_scan.setObjectName("primaryBtn")
        self.btn_scan.clicked.connect(self._scan_selected)
        btn_row.addWidget(self.btn_refresh)
        btn_row.addWidget(self.btn_scan)
        btn_row.addStretch()
        cl.addLayout(btn_row)

        self.scan_status = QLabel("")
        self.scan_status.setWordWrap(True)
        self.scan_status.setObjectName("muted")
        self.scan_progress_row = QHBoxLayout()
        self.scan_indicator = QLabel("")  # animated text: "Scanning . . ."
        self.scan_indicator.setObjectName("muted")
        self.scan_progress_row.addWidget(self.scan_indicator)
        cl.addLayout(self.scan_progress_row)
        cl.addWidget(self.scan_status)

        layout.addWidget(card)
        layout.addStretch()
        self.stack.addWidget(page)
        self.pages.append(page)

    def _drive_selected(self) -> None:
        idx = self.drives_table.currentRow()
        if 0 <= idx < self.drives_table.rowCount():
            item = self.drives_table.item(idx, 0)
            self.btn_scan.setEnabled(bool(item))

    def _refresh_drives(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.scan_status.setText("Looking for drives ...")
        self.scan_indicator.setText("Scanning . . .")
        self.drives_table.setRowCount(0)
        self._drives = []  # keep the table and its backing list in sync
        self.scan_worker = ScanWorker(mode="drives")  # type: ignore[name-defined]
        self.scan_worker.drives_listed.connect(self._drives_ready)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.start()

    def _drives_ready(self, drives) -> None:  # noqa: C901
        self._drives = list(drives)
        self.btn_refresh.setEnabled(True)
        self.scan_indicator.setText("")
        self.drives_table.setRowCount(len(drives))
        for row, d in enumerate(drives):
            status_icon = "🟢" if d.has_disc else "⚪"
            self.drives_table.setItem(row, 0, QTableWidgetItem(d.drive_name))
            self.drives_table.setItem(row, 1, QTableWidgetItem(
                d.disc_type.upper() if d.disc_type else "-"))
            self.drives_table.setItem(row, 2, QTableWidgetItem(
                f"{status_icon} {d.disc_label or '-'}"))
            self.drives_table.setItem(row, 3, QTableWidgetItem(d.path))
        self.drives_table.resizeColumnsToContents()
        if not drives:
            self.scan_status.setText("No optical drives found.")
            self.btn_scan.setEnabled(False)
            return
        self.btn_scan.setEnabled(True)
        self.scan_status.setText("")
        # Default to the first loaded disc
        for row, d in enumerate(drives):
            if d.has_disc:
                self.drives_table.selectRow(row)
                return

    def _scan_failed(self, message: str) -> None:
        self.btn_refresh.setEnabled(True)
        self.scan_indicator.setText("")
        self.scan_status.setText(message)
        self.scan_status.setStyleSheet(
            "color: palette(bright-text, err);")
        # A hung drive listing must never leave the user stuck on an
        # endless "Looking for drives" (orphan makemkvcon holding the
        # drive, 2026-10-02): name the fix instead of a bare timeout.
        if "timed out" in message.lower():
            self.scan_status.setText(
                "Drive check timed out - another MakeMKV process is "
                "probably holding the drive. End any leftover makemkvcon "
                "in Task Manager, then click Refresh.")

    def _scan_progress(self, text: str) -> None:
        """Live scan heartbeat: MakeMKV reading titles/streams, not frozen."""
        self.scan_status.setText(text)

    def _scan_selected(self) -> None:
        row = self.drives_table.currentRow()
        # Row is a position in the filtered drive list, not the MakeMKV
        # disc index: address the scan through the stored DriveEntry.
        if not 0 <= row < len(self._drives):
            return
        self.btn_scan.setEnabled(False)
        self.scan_indicator.setText("Scanning disc ...")
        self.scan_status.setText("Reading titles, tracks, and durations ...")
        self.scan_worker = ScanWorker(mode="scan", index=self._drives[row].index)
        self.scan_worker.scanned.connect(self._disc_scanned)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.progress.connect(self._scan_progress)
        self.scan_worker.start()

    def _disc_scanned(self, disc: Disc) -> None:
        self.btn_scan.setEnabled(True)
        self.scan_indicator.setText("")
        self.disc = disc
        self.guess = detect_series(disc)
        self._fill_review_page()
        self._nav_to(2)

    # ------------------------------------------------------------------
    # 2. Review page (recommendations + naming + output)

    def _build_review_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(section("Review"))

        if self.disc is not None:
            sub = f"{self.disc.name} ({self.disc.media_type or 'unknown'})"
            layout.addWidget(muted(f"Detected disc: {sub}"))
            layout.addWidget(muted("Reasons: " + "; ".join(self.guess.reasons)))

        # Naming controls + output
        top_grid = QGridLayout()
        top_grid.setColumnStretch(1, 1)

        # Series or movie naming fields
        self.group_series = QGroupBox("Series naming")
        series_layout = QGridLayout(self.group_series)
        series_layout.setColumnStretch(1, 1)
        series_layout.addWidget(QLabel("Show name:"), 0, 0)
        self.edit_series_name = QLineEdit()
        series_layout.addWidget(self.edit_series_name, 0, 1)
        series_layout.addWidget(QLabel("Season:"), 0, 2)
        self.spin_season = QSpinBox()
        self.spin_season.setRange(1, 99)
        self.spin_season.setValue(1)
        series_layout.addWidget(self.spin_season, 0, 3)
        series_layout.addWidget(QLabel("First episode #:"), 1, 0)
        self.spin_start_ep = QSpinBox()
        self.spin_start_ep.setRange(1, 9999)
        series_layout.addWidget(self.spin_start_ep, 1, 1)
        # D3: naming edits refresh the output preview live
        self.spin_season.valueChanged.connect(lambda *_: self._update_previews())
        self.spin_start_ep.valueChanged.connect(lambda *_: self._update_previews())
        self.edit_series_name.textChanged.connect(lambda *_: self._update_previews())
        self.btn_fetch_episodes = QPushButton("Fetch episode titles from TMDB")
        self.btn_fetch_episodes.clicked.connect(self._fetch_episode_titles)
        series_layout.addWidget(self.btn_fetch_episodes, 2, 0, 1, 4)
        top_grid.addWidget(self.group_series, 0, 0, 1, 2)

        self.group_movie = QGroupBox("Movie naming")
        movie_layout = QGridLayout(self.group_movie)
        movie_layout.setColumnStretch(1, 1)
        movie_layout.addWidget(QLabel("Title:"), 0, 0)
        self.edit_movie_title = QLineEdit()
        self.edit_movie_title.textChanged.connect(lambda *_: self._update_previews())
        movie_layout.addWidget(self.edit_movie_title, 0, 1)
        movie_layout.addWidget(QLabel("Year:"), 0, 2)
        self.spin_year = QSpinBox()
        self.spin_year.setRange(0, 9999)
        self.spin_year.setSpecialValueText("none")
        self.spin_year.valueChanged.connect(lambda *_: self._update_previews())
        movie_layout.addWidget(self.spin_year, 0, 3)
        self.btn_fetch_year = QPushButton("Look up year from TMDB")
        self.btn_fetch_year.clicked.connect(self._fetch_movie_year)
        movie_layout.addWidget(self.btn_fetch_year, 1, 0, 1, 4)
        self.edit_movie_title.textChanged.connect(lambda *_: self._update_previews())
        top_grid.addWidget(self.group_movie, 0, 0, 1, 2)

        layout.addLayout(top_grid)
        self.tmdb_status = QLabel("")
        self.tmdb_status.setWordWrap(True)
        self.tmdb_status.setObjectName("muted")
        layout.addWidget(self.tmdb_status)

        # Track recommendations + naming previews
        rec_card = card_frame()
        rl = QVBoxLayout(rec_card)
        rl.addWidget(section("Recommended tracks"))
        rl.addWidget(muted(
            "Best English audio and plain-English subtitles are pre-selected.\n"
            "Commentary and descriptive tracks are skipped. Click a track to "
            "see every stream on the disc and pick a different one, or "
            "choose (no subtitles)."))
        self.tracks_table = QTableWidget(0, 4)
        self.tracks_table.setHorizontalHeaderLabels(
            ["Title", "Length", "Recommended audio", "Recommended subtitles"])
        self.tracks_table.horizontalHeader().setStretchLastSection(True)
        self.tracks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        rl.addWidget(self.tracks_table)
        # v0.2.14 caveat surfacing (v0.2.15 design 1.4): one muted line
        # under the table, visible only while at least one row's subtitle
        # recommendation carries the "English not confirmed" caveat.
        self.sub_caveat_label = muted(
            "⚠ Subtitle language not confirmed by the disc scan - "
            "check the dropdown.")
        self.sub_caveat_label.setObjectName("muted")
        self.sub_caveat_label.setVisible(False)
        rl.addWidget(self.sub_caveat_label)
        layout.addWidget(rec_card)

        # Outputs + output options
        outputs_card = card_frame()
        ol = QVBoxLayout(outputs_card)
        ol.addWidget(section("Output"))
        ol.addWidget(muted(
            "Filenames below are the exact output names.\n"
            "Edit per-episode titles if you want overrides; changes preview live."))
        self.outputs_table = QTableWidget(0, 2)
        self.outputs_table.setHorizontalHeaderLabels(["Disc title", "Output filename (double-click to edit)"])
        self.outputs_table.horizontalHeader().setStretchLastSection(True)
        # Episode titles are user-editable (G13): column 1 edits become
        # _title_overrides entries and the canonical name is regenerated.
        self.outputs_table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed)
        self.outputs_table.itemChanged.connect(self._output_name_edited)
        ol.addWidget(self.outputs_table)

        row = QGridLayout()
        row.addWidget(QLabel("Output folder:"), 0, 0)
        self.edit_outdir = QLineEdit()
        row.addWidget(self.edit_outdir, 0, 1)
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.setObjectName("ghostBtn")
        self.btn_browse.clicked.connect(self._choose_output_dir)
        row.addWidget(self.btn_browse, 0, 2)
        row.addWidget(QLabel("Encoder:"), 1, 0)
        self.combo_encoder = QComboBox()
        self.combo_encoder.addItems(["auto", "nvenc", "x265"])
        row.addWidget(self.combo_encoder, 1, 1)
        self.combo_encoder.currentTextChanged.connect(lambda _: self._update_previews())
        row.addWidget(QLabel("Include subtitles:"), 1, 2)
        self.check_subs = QCheckBox("OCR to .srt")
        self.check_subs.setChecked(True)
        row.addWidget(self.check_subs, 1, 3)
        # v0.2.18 (design 1.3): size target on the encoder row - one
        # control per run, the same mental slot as the encoder combo.
        # QDoubleSpinBox over a QSlider (design 1.2, deliberate): exact
        # 0.25 steps, typed input, a truthful numeric readout, and no
        # tick math; theme.py already styles the shared spin chrome.
        row.addWidget(QLabel("Size target:"), 1, 4)
        self.spin_gbph = QDoubleSpinBox()
        self.spin_gbph.setRange(0.5, 4.0)
        self.spin_gbph.setDecimals(2)
        self.spin_gbph.setSingleStep(0.25)
        self.spin_gbph.setCorrectionMode(
            QAbstractSpinBox.CorrectionMode.CorrectToNearestValue)
        self.spin_gbph.setSuffix(" GB/hour")
        self.spin_gbph.setValue(BLURAY_GB_PER_HOUR)
        self.btn_gbph_reset = QPushButton("Reset")
        self.btn_gbph_reset.setObjectName("ghostBtn")
        self.btn_gbph_reset.clicked.connect(self._reset_gbph)
        # v0.2.18 (design 1.4): live projected-size feedback, refreshed
        # on every spin change and every preview/rescan refresh.
        self.label_gbph_projection = QLabel("")
        self.label_gbph_projection.setObjectName("muted")
        self.spin_gbph.valueChanged.connect(
            lambda *_: self._update_gbph_projection())
        row.addWidget(self.spin_gbph, 1, 5)
        row.addWidget(self.btn_gbph_reset, 1, 6)
        row.addWidget(self.label_gbph_projection, 2, 4, 1, 3)
        row.addWidget(QLabel("AI upscale:"), 3, 4)
        self.combo_upscale = QComboBox()
        self.combo_upscale.addItem("Off", "off")
        self.combo_upscale.addItem("720p", "720p")
        self.combo_upscale.addItem("720p (anime)", "720p_anime")
        self.combo_upscale.currentIndexChanged.connect(self._upscale_changed)
        row.addWidget(self.combo_upscale, 3, 5, 1, 2)
        row.addWidget(QLabel("TMDB key:"), 2, 0)
        self.edit_tmdb_key = QLineEdit()
        self.edit_tmdb_key.setPlaceholderText(
            "free key: https://www.themoviedb.org/settings/api")
        self.edit_tmdb_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_tmdb_key.setText(self.settings.tmdb_key or "")
        row.addWidget(self.edit_tmdb_key, 2, 1)
        self.check_monitor = QCheckBox(
            "LAN monitoring (Jarvis checks rip progress from the home server)")
        self.check_monitor.setChecked(self.settings.monitor_enabled)
        self.check_monitor.toggled.connect(self._toggle_monitor)
        row.addWidget(self.check_monitor, 3, 0, 1, 4)
        ol.addLayout(row)
        layout.addWidget(outputs_card)

        btns = QHBoxLayout()
        self.btn_back = QPushButton("Back")
        self.btn_back.setObjectName("ghostBtn")
        self.btn_back.clicked.connect(lambda: self._nav_to(1))
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("primaryBtn")
        self.btn_start.clicked.connect(self._start_run)
        btns.addWidget(self.btn_back)
        btns.addStretch()
        btns.addWidget(self.btn_start)
        layout.addLayout(btns)
        layout.addStretch()
        self.stack.addWidget(page)
        self.pages.append(page)

    # ------------------------------------------------------------------
    # 3. Encode page (progress + ETA + cancel)

    def _build_encode_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(12)
        layout.addWidget(section("Encode"))

        # Top card: current job + progress
        card = card_frame()
        card.setObjectName("runCard")
        cl = QVBoxLayout(card)
        cl.setSpacing(12)
        cl.addWidget(section("Current"))
        self.progress_current = QLabel("Ripping disc (decryption can take a few minutes) ...")
        self.progress_current.setObjectName("runTitle")
        cl.addWidget(self.progress_current)
        self.progress_stage = QLabel("")
        self.progress_stage.setObjectName("runSub")
        self.progress_stage.setWordWrap(True)
        cl.addWidget(self.progress_stage)

        # Liveness + time row: moving dots, elapsed clock, remaining
        # estimate (validation-day request: the screen must always show
        # work happening, even when percent sits at 0 during BD+ open).
        time_row = QHBoxLayout()
        self.activity_dots = ActivityDots()
        time_row.addWidget(self.activity_dots)
        self.time_elapsed = QLabel("Elapsed 0:00")
        self.time_elapsed.setObjectName("metricText")
        time_row.addWidget(self.time_elapsed)
        self.time_remaining = QLabel("")
        self.time_remaining.setObjectName("metricText")
        time_row.addWidget(self.time_remaining)
        time_row.addStretch()
        cl.addLayout(time_row)

        self.overall_row = card_frame()
        self.overall_row.setObjectName("progressPanel")
        overall_layout = QVBoxLayout(self.overall_row)
        overall_layout.setSpacing(6)
        overall_label = QLabel("Overall queue")
        overall_label.setObjectName("progressLabel")
        overall_layout.addWidget(overall_label)
        self.overall_bar = EtaProgressBar()
        overall_layout.addWidget(self.overall_bar)
        self.stage_row = card_frame()
        self.stage_row.setObjectName("progressPanel")
        stage_layout = QVBoxLayout(self.stage_row)
        stage_layout.setSpacing(6)
        stage_label = QLabel("Current stage")
        stage_label.setObjectName("progressLabel")
        stage_layout.addWidget(stage_label)
        self.stage_bar = EtaProgressBar()
        stage_layout.addWidget(self.stage_bar)
        cl.addWidget(self.overall_row)
        cl.addWidget(self.stage_row)
        cl.addWidget(section("Log"))
        self.log_text = QLabel("")
        self.log_text.setObjectName("muted")
        self.log_text.setWordWrap(True)
        cl.addWidget(self.log_text)

        btn_row = QHBoxLayout()
        self.btn_encode_back = QPushButton("Back")
        self.btn_encode_back.setObjectName("ghostBtn")
        self.btn_encode_back.clicked.connect(lambda: self._nav_to(2))
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setObjectName("dangerBtn")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_run)
        btn_row.addWidget(self.btn_encode_back)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_cancel)
        cl.addLayout(btn_row)
        layout.addWidget(card)
        layout.addStretch()
        self.stack.addWidget(page)
        self.pages.append(page)

    # ------------------------------------------------------------------
    # 4. History page

    def _build_history_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(section("History"))
        layout.addWidget(muted("Recently completed outputs (newest first)."))
        card = card_frame()
        cl = QVBoxLayout(card)
        self.history_table = QTableWidget(0, 7)
        self.history_table.setHorizontalHeaderLabels(
            ["Output", "Type", "Duration", "Size", "GB/hr",
             "Target GB/hr", "Notes"])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        cl.addWidget(self.history_table)
        btn_row = QHBoxLayout()
        self.btn_refresh_history = QPushButton("Refresh")
        self.btn_refresh_history.clicked.connect(self._refresh_history)
        btn_row.addWidget(self.btn_refresh_history)
        btn_row.addStretch()
        cl.addLayout(btn_row)
        # QW-1 (Bob audit): the table used to be added a SECOND time to
        # the page layout below, which yanked it out of this card - the
        # History card rendered as a floating Refresh button over an
        # orphaned table. The table stays in the card, period.
        layout.addWidget(card)
        self.stack.addWidget(page)
        self.pages.append(page)

    def _refresh_history(self) -> None:
        from ..monitor import state as monitor_state

        with monitor_state._lock:
            history = list(monitor_state.recent)
        self.history_table.setRowCount(len(history))
        for row, rec in enumerate(history):
            name = rec.name
            duration = ("-" if not rec.duration_seconds else
            f"{rec.duration_seconds // 3600}:{(rec.duration_seconds % 3600) // 60:02d}:{rec.duration_seconds % 60:02d}")
            warnings = ", ".join(rec.warnings) if rec.warnings else ""
            # QW-6: size + achieved GB/hour per record - the budget
            # check Eric has been doing by hand after every run. Decimal
            # units (GB = 1e9) match the encode size budgets; unknown
            # size or duration renders as "-".
            size_bytes = getattr(rec, "size_bytes", 0)
            if not size_bytes:
                size_text = "-"
            elif size_bytes >= 1_000_000_000:
                size_text = f"{size_bytes / 1e9:.1f} GB"
            else:
                size_text = f"{size_bytes / 1e6:.0f} MB"
            gbph = rec.gb_per_hour if (size_bytes and rec.duration_seconds) else 0.0
            gb_text = f"{gbph:.1f}" if gbph else "-"
            # v0.2.18 (design 6): the run's Target GB/hr beside the
            # achieved rate - target vs actual, the pair Eric checks
            # by hand every run. Pre-slider records (0.0) render as an
            # empty cell.
            target = getattr(rec, "target_gb_per_hour", 0.0)
            target_text = f"{target:.2f}" if target else ""
            self.history_table.setItem(row, 0, QTableWidgetItem(name))
            self.history_table.setItem(row, 1, QTableWidgetItem(rec.kind))
            self.history_table.setItem(row, 2, QTableWidgetItem(duration))
            self.history_table.setItem(row, 3, QTableWidgetItem(size_text))
            self.history_table.setItem(row, 4, QTableWidgetItem(gb_text))
            self.history_table.setItem(row, 5, QTableWidgetItem(target_text))
            self.history_table.setItem(row, 6, QTableWidgetItem(warnings))
        self.history_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Shared UI actions (browse, previews, toggle, autofill)

    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose output folder", self.settings.outdir or "",
            QFileDialog.Options.ShowDirsOnly | QFileDialog.Options.DontResolveSymlinks,
        )
        if directory:
            self.edit_outdir.setText(directory)
            self.settings.outdir = directory
            self.settings.save()

    def _output_name_edited(self, item) -> None:
        """G13: an edited output filename becomes a per-title override."""
        if getattr(self, "_in_preview_update", False):
            return
        if item.column() != 1 or not self.guess or not self.guess.is_series:
            return
        tids = getattr(self, "_preview_tids", [])
        if item.row() >= len(tids):
            return
        tid = tids[item.row()]
        import re
        m = re.match(r"^.*? - S\d+E\d+ - (.*)\.mp4$", item.text().strip())
        label = m.group(1) if m else re.sub(r"\.mp4$", "", item.text().strip())
        if label:
            self._title_overrides[tid] = label
        else:
            self._title_overrides.pop(tid, None)
        self._update_previews()

    def _update_previews(self) -> None:
        """Rebuild the preview filename table from the shared naming pass;
        guard against re-entry from itemChanged signals fired mid-rebuild."""
        if getattr(self, "_in_preview_update", False):
            return
        self._in_preview_update = True
        try:
            self._update_previews_impl()
        finally:
            self._in_preview_update = False

    def _update_previews_impl(self) -> None:
        """Rebuild the preview filename table from the shared naming pass."""
        if self.disc is None or self.guess is None:
            return
        outputs = self._plan_output_names()
        self._preview_tids = [tid for tid, _ in outputs]
        self.outputs_table.setRowCount(len(outputs))
        for row, (tid, fname) in enumerate(outputs):
            title = self.disc.title(tid)
            self.outputs_table.setItem(row, 0, QTableWidgetItem(
                (title.title_name if title else f"Title {tid}") or f"Title {tid}"))
            self.outputs_table.setItem(row, 1, QTableWidgetItem(fname))
        self.outputs_table.resizeColumnsToContents()
        # v0.2.18 (design 5): rescans and episode edits land in the
        # projection too - the label reads from the same naming pass.
        self._update_gbph_projection(outputs)

    def _plan_output_names(self) -> list[tuple[int, str]]:
        """(tid, output filename) pairs - the one naming pass shared by
        _update_previews and _start_run so previews and the run plan agree."""
        if self.disc is None or self.guess is None:
            return []
        season = self.spin_season.value()
        start = self.spin_start_ep.value()
        name = (
            self.edit_series_name.text().strip()
            if self.guess.is_series
            else self.edit_movie_title.text().strip()
        )
        year = self.spin_year.value() or None

        if self.guess.is_series:
            tids = self.guess.episode_tids
            base_names = {
                t: self.disc.title(t).filename
                for t in tids if self.disc.title(t)
            }
            return episode_output_names(
                name, season,
                start_episode=start,
                episode_tids=tids,
                episode_titles=self._episode_titles,
                base_names=base_names,
                title_overrides=self._title_overrides,
            )
        main = main_feature(self.disc)
        if not main:
            return []
        return [(main.tid, movie_output_name(name, year=year))]

    # ------------------------------------------------------------------
    # v0.2.18: GB/hour size target (design 1-5)

    def _gbph_default_for(self, media_type: str | None) -> float:
        """This disc type's built-in size budget (encode.py constants)."""
        return DVD_GB_PER_HOUR if media_type == "dvd" else BLURAY_GB_PER_HOUR

    def _gbph_saved_or_default(self, media_type: str | None) -> float:
        """Effective spin default: the per-type value persisted in
        settings.extras (design 4) when present, else the encode.py
        constant. A hand-edited config.json must never produce an
        out-of-range spin: clamp into [0.5, 4.0] and snap to the 0.25
        step on read."""
        default = self._gbph_default_for(media_type)
        saved = self.settings.extras.get(f"gbph_{media_type or 'bluray'}")
        if saved is None:
            return default
        try:
            value = float(saved)
        except (TypeError, ValueError):
            return default
        value = round(value / 0.25) * 0.25
        value = min(4.0, max(0.5, value))
        return round(value, 2)

    def _reset_gbph(self) -> None:
        """Reset restores the disc type's built-in default (2.0 Blu-ray,
        1.0 DVD) and doubles as the discoverable answer to "what was the
        normal value?" (design 1.3)."""
        media_type = self.disc.media_type if self.disc else None
        self.spin_gbph.setValue(self._gbph_default_for(media_type))

    def _upscale_mode(self) -> str:
        return self.combo_upscale.currentData() or "off"

    def _upscale_changed(self) -> None:
        if self.disc is None:
            return
        if self.disc.media_type != "dvd":
            self.combo_upscale.setCurrentIndex(0)
            self.combo_upscale.setEnabled(False)
            return
        if self._upscale_mode() != "off" and self.spin_gbph.value() < 1.5:
            self.spin_gbph.setValue(1.5)
        self._update_gbph_projection()

    def _update_gbph_projection(self, outputs=None) -> None:
        """Live projected-size feedback (design 1.4/5): total budget is
        gbph * hours by definition (container slack and audio are inside
        the gbph budget), from the same _plan_output_names() pass
        _start_run uses, so the number the user reads on Review is the
        number the run will target."""
        self._set_text(
            self.label_gbph_projection, self._gbph_projection_text(outputs))

    def _gbph_projection_text(self, outputs=None) -> str:
        if self.disc is None or self.guess is None:
            return ""
        if outputs is None:
            outputs = self._plan_output_names()
        durations = [
            t.duration_seconds for t in (
                self.disc.title(tid) for tid, _ in outputs
            ) if t
        ]
        if not durations:
            return ""
        total_seconds = sum(durations)
        if total_seconds <= 0:
            return ""
        gbph = self.spin_gbph.value()
        total_gb = gbph * total_seconds / 3600
        minutes = total_seconds // 60
        if len(durations) == 1:
            return f"{minutes} min -> about {total_gb:.1f} GB total"
        return (f"{len(durations)} episodes, {minutes} min total -> "
                f"about {total_gb:.1f} GB total")

    def _toggle_monitor(self, checked: bool) -> None:
        self.settings.monitor_enabled = checked
        try:
            if checked:
                start_server(
                    host=self.settings.monitor_host,
                    port=self.settings.monitor_port,
                    token=self.settings.monitor_token,
                )
                ip = lan_ip()
                self.monitor_pill.setText(
                    f"Monitor on: {ip}:{self.settings.monitor_port}")
                self.tmdb_status.setText(
                    f"Monitoring on: http://{ip}:{self.settings.monitor_port} (read-only)")
            else:
                stop_server()
                self.monitor_pill.setText("Monitor off")
                self.tmdb_status.setText("Monitoring off.")
            self.settings.save()
        except OSError as exc:
            self.settings.monitor_enabled = False
            self.check_monitor.setChecked(False)
            self.tmdb_status.setText(f"Monitor start failed: {exc}")
        self._refresh_monitor_pill()

    def _tmdb_key(self) -> str:
        return (self.edit_tmdb_key.text().strip() or
                (self.settings.tmdb_key or "").strip())

    def _fetch_episode_titles(self) -> None:
        assert self.disc is not None and self.guess is not None
        key = self._tmdb_key()
        if not key:
            self.tmdb_status.setText(
                "Enter a TMDB key first (free: themoviedb.org/settings/api).")
            return
        show = self.edit_series_name.text().strip() or self.disc.name
        season = self.spin_season.value()
        start = self.spin_start_ep.value()
        count = self.guess.episode_count
        self.btn_fetch_episodes.setEnabled(False)
        self.btn_fetch_year.setEnabled(False)
        self.tmdb_status.setText(f"Looking up episode titles for {show!r} ...")

        def do_fetch():
            return season_episode_titles(
                show, season, start_episode=start, count=count, key=key)

        self.fetch_worker = FetchWorker(do_fetch)
        self.fetch_worker.done.connect(self._episode_titles_ready)
        self.fetch_worker.failed.connect(self._tmdb_failed)
        self.fetch_worker.start()

    def _episode_titles_ready(self, titles) -> None:
        self.btn_fetch_episodes.setEnabled(True)
        self.btn_fetch_year.setEnabled(True)
        if self.disc is None or self.guess is None:
            self.tmdb_status.setText("Scan a disc first, then fetch titles.")
            return
        tids = self.guess.episode_tids
        self._episode_titles.update(
            {tid: title for tid, title in zip(tids, titles) if title})
        found = sum(1 for t in titles if t)
        self.tmdb_status.setText(
            f"TMDB: {found} of {len(tids)} episode titles found.")
        self._update_previews()

    def _fetch_movie_year(self) -> None:
        assert self.disc is not None
        key = self._tmdb_key()
        if not key:
            self.tmdb_status.setText(
                "Enter a TMDB key first (free: themoviedb.org/settings/api).")
            return
        title = self.edit_movie_title.text().strip() or self.disc.name
        self.btn_fetch_year.setEnabled(False)
        self.btn_fetch_episodes.setEnabled(False)
        self.tmdb_status.setText(f"Looking up {title!r} on TMDB ...")

        def do_fetch():
            return lookup_movie(title, key=key)

        self.fetch_worker = FetchWorker(do_fetch)
        self.fetch_worker.done.connect(self._movie_year_ready)
        self.fetch_worker.failed.connect(self._tmdb_failed)
        self.fetch_worker.start()

    def _movie_year_ready(self, hit) -> None:
        self.btn_fetch_year.setEnabled(True)
        self.btn_fetch_episodes.setEnabled(True)
        if not hit or not hit.get("year"):
            self.tmdb_status.setText("TMDB: no movie match found.")
            return
        self.spin_year.setValue(hit["year"])
        self.edit_movie_title.setText(hit["title"])
        self.tmdb_status.setText(f"TMDB match: {hit['title']} ({hit['year']})")
        self._update_previews()

    def _tmdb_failed(self, message: str) -> None:
        self.btn_fetch_year.setEnabled(True)
        self.btn_fetch_episodes.setEnabled(True)
        self.tmdb_status.setText(f"TMDB lookup failed: {message}")

    # ------------------------------------------------------------------
    # Run control

    def _start_run(self) -> None:
        assert self.disc is not None
        outdir = self.edit_outdir.text().strip()
        if not outdir:
            self.statusBar().showMessage("Choose an output folder first")
            return
        self.settings.outdir = outdir
        self.settings.encoder = self.combo_encoder.currentText()
        # v0.2.18 (design 4): persist per media type in settings.extras -
        # each disc type remembers exactly its own value, so a DVD never
        # inherits a remembered Blu-ray target (and vice versa).
        self.settings.extras[
            f"gbph_{self.disc.media_type or 'bluray'}"
        ] = float(self.spin_gbph.value())
        self.settings.last_series_name = self.edit_series_name.text().strip()
        self.settings.tmdb_key = self.edit_tmdb_key.text().strip()
        self.settings.save()

        plan = {
            "disc_name": self.disc.name,
            "media_type": self.disc.media_type,
            "is_series": bool(self.guess.is_series),
            "encoder": self.combo_encoder.currentText(),
            "outdir": outdir,
            "series_name": self.edit_series_name.text().strip()
            if self.guess.is_series else None,
            "season": self.spin_season.value(),
            "start_episode": self.spin_start_ep.value(),
            "episode_titles": self._episode_titles or None,
            "title_overrides": dict(self._title_overrides),
            # v0.2.15 (Gap 1): only tids the user actually changed; both
            # keys per entry, subtitle_sid None = "(no subtitles)".
            # Copied by value (not by reference) so Review-page edits
            # during a run can never race the worker thread (QW-5).
            "stream_overrides": {
                tid: dict(ov) for tid, ov in self._stream_overrides.items()
            },
            "year": self.spin_year.value() or None,
            "want_subtitles": self.check_subs.isChecked(),
            # v0.2.18 (design 3): always written, read at Start time
            # (same contract as stream_overrides, so mid-run UI edits can
            # never race the worker); the pipeline normalizes a value
            # equal to the media-type default back to None.
            "target_gb_per_hour": float(self.spin_gbph.value()),
            "dvd_upscale_mode": self._upscale_mode(),
            "jobs": [],
            "tid_names": {},
        }

        # Populate the run plan with the same naming pass the user saw in
        # the preview: the monitor's jobs_total and the overall progress
        # bar are computed from this list.
        outputs = self._plan_output_names()
        plan["jobs"] = [
            {
                "tid": tid,
                "output_name": fname,
                "duration_seconds": (
                    self.disc.title(tid).duration_seconds
                    if self.disc.title(tid) else 0
                ),
            }
            for tid, fname in outputs
        ]
        plan["tid_names"] = {tid: fname for tid, fname in outputs}

        self._nav_to(3)
        self.progress_stage.setText("Starting ...")
        self.stage_bar.reset()
        self.overall_bar.reset()
        self.btn_cancel.setEnabled(True)
        # Liveness: dots animate, clock ticks, and the stage bar pulses
        # until the first real progress event arrives (BD+ open can take
        # minutes with the percent legitimately at 0).
        self._run_started_at = time.time()
        self._elapsed_timer.start()
        self.activity_dots.start()
        self.stage_bar.set_busy(True, "starting ...")
        self.overall_bar.set_busy(True, "queue active ...")
        self.time_elapsed.setText("Elapsed 0:00")
        self._stage_now = "scan"
        self._stage_eta = None

        self.run_worker = RunWorker(self.disc.index, plan, self.settings)
        self.run_worker.stage.connect(self._run_stage)
        self.run_worker.progress.connect(self._run_progress)
        self.run_worker.done.connect(self._run_done)
        self.run_worker.failed.connect(self._run_failed)
        self.run_worker.start()
        # Forecast once the worker (and its plan) exists: seeds the
        # Encode-card estimate and the header pill immediately instead
        # of waiting for the first stage signal or the 1 s tick (the
        # old pre-worker call was a silent no-op - run_worker was None,
        # so _forecast_total_seconds returned None).
        self._forecast_total()

    def _cancel_run(self) -> None:
        if not self.run_worker or not self.run_worker.isRunning():
            return
        self.run_worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.progress_stage.setText("Cancelling ...")

    @staticmethod
    def _set_text(label: QLabel, text: str) -> None:
        """setText only when the string actually changes: the 1 s
        _tick_elapsed pass then costs nothing on identical values
        (no per-second repaint churn on a steady estimate)."""
        if label.text() != text:
            label.setText(text)

    def _fmt_hm(self, seconds: "float | None") -> str:
        if seconds is None:
            return "..."
        m, s = divmod(max(0, int(seconds)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _forecast_total_seconds(self) -> float | None:
        """Forecast the WHOLE remaining run: the current stage's live ETA
        (measured, when available) plus forecast seconds for every stage
        and job still ahead (Eric, validation run 7: a total estimate
        that properly adjusts when each step starts)."""
        if self.run_worker is None:
            return None
        plan = self.run_worker.plan
        jobs = plan.get("jobs", [])
        if not jobs:
            return None
        encoder = (plan.get("encoder") or
                   getattr(self.settings, "encoder", "auto"))
        want_subs = bool(plan.get("want_subtitles", True))
        upscale_mode = str(plan.get("dvd_upscale_mode") or "off").lower()
        wants_upscale = upscale_mode != "off"
        nvenc = encoder in ("auto", "nvenc")
        cur = self._current_job_index
        if cur is None:
            cur = 0
        live = self._stage_eta if self._stage_eta is not None else 0.0
        total = live

        # Real-ESRGAN on DVD live action is deliberately slow and often
        # reports no granular ETA until a full chunk completes. Use a
        # conservative 0.3x realtime forecast so the UI gives an
        # hours-scale estimate instead of "estimating" forever.
        def upscale_seconds(duration: float) -> float:
            return duration / 0.3

        for i, j in enumerate(jobs):
            d = float(j.get("duration_seconds") or 0)
            if i == cur:
                # Current job: swap live ETA in for the current stage;
                # forecast the stages still ahead of it.
                if (
                    wants_upscale
                    and self._stage_now not in ("encode", "ocr")
                    and (self._stage_now != "upscale" or self._stage_eta is None)
                    and d > 0
                ):
                    total += upscale_seconds(d)
                if self._stage_now not in ("encode", "ocr") and d > 0:
                    total += d / (6.0 if nvenc else 1.5)   # encode ahead
                if self._stage_now != "ocr" and want_subs and d > 0:
                    total += d / 20.0                        # OCR ahead
                if self._stage_now == "scan" and d > 0:
                    total += 5 * 60 + d / 40.0               # rip ahead
                continue
            if d <= 0:
                total += 20 * 60  # unknown length: ~2h average fallback
                continue
            if i > cur:
                total += 5 * 60 + d / 40.0                   # rip
                if wants_upscale:
                    total += upscale_seconds(d)
                total += d / (6.0 if nvenc else 1.5)         # encode
                if want_subs:
                    total += d / 20.0                       # OCR
        return total

    def _forecast_total(self) -> None:
        secs = self._forecast_total_seconds()
        if secs is None:
            # No live run plan: nothing to forecast in the header pill.
            # (time_remaining is cleared separately in _stop_liveness.)
            self._set_text(self.total_pill, "")
            return
        # Single writer of the Encode card's time_remaining (live run 12
        # flicker fix): the all-steps total, refreshed once a second by
        # _tick_elapsed. _run_progress no longer touches this label -
        # per-stage ETAs live on stage_bar's eta_label - so the total can
        # no longer flash for <500 ms between progress events.
        self._set_text(
            self.time_remaining,
            f"Total remaining ~{self._fmt_hm(secs)} (all steps)")
        # Header pill (addendum §6): the all-steps forecast, visible from
        # every page, only while a run is active. Rewritten once a second
        # by _tick_elapsed -> _forecast_total, so it can never flicker or
        # go stale, and _stop_liveness clears it on every done/failed/
        # cancel path.
        if self._run_started_at is not None and self._stage_now != "done":
            self._set_text(
                self.total_pill, f"Total remaining ~{self._fmt_hm(secs)}")

    def _tick_elapsed(self) -> None:
        if self._run_started_at is None:
            return
        s = int(time.time() - self._run_started_at)
        m, sec = divmod(s, 60)
        self.time_elapsed.setText(f"Elapsed {m}:{sec:02d}")
        self._forecast_total()

    def _stop_liveness(self) -> None:
        self._elapsed_timer.stop()
        self.activity_dots.stop()
        if self._run_started_at is not None:
            secs = time.time() - self._run_started_at
            self.time_elapsed.setText(f"Total {self._fmt_hm(secs)}")
        self._run_started_at = None
        self.stage_bar.set_busy(False)
        self._set_text(self.time_remaining, "")
        # Header pill shares the time_remaining lifecycle: cleared on
        # done/failed/cancel (every stop path passes through here).
        self._set_text(self.total_pill, "")

    def _run_stage(self, msg: str) -> None:
        self.progress_stage.setText(msg)
        self.log_text.setText(msg)
        self.statusBar().showMessage(msg.removesuffix(" ..."))
        low = msg.lower()
        prev = getattr(self, "_stage_now", "scan")
        if "ocr subtitles" in low:
            self._stage_now = "ocr"
        elif low.startswith("upscaling"):
            self._stage_now = "upscale"
            self.progress_current.setText(msg.removesuffix(" ..."))
        elif low.startswith("encoding"):
            self._stage_now = "encode"
        elif low.startswith("ripping") or "reusing existing rip" in low:
            self._stage_now = "rip"
        elif low.startswith("verifying"):
            self._stage_now = "verify"
        if self._stage_now != prev:
            self._stage_eta = None  # new stage: forecast until live ETA
            self.stage_bar.set_busy(
                True,
                "upscaling ..." if self._stage_now == "upscale" else "estimating ...",
            )
            if self.overall_bar.bar.value() == 0:
                self.overall_bar.set_busy(True, "queue active ...")
        self._forecast_total()

    def _run_progress(
        self,
        job_name: str,
        tid: int,
        frac: float,
        eta: float | None,
        detail: dict | None = None,
    ) -> None:
        self.progress_current.setText(job_name)
        if isinstance(detail, dict) and detail.get("chunk") and detail.get("chunks"):
            step = detail.get("step") or "working"
            self.progress_stage.setText(
                f"Upscaling: chunk {detail['chunk']} of {detail['chunks']} - {step}"
            )
        jobs = self.run_worker.plan.get("jobs", []) if self.run_worker else []
        self._current_job_index = next(
            (i for i, j in enumerate(jobs) if j.get("tid") == tid), 0)
        if eta is not None:
            self._stage_eta = eta
        self._forecast_total()
        self.stage_bar.set_value(frac, eta)
        # Single-writer rule (live run 12 flicker): _run_progress fires
        # 2+/s (MakeMKV PRGV duplicates + ffmpeg -progress lines) and
        # used to overwrite time_remaining with this stage's ETA, so the
        # once-a-second total from _forecast_total only flashed
        # briefly. The stage ETA now lives ONLY on stage_bar's eta_label
        # (set_value above carries the last real estimate across
        # duplicate lines - validation run 5); time_remaining is owned
        # by _forecast_total and shows the all-steps total.
        if eta is not None:
            self.statusBar().showMessage(
                f"{job_name}: {int(frac*100)}% - {int(eta//60)}:{int(eta%60):02d} remaining")
        else:
            self.statusBar().showMessage(f"{job_name}: {int(frac*100)}%")

        # Overall = jobs already completed (index of the current tid in
        # the plan; jobs run in order) + current fraction, over the total.
        jobs = self.run_worker.plan.get("jobs", [])
        total = max(len(jobs), 1)
        done = next((i for i, j in enumerate(jobs) if j.get("tid") == tid), 0)
        self.overall_bar.set_value((done + frac) / total)

    def _run_done(self, result) -> None:
        self.btn_cancel.setEnabled(False)
        self._stop_liveness()
        self._stage_now = "done"
        self.stage_bar.set_value(1.0)
        errors = getattr(result, "errors", [])
        if errors:
            # D5: partial failure must read as partial, never "All done".
            done = sum(1 for j in result.jobs if j.completed)
            self.progress_stage.setText(
                f"Finished with {len(errors)} failure(s): "
                + "; ".join(errors))
            self.statusBar().showMessage(
                f"{done} of {len(result.jobs)} outputs finished");
        else:
            self.overall_bar.set_value(1.0)
            self.progress_stage.setText("All done")
            self.statusBar().showMessage("All outputs finished")
        self._refresh_history()
        # Navigate back to Review to make it obvious something changed
        self._nav_to(2)
        self._fill_review_page()
        self._nav_to(4)

    def _run_failed(self, message: str) -> None:
        self.btn_cancel.setEnabled(False)
        self._stop_liveness()
        self.progress_stage.setText("Failed")
        self.log_text.setText(message)
        self.statusBar().showMessage("Rip failed", 5000)
        self._update_previews()
        self.statusBar().showMessage(f"Error: {message}", 5000)

    # ------------------------------------------------------------------
    # Close: stop workers + monitor cleanly

    def closeEvent(self, event) -> None:
        if self.run_worker and self.run_worker.isRunning():
            answer = QMessageBox.question(
                self, "Rip in progress",
                "A rip/encode is still running. Close anyway? (It will stop.)",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # Signal a graceful stop first so ffmpeg gets its terminate
            # window (partial MP4 may be discarded, temp files kept).
            self.run_worker.cancel()
        stop_server()
        for worker in (self.scan_worker, self.fetch_worker):
            if worker and worker.isRunning():
                worker.wait(_CLOSE_WAIT_SCAN_MS)
        if self.run_worker and self.run_worker.isRunning():
            if not self.run_worker.wait(_CLOSE_WAIT_RUN_MS):
                # Last resort: the thread ignored cancel for 30 s (ffmpeg
                # normally exits within its 10 s terminate window). kill()
                # the OS thread rather than leak a live encode into a
                # destroyed window - documented behavior for stuck runs.
                self.run_worker.terminate()
                self.run_worker.wait(3000)
        # Never accept() while a QThread is still running: a live thread
        # emitting into a destroyed QWidget crashes the app.
        if ((self.scan_worker and self.scan_worker.isRunning())
                or (self.fetch_worker and self.fetch_worker.isRunning())
                or (self.run_worker and self.run_worker.isRunning())):
            event.ignore()
            return
        event.accept()

    # ------------------------------------------------------------------
    # Helpers

    def _nav_to(self, index: int) -> None:
        self.nav_list.setCurrentRow(index)
        self._refresh_monitor_pill()

    # ------------------------------------------------------------------
    # Fill helpers (extracted to keep the layout code readable)

    def _fill_review_page(self) -> None:
        assert self.disc is not None
        self.group_series.setVisible(bool(self.guess and self.guess.is_series))
        self.group_movie.setVisible(not self.guess or self.guess.is_series is False)
        if self.disc.name:
            self.edit_series_name.setText(self.disc.name)
            self.edit_movie_title.setText(self.disc.name)
        if self.settings.outdir:
            self.edit_outdir.setText(self.settings.outdir)
        self.combo_encoder.setCurrentText(self.settings.encoder)
        # v0.2.18 (design 2.1): a fresh scan re-seeds the size-target spin
        # from the per-type saved value (design 4), else the encode.py
        # constant - rescanning a different disc type resets the control
        # to that type's effective default.
        self.combo_upscale.setCurrentIndex(0)
        is_dvd = self.disc.media_type == "dvd"
        self.combo_upscale.setEnabled(is_dvd)
        self.combo_upscale.setToolTip(
            "Optional DVD-only AI upscale before encoding"
            if is_dvd else "AI upscale is only available for DVD scans")
        self.spin_gbph.setValue(
            self._gbph_saved_or_default(self.disc.media_type))
        self.edit_tmdb_key.setText(self.settings.tmdb_key or "")
        self._episode_titles = {}
        self._title_overrides = {}
        # Stream overrides belong to THIS scan's sids - a fresh scan
        # resets them (design 1.3); missing keys mean "use the
        # recommendation" on the pipeline side.
        self._stream_overrides = {}
        self._fill_tracks_table()
        self._update_previews()

    def _fill_tracks_table(self) -> None:
        assert self.disc is not None
        titles = (
            [self.disc.title(t) for t in self.guess.episode_tids]
            if self.guess.is_series else [main_feature(self.disc)]
        )
        titles = [t for t in titles if t]
        self.tracks_table.setRowCount(len(titles))
        any_caveat = False
        for row, t in enumerate(titles):
            sel = recommend_title(t)
            m, s = divmod(t.duration_seconds, 60)
            self.tracks_table.setItem(row, 0, QTableWidgetItem(
                t.title_name or f"Title {t.tid}"))
            self.tracks_table.setItem(row, 1, QTableWidgetItem(f"{m}:{s:02d}"))

            # v0.2.15 (design Gap 1): the audio/subtitle columns are
            # per-row QComboBox cell widgets (same pattern as the
            # deps_table "Open" buttons) so a wrong recommendation is
            # fixable in place. The DEFAULT selection is the
            # recommendation, so the row reads exactly like the old
            # static text until a combo is clicked.
            combo_audio = QComboBox()
            for stream in sorted(t.streams_of("audio"), key=lambda x: x.sid):
                label = (f"{stream.lang_name or '?'} "
                         f"{stream.codec_long or stream.codec} "
                         f"({stream.channels}ch)")
                if is_commentary(stream):
                    label += " [commentary]"
                combo_audio.addItem(label, stream.sid)
            if not combo_audio.count():
                combo_audio.addItem("(no audio)", None)  # broken scan only
            if sel.audio_sid is None:
                idx_audio = 0  # no recommendation: first stream
            else:
                idx_audio = combo_audio.findData(sel.audio_sid)
                if idx_audio < 0:
                    idx_audio = 0
            # Default index BEFORE the handler connect below, so the
            # programmatic fill never writes into _stream_overrides.
            combo_audio.setCurrentIndex(idx_audio)
            self.tracks_table.setCellWidget(row, 2, combo_audio)

            combo_subs = QComboBox()
            combo_subs.addItem("(no subtitles)", None)
            for stream in sorted(t.streams_of("subtitle"),
                                 key=lambda x: x.sid):
                label = (f"{stream.lang_name or 'Sub'} "
                         f"({stream.name or stream.codec})"
                         f"{classify_sub_label(stream)}")
                combo_subs.addItem(label, stream.sid)
            if sel.subtitle_sid is None:
                idx_subs = 0  # recommendation is "none" or no streams
            else:
                idx_subs = combo_subs.findData(sel.subtitle_sid)
                if idx_subs < 0:
                    idx_subs = 0
            # v0.2.14 caveat surfacing (design 1.4): when the scan
            # cannot confirm the subtitle language, mark the row
            # instead of leaving the caveat invisible - ⚠ prefix on
            # the picked item, full reasons on hover.
            if any("English not confirmed" in r
                   for r in sel.subtitle_reasons):
                any_caveat = True
                combo_subs.setItemText(
                    idx_subs, "⚠ " + combo_subs.itemText(idx_subs))
                combo_subs.setToolTip("\n".join(sel.subtitle_reasons))
            combo_subs.setCurrentIndex(idx_subs)
            self.tracks_table.setCellWidget(row, 3, combo_subs)

            # Record an override ONLY on a real combo change: both
            # programmatic setCurrentIndex calls above happen before
            # this connect, and nothing else moves the combos, so a
            # tid lands in _stream_overrides only when the user picks.
            # Both keys are always present; subtitle_sid None is the
            # explicit "(no subtitles)" choice.
            def _on_combo_change(*_args, _tid=t.tid, _audio=combo_audio,
                                 _subs=combo_subs):
                self._stream_overrides[_tid] = {
                    "audio_sid": _audio.currentData(),
                    "subtitle_sid": _subs.currentData(),
                }

            combo_audio.currentIndexChanged.connect(_on_combo_change)
            combo_subs.currentIndexChanged.connect(_on_combo_change)
        # One muted line under the table while ANY row carries the
        # unconfirmed-English caveat - series discs surface it once.
        self.sub_caveat_label.setVisible(any_caveat)
        self.tracks_table.resizeColumnsToContents()
