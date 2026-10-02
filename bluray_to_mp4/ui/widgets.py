"""Shared UI widgets: section headers, progress bar with ETA and
indeterminate mode, activity dots, history table."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


def section(title: str) -> QLabel:
    lab = QLabel(title)
    lab.setObjectName("h2")
    return lab


def muted(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


def card_frame() -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    return frame


class ActivityDots(QLabel):
    """Three moving dots: a pure 'the app is alive' indicator that needs
    no progress data at all. BD+ decryption can sit at 0% for many
    minutes (first real-disc validation, 2026-10-02); the dots keep
    moving the whole time so the screen never looks frozen."""

    _FRAMES = ("\u2022 \u00b7 \u00b7", "\u00b7 \u2022 \u00b7", "\u00b7 \u00b7 \u2022", "\u00b7 \u2022 \u00b7")

    def __init__(self, parent=None):
        super().__init__("\u00b7 \u00b7 \u00b7", parent)
        self.setObjectName("activityDots")
        f = self.font()
        if f.pointSize() > 0:
            f.setPointSize(max(f.pointSize(), 14))
        else:
            f.setPixelSize(max(f.pixelSize(), 16))
        self.setFont(f)
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self.setText(self._FRAMES[self._frame])

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.setText("\u00b7 \u00b7 \u00b7")


class EtaProgressBar(QWidget):
    """Progress bar plus an ETA line that updates itself.

    set_busy(True) switches the bar to Qt's indeterminate pulse and
    shows 'estimating ...' - used while a stage runs but no measurable
    progress exists yet (BD+ open). The first set_value with real data
    exits busy mode automatically."""

    def __init__(self, parent=None):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.eta_label = QLabel("")
        self.eta_label.setObjectName("muted")
        layout.addWidget(self.bar, stretch=1)
        layout.addWidget(self.eta_label)
        self._busy = False

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.bar.setRange(0, 0)  # 0..0 = Qt indeterminate pulse
            self.eta_label.setText("estimating ...")
        else:
            self.bar.setRange(0, 1000)
            if self.eta_label.text() == "estimating ...":
                self.eta_label.setText("")

    def set_value(self, fraction: float, eta_seconds: float | None = None):
        if self._busy and (eta_seconds is not None or fraction > 0.0):
            self.set_busy(False)  # real data arrived: leave pulse mode
        self.bar.setValue(int(max(0.0, min(fraction, 1.0)) * 1000))
        text = ""
        if eta_seconds is not None and eta_seconds > 1:
            m, s = divmod(int(eta_seconds), 60)
            text = f"ETA {m}:{s:02d}"
        self.eta_label.setText(text)

    def reset(self):
        self.set_busy(False)
        self.bar.setValue(0)
        self.eta_label.setText("")


__all__ = ["section", "muted", "card_frame", "EtaProgressBar", "ActivityDots"]