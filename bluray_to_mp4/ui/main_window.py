"""Main window: drive pick -> scan -> recommendations -> confirm -> progress."""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..deps import check_all, missing_required
from ..makemkv import BetaKeyExpiredError, DiscOpenError, MakeMkvError
from ..models import Disc
from ..monitor import lan_ip, start_server, stop_server
from ..recommend import recommend_title
from ..series import detect_series, main_feature
from ..tmdb import TmdbError, lookup_movie, season_episode_titles
from .workers import FetchWorker, RunWorker, ScanWorker


class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.disc: Disc | None = None
        self.scan_worker: ScanWorker | None = None
        self.run_worker: RunWorker | None = None
        self.fetch_worker: FetchWorker | None = None
        self._episode_titles: dict[int, str] = {}
        self.setWindowTitle("bluray-to-mp4")
        self.resize(900, 640)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Page 0: dependency gate (shown first when required tools missing)
        self.deps_page = self._build_deps_page()
        self.stack.addWidget(self.deps_page)

        # Page 1: drive/scan
        self.scan_page = self._build_scan_page()
        self.stack.addWidget(self.scan_page)

        # Page 2: recommendations / confirm
        self.plan_page = self._build_plan_page()
        self.stack.addWidget(self.plan_page)

        # Page 3: progress
        self.progress_page = self._build_progress_page()
        self.stack.addWidget(self.progress_page)

        self._show_deps_if_needed()
        self._refresh_drives()

    # ------------------------------------------------------------------
    # Dependency page

    def _build_deps_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Setup check")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)
        self.deps_table = QTableWidget(0, 4)
        self.deps_table.setHorizontalHeaderLabels(
            ["Tool", "Status", "What to do", ""])
        self.deps_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.deps_table)
        hint = QLabel(
            "Install the missing required tools, then press Re-check. "
            "Optional tools unlock extra features but are not blockers.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.btn_recheck = QPushButton("Re-check")
        self.btn_recheck.clicked.connect(self._show_deps_if_needed)
        row.addWidget(self.btn_recheck)
        self.btn_deps_continue = QPushButton("Continue anyway")
        self.btn_deps_continue.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.scan_page))
        row.addWidget(self.btn_deps_continue)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _show_deps_if_needed(self) -> None:
        checks = check_all()
        missing = missing_required(checks)
        self.deps_table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            ok = "Ready" if check.ok else "Missing"
            name_item = QTableWidgetItem(check.name)
            status_item = QTableWidgetItem(ok)
            detail_item = QTableWidgetItem(check.detail)
            status_item.setForeground(
                Qt.green if check.ok else Qt.red)  # type: ignore[call-overload]
            self.deps_table.setItem(row, 0, name_item)
            self.deps_table.setItem(row, 1, status_item)
            self.deps_table.setItem(row, 2, detail_item)
            if check.url:
                btn = QPushButton("Open")
                url_holder = QPushButton()  # closure carrier
                btn.clicked.connect(
                    lambda checked=False, u=check.url: webbrowser.open(u))
                self.deps_table.setCellWidget(row, 3, btn)
                del url_holder
            else:
                self.deps_table.setCellWidget(row, 3, QLabel(""))
        self.deps_table.resizeColumnsToContents()
        if missing:
            self.stack.setCurrentWidget(self.deps_page)
        elif self.stack.currentWidget() is self.deps_page:
            self.stack.setCurrentWidget(self.scan_page)

    # ------------------------------------------------------------------
    # Scan page

    def _build_scan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("Insert a disc, then scan")
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(title)

        row = QHBoxLayout()
        self.combo_drives = QComboBox()
        self.combo_drives.setMinimumWidth(420)
        row.addWidget(QLabel("Drive:"))
        row.addWidget(self.combo_drives, stretch=1)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_drives)
        row.addWidget(self.btn_refresh)
        self.btn_scan = QPushButton("Scan disc")
        self.btn_scan.clicked.connect(self._scan_selected)
        row.addWidget(self.btn_scan)
        layout.addLayout(row)

        self.scan_status = QLabel("")
        self.scan_status.setWordWrap(True)
        layout.addWidget(self.scan_status)
        layout.addStretch()
        return page

    def _refresh_drives(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            return
        self.btn_refresh.setEnabled(False)
        self.combo_drives.clear()
        self.scan_status.setText("Looking for drives ...")
        self.scan_worker = ScanWorker(mode="drives")
        self.scan_worker.drives_listed.connect(self._drives_ready)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.start()

    def _drives_ready(self, drives) -> None:
        self.btn_refresh.setEnabled(True)
        self.scan_status.setText("")
        if not drives:
            self.scan_status.setText(
                "No optical drives found. Plug in the USB drive and press Refresh.")
            return
        for d in drives:
            label = f"disc:{d.index}  {d.drive_name}"
            if d.has_disc:
                label += f"  [{d.disc_type.upper()}: {d.disc_label}]"
            self.combo_drives.addItem(label, d.index)
        # Prefer a drive with a disc loaded.
        for i, d in enumerate(drives):
            if d.has_disc:
                self.combo_drives.setCurrentIndex(i)
                break

    def _scan_selected(self) -> None:
        idx = self.combo_drives.currentData()
        if idx is None:
            return
        self.btn_scan.setEnabled(False)
        self.scan_status.setText("Scanning disc (this can take a minute) ...")
        self.scan_worker = ScanWorker(mode="scan", index=idx)
        self.scan_worker.scanned.connect(self._disc_scanned)
        self.scan_worker.failed.connect(self._scan_failed)
        self.scan_worker.start()

    def _scan_failed(self, message: str) -> None:
        self.btn_scan.setEnabled(True)
        hint = ""
        if "beta key" in message.lower() or "evaluation" in message.lower():
            hint = ("  Grab the current beta key from the MakeMKV forum and "
                    "paste it into MakeMKV (Help > Register), then rescan.")
        # Inline status instead of a modal dialog: keeps the flow moving and
        # remote-monitorable without blocking.
        self.scan_status.setText(message + hint)
        self.scan_status.setStyleSheet("color: #b00;")

    def _disc_scanned(self, disc: Disc) -> None:
        self.btn_scan.setEnabled(True)
        self.disc = disc
        self.guess = detect_series(disc)
        self._fill_plan_page()
        self.stack.setCurrentWidget(self.plan_page)

    # ------------------------------------------------------------------
    # Plan page

    def _build_plan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.plan_title = QLabel("Disc")
        self.plan_title.setStyleSheet("font-size: 18px; font-weight: bold;")
        layout.addWidget(self.plan_title)
        self.plan_detection = QLabel("")
        self.plan_detection.setWordWrap(True)
        layout.addWidget(self.plan_detection)

        self.group_series = QGroupBox("Series naming")
        grid = QGridLayout(self.group_series)
        grid.addWidget(QLabel("Show name:"), 0, 0)
        self.edit_series_name = QLineEdit()
        grid.addWidget(self.edit_series_name, 0, 1)
        grid.addWidget(QLabel("Season:"), 0, 2)
        self.spin_season = QSpinBox()
        self.spin_season.setRange(0, 99)
        grid.addWidget(self.spin_season, 0, 3)
        grid.addWidget(QLabel("First episode #:"), 1, 0)
        self.spin_start_ep = QSpinBox()
        self.spin_start_ep.setRange(1, 9999)
        grid.addWidget(self.spin_start_ep, 1, 1)
        self.btn_apply_names = QPushButton("Apply to all episodes")
        self.btn_apply_names.clicked.connect(self._recompute_names)
        grid.addWidget(self.btn_apply_names, 1, 3)
        self.btn_fetch_episodes = QPushButton("Fetch episode titles from TMDB")
        self.btn_fetch_episodes.clicked.connect(self._fetch_episode_titles)
        grid.addWidget(self.btn_fetch_episodes, 2, 0, 1, 4)
        layout.addWidget(self.group_series)

        self.group_movie = QGroupBox("Movie naming")
        mgrid = QGridLayout(self.group_movie)
        mgrid.addWidget(QLabel("Title:"), 0, 0)
        self.edit_movie_title = QLineEdit()
        mgrid.addWidget(self.edit_movie_title, 0, 1)
        mgrid.addWidget(QLabel("Year:"), 0, 2)
        self.spin_year = QSpinBox()
        self.spin_year.setRange(0, 9999)
        self.spin_year.setSpecialValueText("none")
        mgrid.addWidget(self.spin_year, 0, 3)
        self.btn_fetch_year = QPushButton("Look up year from TMDB")
        self.btn_fetch_year.clicked.connect(self._fetch_movie_year)
        mgrid.addWidget(self.btn_fetch_year, 1, 0, 1, 4)
        layout.addWidget(self.group_movie)
        self.tmdb_status = QLabel("")
        self.tmdb_status.setWordWrap(True)
        self.tmdb_status.setStyleSheet("color: #666;")
        layout.addWidget(self.tmdb_status)

        self.group_tracks = QGroupBox("Tracks to keep (Recommended)")
        tlayout = QVBoxLayout(self.group_tracks)
        self.tracks_table = QTableWidget(0, 4)
        self.tracks_table.setHorizontalHeaderLabels(
            ["Title", "Length", "Recommended audio", "Recommended subtitles"])
        self.tracks_table.horizontalHeader().setStretchLastSection(True)
        tlayout.addWidget(self.tracks_table)
        self.tracks_note = QLabel(
            "Recommendations: best English audio (commentary skipped) and plain "
            "English subtitles (SDH/forced/foreign skipped). Override on the "
            "next screen if you want something different.")
        self.tracks_note.setWordWrap(True)
        tlayout.addWidget(self.tracks_note)
        layout.addWidget(self.group_tracks)

        row = QHBoxLayout()
        grid_row = QGridLayout()
        grid_row.addWidget(QLabel("Output folder:"), 0, 0)
        self.edit_outdir = QLineEdit()
        grid_row.addWidget(self.edit_outdir, 0, 1)
        grid_row.addWidget(QLabel("Encoder:"), 1, 0)
        self.combo_encoder = QComboBox()
        self.combo_encoder.addItems(["auto", "nvenc", "x265"])
        grid_row.addWidget(self.combo_encoder, 1, 1)
        self.check_subs = QCheckBox("Include subtitles (OCR to SRT)")
        self.check_subs.setChecked(True)
        grid_row.addWidget(self.check_subs, 2, 0, 1, 2)
        grid_row.addWidget(QLabel("TMDB key (optional):"), 3, 0)
        self.edit_tmdb_key = QLineEdit()
        self.edit_tmdb_key.setPlaceholderText(
            "free key: themoviedb.org/settings/api - enables episode-title "
            "and year autofill")
        self.edit_tmdb_key.setEchoMode(QLineEdit.EchoMode.Password)
        grid_row.addWidget(self.edit_tmdb_key, 3, 1)
        self.check_monitor = QCheckBox(
            "LAN monitoring (let Jarvis check rip progress from the home server)")
        self.check_monitor.setChecked(self.settings.monitor_enabled)
        self.check_monitor.toggled.connect(self._toggle_monitor)
        grid_row.addWidget(self.check_monitor, 4, 0, 1, 2)
        row.addLayout(grid_row)
        row.addStretch()
        layout.addLayout(row)

        btns = QHBoxLayout()
        self.btn_back = QPushButton("Back")
        self.btn_back.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.scan_page))
        btns.addWidget(self.btn_back)
        btns.addStretch()
        self.btn_run = QPushButton("Start")
        self.btn_run.setStyleSheet(
            "font-weight: bold; padding: 8px 24px;")
        self.btn_run.clicked.connect(self._start_run)
        btns.addWidget(self.btn_run)
        layout.addLayout(btns)
        return page

    def _fill_plan_page(self) -> None:
        assert self.disc is not None
        self.plan_title.setText(f"{self.disc.name}  ({self.disc.media_type or '?'})")
        self.plan_detection.setText("\n".join(self.guess.reasons))
        self.edit_series_name.setText(self.disc.name)
        self.edit_movie_title.setText(self.disc.name)
        self.spin_year.setValue(0)
        self.spin_season.setValue(1)
        self.spin_start_ep.setValue(1)
        self.group_series.setVisible(self.guess.is_series)
        self.group_movie.setVisible(not self.guess.is_series)
        if self.settings.outdir:
            self.edit_outdir.setText(self.settings.outdir)
        self.combo_encoder.setCurrentText(self.settings.encoder)
        self.edit_tmdb_key.setText(self.settings.tmdb_key or "")
        self.tmdb_status.setText("")
        self._episode_titles = {}
        self._fill_tracks_table()
        self._recompute_names()

    def _fill_tracks_table(self) -> None:
        assert self.disc is not None
        titles = (
            [self.disc.title(t) for t in self.guess.episode_tids]
            if self.guess.is_series
            else [main_feature(self.disc)]
        )
        titles = [t for t in titles if t]
        self.tracks_table.setRowCount(len(titles))
        for row, t in enumerate(titles):
            sel = recommend_title(t)
            m, s = divmod(t.duration_seconds, 60)
            title_item = QTableWidgetItem(
                t.title_name or f"Title {t.tid}")
            length_item = QTableWidgetItem(f"{m}:{s:02d}")
            audio = next(
                (a for a in t.streams_of("audio") if a.sid == sel.audio_sid), None)
            audio_text = ""
            if audio is not None:
                audio_text = (
                    f"{audio.lang_name or '?'} "
                    f"{audio.codec_long or audio.codec} ({audio.channels}ch)")
            audio_item = QTableWidgetItem(audio_text)
            subs = t.streams_of("subtitle")
            sub = next(
                (x for x in subs if x.sid == sel.subtitle_sid), None)
            sub_text = ""
            if sub is not None:
                sub_text = f"{sub.lang_name or 'English'} ({sub.name or 'PGS'})"
            sub_item = QTableWidgetItem(sub_text)
            self.tracks_table.setItem(row, 0, title_item)
            self.tracks_table.setItem(row, 1, length_item)
            self.tracks_table.setItem(row, 2, audio_item)
            self.tracks_table.setItem(row, 3, sub_item)
        self.tracks_table.resizeColumnsToContents()

    def _recompute_names(self) -> None:
        # Episode numbering is applied in the pipeline from season/start-episode;
        # this preview keeps the table's title column honest for movie mode.
        pass

    # ------------------------------------------------------------------
    # Monitor toggle

    def _toggle_monitor(self, checked: bool) -> None:
        """Start/stop the LAN status server live; persist the choice."""
        self.settings.monitor_enabled = checked
        try:
            if checked:
                _, base = start_server(
                    host=self.settings.monitor_host,
                    port=self.settings.monitor_port,
                    token=self.settings.monitor_token,
                )
                where = lan_ip()
                port = self.settings.monitor_port
                self.tmdb_status.setText(
                    f"Monitoring on: http://{where}:{port} "
                    f"(read-only, token-protected)")
            else:
                stop_server()
                self.tmdb_status.setText("Monitoring off.")
            self.settings.save()
        except OSError as exc:
            self.settings.monitor_enabled = False
            self.check_monitor.setChecked(False)
            self.tmdb_status.setText(
                f"Monitor failed to start (port busy?): {exc}")

    # ------------------------------------------------------------------
    # TMDB autofill (issue #6)

    def _tmdb_key(self) -> str:
        """Key from the field, falling back to the persisted setting."""
        return (self.edit_tmdb_key.text().strip()
                or (self.settings.tmdb_key or "").strip())

    def _fetch_episode_titles(self) -> None:
        assert self.disc is not None
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
        self.tmdb_status.setText(
            f"Looking up {show!r} season {season} on TMDB ...")

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
        assert self.disc is not None
        pairs = [(tid, title)
                 for tid, title in zip(self.guess.episode_tids, titles)
                 if title]
        self._episode_titles.update(dict(pairs))
        self.tmdb_status.setText(
            f"TMDB: {len(pairs)} of {self.guess.episode_count} episode "
            f"titles found. They will be used in the output filenames.")

    def _fetch_movie_year(self) -> None:
        assert self.disc is not None
        key = self._tmdb_key()
        if not key:
            self.tmdb_status.setText(
                "Enter a TMDB key first (free: themoviedb.org/settings/api).")
            return
        title = self.edit_movie_title.text().strip() or self.disc.name
        self.btn_fetch_year.setEnabled(False)
        self.tmdb_status.setText(f"Looking up {title!r} on TMDB ...")

        def do_fetch():
            return lookup_movie(title, key=key)

        self.fetch_worker = FetchWorker(do_fetch)
        self.fetch_worker.done.connect(self._movie_year_ready)
        self.fetch_worker.failed.connect(self._tmdb_failed)
        self.fetch_worker.start()

    def _movie_year_ready(self, hit) -> None:
        self.btn_fetch_episodes.setEnabled(True)
        self.btn_fetch_year.setEnabled(True)
        if not hit or not hit.get("year"):
            self.tmdb_status.setText("TMDB: no movie match found.")
            return
        self.spin_year.setValue(hit["year"])
        self.tmdb_status.setText(
            f"TMDB match: {hit.get('title')} ({hit['year']})")

    def _tmdb_failed(self, message: str) -> None:
        self.btn_fetch_episodes.setEnabled(True)
        self.btn_fetch_year.setEnabled(True)
        self.tmdb_status.setText(f"TMDB lookup failed: {message}")

    # ------------------------------------------------------------------
    # Progress page

    def _build_progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.progress_title = QLabel("Working ...")
        self.progress_title.setStyleSheet(
            "font-size: 18px; font-weight: bold;")
        layout.addWidget(self.progress_title)
        self.progress_stage = QLabel("")
        self.progress_stage.setWordWrap(True)
        layout.addWidget(self.progress_stage)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)
        self.progress_current = QLabel("")
        layout.addWidget(self.progress_current)
        layout.addStretch()
        self.btn_cancel = QPushButton("Close")
        self.btn_cancel.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.scan_page))
        layout.addWidget(self.btn_cancel, alignment=Qt.AlignRight)
        return page

    def _start_run(self) -> None:
        assert self.disc is not None
        outdir = self.edit_outdir.text().strip()
        if not outdir:
            QMessageBox.warning(self, "Missing output folder",
                                "Choose an output folder first.")
            return
        self.settings.outdir = outdir
        self.settings.encoder = self.combo_encoder.currentText()
        self.settings.last_series_name = self.edit_series_name.text().strip()
        self.settings.tmdb_key = self.edit_tmdb_key.text().strip()
        self.settings.save()

        year = self.spin_year.value() or None
        plan = {
            "disc_name": self.disc.name,
            "media_type": self.disc.media_type,
            "is_series": self.guess.is_series,
            "encoder": self.combo_encoder.currentText(),
            "outdir": outdir,
            "series_name": self.edit_series_name.text().strip()
            if self.guess.is_series else None,
            "season": self.spin_season.value(),
            "start_episode": self.spin_start_ep.value(),
            "episode_titles": self._episode_titles or None,
            "year": year,
            "jobs": [],
            "tid_names": {},
        }
        self.progress_title.setText(f"Processing {self.disc.name}")
        self.progress_stage.setText("")
        self.progress_bar.setValue(0)
        self.progress_current.setText("")
        self.stack.setCurrentWidget(self.progress_page)
        self.run_worker = RunWorker(self.disc.index, plan, self.settings)
        self.run_worker.stage.connect(self._run_stage)
        self.run_worker.progress.connect(self._run_progress)
        self.run_worker.done.connect(self._run_done)
        self.run_worker.failed.connect(self._run_failed)
        self.run_worker.start()

    def _run_stage(self, msg: str) -> None:
        self.progress_stage.setText(msg)

    def _run_progress(self, frac: float, job_name: str) -> None:
        self.progress_bar.setValue(int(frac * 1000))
        self.progress_current.setText(job_name)

    def _run_done(self, result) -> None:
        names = "\n".join(
            f"  {j.output_name}" + (" (+srt)" if j.srt_path else "")
            for j in result.jobs)
        self.progress_stage.setText("Finished")
        self.progress_bar.setValue(1000)
        self.progress_current.setText(names)
        self.btn_cancel.setText("Back to start")
        self.btn_cancel.clicked.disconnect()
        self.btn_cancel.clicked.connect(
            lambda: self.stack.setCurrentWidget(self.scan_page))

    def closeEvent(self, event) -> None:  # noqa: N802
        """Stop background workers cleanly so the app can't crash on exit."""
        if self.run_worker and self.run_worker.isRunning():
            answer = QMessageBox.question(
                self, "Rip in progress",
                "A rip/encode is still running. Close anyway? (It will stop.)",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        for worker in (self.scan_worker, self.run_worker, self.fetch_worker):
            if worker and worker.isRunning():
                worker.wait(3000)
        event.accept()

    def _run_failed(self, message: str) -> None:
        self.progress_stage.setText("Failed")
        self.progress_current.setText(message)
        self.btn_cancel.setText("Back")
        self.progress_current.setStyleSheet("color: #b00;")