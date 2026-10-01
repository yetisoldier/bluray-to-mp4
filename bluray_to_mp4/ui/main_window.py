"""Main window: sidebar navigation, cards, browse-for-folder, previews,

cancel, ETA, history. Design system: apply_theme() from theme.py.
Pages are managed by the sidebar index (0=Setup, 1=Source, 2=Review,
3=Encode, 4=History).
"""

from __future__ import annotations

import os
import webbrowser

from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
from ..makemkv import MakeMkvError
from ..models import Disc
from ..monitor import lan_ip, start_server, stop_server
from ..naming import movie_output_name
from ..recommend import recommend_title
from ..series import detect_series, episode_output_names, main_feature
from ..tmdb import lookup_movie, season_episode_titles
from .workers import FetchWorker, RunWorker, ScanWorker
from .widgets import card_frame, muted, section, EtaProgressBar

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
        # DriveEntry list backing the drives table rows; row != MakeMKV
        # disc index once status-256 drives are filtered out (C7).
        self._drives: list = []

        self.scan_worker: ScanWorker | None = None
        self.run_worker: RunWorker | None = None
        self.fetch_worker: FetchWorker | None = None

        self.setWindowTitle("bluray-to-mp4")
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
        brand = QLabel("bluray-to-mp4")
        brand.setObjectName("brand")
        accent = QLabel("•")
        accent.setObjectName("brandAccent")
        brand_row.addWidget(brand)
        brand_row.addWidget(accent)
        brand_row.addStretch()
        nav_layout.addLayout(brand_row)

        brand_sub = QLabel("Local disc → MP4")
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
        self.nav_list.setCurrentRow(1)  # Source is the default page
        self.nav_list.currentRowChanged.connect(self._nav_changed)
        nav_layout.addWidget(self.nav_list)
        nav_layout.addStretch()
        layout.addWidget(navbar, stretch=0)

        # --- main column -------------------------------------------------
        column = QWidget()
        content_layout = QVBoxLayout(column)
        content_layout.setContentsMargins(12, 8, 12, 8)
        content_layout.setSpacing(10)

        # Header: current page title + monitor status pill
        header_row = QHBoxLayout()
        self.page_title = QLabel("Source")
        self.page_title.setObjectName("h1")
        header_row.addWidget(self.page_title)
        header_row.addStretch()
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
            "About bluray-to-mp4",
            f"bluray-to-mp4 v{bluray_to_mp4.__version__}\n\n"
            "Local disc-to-MP4 ripper with recommendations and monitoring.\n"
            "Requires: MakeMKV, ffmpeg/ffprobe (bundled when built with CI).\n\n"
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
        self.btn_setup_continue = QPushButton("Continue anyway")
        self.btn_setup_continue.setObjectName("ghostBtn")
        self.btn_setup_continue.clicked.connect(lambda: self._nav_to(1))
        row.addWidget(self.btn_setup_recheck)
        row.addWidget(self.btn_setup_continue)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        self.stack.addWidget(page)
        self.pages.append(page)

    def _show_deps_if_needed(self) -> None:
        checks = check_all()
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
        if missing:
            self._nav_to(0)
        elif self.stack.currentIndex() == 0:
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
            "Commentary and descriptive tracks are skipped. Override any choice if needed."))
        self.tracks_table = QTableWidget(0, 4)
        self.tracks_table.setHorizontalHeaderLabels(
            ["Title", "Length", "Recommended audio", "Recommended subtitles"])
        self.tracks_table.horizontalHeader().setStretchLastSection(True)
        self.tracks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        rl.addWidget(self.tracks_table)
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
        cl = QVBoxLayout(card)
        cl.setSpacing(10)
        cl.addWidget(section("Current"))
        self.progress_current = QLabel("Waiting to start ...")
        self.progress_current.setObjectName("h1")
        cl.addWidget(self.progress_current)
        self.progress_stage = QLabel("")
        self.progress_stage.setObjectName("muted")
        self.progress_stage.setWordWrap(True)
        cl.addWidget(self.progress_stage)

        self.overall_row = card_frame()
        overall_layout = QVBoxLayout(self.overall_row)
        overall_layout.setSpacing(6)
        overall_layout.addWidget(QLabel("Overall (queue)"))
        self.overall_bar = EtaProgressBar()
        overall_layout.addWidget(self.overall_bar)
        self.stage_row = card_frame()
        stage_layout = QVBoxLayout(self.stage_row)
        stage_layout.setSpacing(6)
        stage_layout.addWidget(QLabel("Current stage"))
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
        self.history_table = QTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(
            ["Output", "Type", "Duration", "Notes"])
        self.history_table.horizontalHeader().setStretchLastSection(True)
        cl.addWidget(self.history_table)
        btn_row = QHBoxLayout()
        self.btn_refresh_history = QPushButton("Refresh")
        self.btn_refresh_history.clicked.connect(self._refresh_history)
        btn_row.addWidget(self.btn_refresh_history)
        btn_row.addStretch()
        cl.addLayout(btn_row)
        layout.addWidget(self.history_table)
        history_label = QLabel("")
        layout.addWidget(history_label)
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
            self.history_table.setItem(row, 0, QTableWidgetItem(name))
            self.history_table.setItem(row, 1, QTableWidgetItem(rec.kind))
            self.history_table.setItem(row, 2, QTableWidgetItem(duration))
            self.history_table.setItem(row, 3, QTableWidgetItem(warnings))
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
            "title_overrides": self._title_overrides,
            "year": self.spin_year.value() or None,
            "want_subtitles": self.check_subs.isChecked(),
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

        self.run_worker = RunWorker(self.disc.index, plan, self.settings)
        self.run_worker.stage.connect(self._run_stage)
        self.run_worker.progress.connect(self._run_progress)
        self.run_worker.done.connect(self._run_done)
        self.run_worker.failed.connect(self._run_failed)
        self.run_worker.start()

    def _cancel_run(self) -> None:
        if not self.run_worker or not self.run_worker.isRunning():
            return
        self.run_worker.cancel()
        self.btn_cancel.setEnabled(False)
        self.progress_stage.setText("Cancelling ...")

    def _run_stage(self, msg: str) -> None:
        self.progress_stage.setText(msg)
        self.log_text.setText(msg)

    def _run_progress(self, job_name: str, tid: int, frac: float, eta: float | None) -> None:
        self.progress_current.setText(job_name)
        self.stage_bar.set_value(frac, eta)
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
        self.edit_tmdb_key.setText(self.settings.tmdb_key or "")
        self._episode_titles = {}
        self._title_overrides = {}
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
        for row, t in enumerate(titles):
            sel = recommend_title(t)
            m, s = divmod(t.duration_seconds, 60)
            self.tracks_table.setItem(row, 0, QTableWidgetItem(
                t.title_name or f"Title {t.tid}"))
            self.tracks_table.setItem(row, 1, QTableWidgetItem(f"{m}:{s:02d}"))
            audio = next(
                (a for a in t.streams_of("audio") if a.sid == sel.audio_sid), None)
            audio_text = ""
            if audio is not None:
                audio_text = (
                    f"{audio.lang_name or '?'} "
                    f"{audio.codec_long or audio.codec} ({audio.channels}ch)")
            self.tracks_table.setItem(row, 2, QTableWidgetItem(audio_text))
            sub = next(
                (x for x in t.streams_of("subtitle")
                 if x.sid == sel.subtitle_sid), None)
            sub_text = ""
            if sub is not None:
                sub_text = f"{sub.lang_name or 'English'} ({sub.name or 'PGS'})"
            self.tracks_table.setItem(row, 3, QTableWidgetItem(sub_text))
        self.tracks_table.resizeColumnsToContents()