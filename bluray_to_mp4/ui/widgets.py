"""Shared UI widgets: section headers, progress bar with ETA, history table."""

from __future__ import annotations

from PySide6.QtCore import Qt
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


class EtaProgressBar(QWidget):
    """Progress bar plus an ETA line that updates itself."""

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

    def set_value(self, fraction: float, eta_seconds: float | None = None):
        self.bar.setValue(int(max(0.0, min(fraction, 1.0) * 1000)))
        text = ""
        if eta_seconds is not None and eta_seconds > 1:
            m, s = divmod(int(eta_seconds), 60)
            text = f"ETA {m}:{s:02d}"
        self.eta_label.setText(text)

    def reset(self):
        self.bar.setValue(0)
        self.eta_label.setText("")


__all__ = ["section", "muted", "card_frame", "EtaProgressBar"]