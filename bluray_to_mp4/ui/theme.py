"""Visual theme for the desktop app.

Dark is the default. One palette object feeds one QSS stylesheet; nothing
in the UI hardcodes colors, so light mode lands for free by switching the
palette. Widget-specific variants are selected by objectName conventions:

  objectName="primaryBtn"  - filled accent call-to-action
  objectName="ghostBtn"    - borderless secondary button
  objectName="card"        - QFrame/QGroupBox panel with border + radius
  objectName="runCard"     - Encode page's active-operation panel
  objectName="progressPanel" - compact progress lane inside runCard
  objectName="navBar"      - the left navigation column
  objectName="muted"       - secondary text
  objectName="statusPill"  - header status chip (monitor on/off)
  objectName="brand"       - application wordmark
  objectName="h1"/"h2"     - page headings
"""

from __future__ import annotations

__all__ = ["apply_theme", "THEMES", "DEFAULT_THEME"]

DEFAULT_THEME = "dark"
THEMES = ("dark", "light")

_PALETTES = {
    "dark": dict(
        bg="#0E1014",
        panel="#161A22",
        card="#1B212C",
        card_border="#2A3140",
        card_border_strong="#394457",
        text="#E9ECF2",
        text_muted="#9AA3B4",
        accent="#6366F1",
        accent_hover="#7C7FF6",
        accent_soft="#222A55",
        accent_2="#22D3EE",
        accent_text="#FFFFFF",
        ok="#34D399",
        warn="#FBBF24",
        err="#F87171",
        input_bg="#10141B",
        row_alt="#161B25",
        progress_bg="#0B0F16",
    ),
    "light": dict(
        bg="#F6F7FA",
        panel="#FFFFFF",
        card="#FFFFFF",
        card_border="#DDE1EA",
        card_border_strong="#C7CEDB",
        text="#191D27",
        text_muted="#5B6472",
        accent="#4F46E5",
        accent_hover="#6366F1",
        accent_soft="#EEF2FF",
        accent_2="#0891B2",
        accent_text="#FFFFFF",
        ok="#059669",
        warn="#B45309",
        err="#DC2626",
        input_bg="#FFFFFF",
        row_alt="#F2F4F9",
        progress_bg="#EEF1F7",
    ),
}

_FONT = 'Segoe UI, "SF Pro Display", Ubuntu, "Helvetica Neue", sans-serif'


def _qss(p: dict) -> str:
    return f"""
* {{
  font-family: {_FONT};
  font-size: 13px;
  color: {p['text']};
  selection-background-color: {p['accent']};
  selection-color: {p['accent_text']};
}}
QMainWindow, QWidget#root {{ background: {p['bg']}; }}
QToolTip {{
  background: {p['card']}; color: {p['text']};
  border: 1px solid {p['card_border']}; padding: 6px 8px;
  border-radius: 6px;
}}

/* ---------- header / navigation ---------- */
QFrame#navBar {{
  background: {p['panel']};
  border: none;
  border-right: 1px solid {p['card_border']};
}}
QLabel#brand {{
  font-size: 18px;
  font-weight: 800;
  color: {p['text']};
}}
QLabel#brandAccent {{ color: {p['accent']}; font-size: 17px; }}
QLabel#brandSub {{
  color: {p['text_muted']};
  font-size: 11px;
}}
QListWidget#navList {{
  background: transparent;
  border: none;
  outline: 0;
  font-size: 13px;
}}
QListWidget#navList::item {{
  padding: 10px 12px;
  margin: 2px 8px;
  border-radius: 8px;
  color: {p['text_muted']};
}}
QListWidget#navList::item:hover {{
  background: {p['row_alt']};
  color: {p['text']};
}}
QListWidget#navList::item:selected {{
  background: {p['accent']};
  color: {p['accent_text']};
  font-weight: 600;
}}
QLabel#statusPill {{
  padding: 5px 11px;
  border-radius: 8px;
  background: {p['row_alt']};
  border: 1px solid {p['card_border']};
  color: {p['text_muted']};
  font-size: 11px;
}}

/* ---------- content ---------- */
QLabel#h1 {{ font-size: 22px; font-weight: 750; }}
QLabel#h2 {{ font-size: 15px; font-weight: 650; }}
QLabel#muted, QLabel {{ color: {p['text']}; }}
QLabel#muted {{ color: {p['text_muted']}; }}
QLabel#runTitle {{ font-size: 23px; font-weight: 800; color: {p['text']}; }}
QLabel#runSub {{ color: {p['text_muted']}; font-size: 12px; }}
QLabel#metricText {{
  padding: 5px 10px;
  border-radius: 8px;
  background: {p['row_alt']};
  border: 1px solid {p['card_border']};
  color: {p['text_muted']};
}}
QLabel#activityDots {{
  color: {p['accent_2']};
  font-weight: 800;
}}
QLabel#progressLabel {{
  color: {p['text_muted']};
  font-size: 11px;
  font-weight: 700;
}}

QGroupBox#card, QFrame#card {{
  background: {p['card']};
  border: 1px solid {p['card_border']};
  border-radius: 8px;
  padding: 14px;
  margin-top: 2px;
}}
QFrame#runCard {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                              stop:0 {p['card']}, stop:1 {p['panel']});
  border: 1px solid {p['card_border_strong']};
  border-radius: 8px;
  padding: 16px;
  margin-top: 2px;
}}
QFrame#progressPanel {{
  background: {p['input_bg']};
  border: 1px solid {p['card_border']};
  border-radius: 8px;
  padding: 10px;
  margin-top: 2px;
}}
QGroupBox#card {{
  font-weight: 600;
  font-size: 13px;
  padding-top: 26px;
}}
QGroupBox#card::title {{
  subcontrol-origin: margin;
  subcontrol-position: top left;
  padding: 6px 10px 0 10px;
  color: {p['text_muted']};
  font-weight: 600;
}}

/* ---------- controls ---------- */
QPushButton {{
  background: {p['row_alt']};
  border: 1px solid {p['card_border']};
  border-radius: 8px;
  padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {p['accent']}; }}
QPushButton:pressed {{ background: {p['card_border']}; }}
QPushButton:disabled {{
  color: {p['text_muted']};
  background: {p['card']};
  border-color: {p['card_border']};
}}
QPushButton#primaryBtn {{
  background: {p['accent']};
  color: {p['accent_text']};
  border: 1px solid {p['accent']};
  font-weight: 600;
  padding: 9px 22px;
}}
QPushButton#primaryBtn:hover {{ background: {p['accent_hover']}; }}
QPushButton#primaryBtn:disabled {{
  background: {p['card_border']};
  color: {p['text_muted']};
}}
QPushButton#ghostBtn {{
  background: transparent;
  border: 1px solid transparent;
  color: {p['text_muted']};
}}
QPushButton#ghostBtn:hover {{ color: {p['text']}; border-color: {p['card_border']}; }}
QPushButton#dangerBtn {{
  background: transparent;
  border: 1px solid {p['err']};
  color: {p['err']};
}}
QPushButton#dangerBtn:hover {{ background: {p['err']}; color: {p['accent_text']}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
  background: {p['input_bg']};
  border: 1px solid {p['card_border']};
  border-radius: 7px;
  padding: 6px 10px;
  min-height: 18px;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
  border-color: {p['accent']};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
  color: {p['text_muted']};
  background: {p['card']};
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
  background: {p['card']};
  border: 1px solid {p['card_border']};
  outline: 0;
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
  width: 17px; height: 17px;
  border-radius: 5px;
  border: 1px solid {p['card_border']};
  background: {p['input_bg']};
}}
QCheckBox::indicator:checked {{
  background: {p['accent']};
  border-color: {p['accent']};
}}

/* ---------- tables ---------- */
QTableWidget {{
  background: transparent;
  alternate-background-color: {p['row_alt']};
  gridline-color: transparent;
  border: none;
  outline: 0;
}}
QTableWidget::item {{ padding: 6px 8px; border: none; }}
QTableWidget::item:selected {{ background: {p['row_alt']}; color: {p['text']}; }}
QHeaderView::section {{
  background: transparent;
  color: {p['text_muted']};
  border: none;
  border-bottom: 1px solid {p['card_border']};
  padding: 6px 8px;
  font-weight: 600;
  text-align: left;
}}

/* ---------- progress ---------- */
QProgressBar {{
  background: {p['progress_bg']};
  border: 1px solid {p['card_border']};
  border-radius: 8px;
  height: 16px;
  text-align: center;
  color: {p['text_muted']};
  font-size: 11px;
}}
QProgressBar::chunk {{
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                              stop:0 {p['accent']}, stop:1 {p['accent_2']});
  border-radius: 7px;
}}

/* ---------- misc ---------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
  background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
  background: {p['card_border']}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {p['text_muted']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
  background: transparent; height: 10px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
  background: {p['card_border']}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QStatusBar {{
  background: {p['panel']};
  border-top: 1px solid {p['card_border']};
  color: {p['text_muted']};
}}
QSplitter::handle {{ background: transparent; width: 2px; }}
"""


def apply_theme(app, name: str = DEFAULT_THEME) -> None:
    """Apply a theme to the whole QApplication."""
    palette = _PALETTES.get(name, _PALETTES[DEFAULT_THEME])
    app.setStyleSheet(_qss(palette))


def color(name: str, key: str) -> str:
    """Palette lookup for inline uses (e.g. status pill variants)."""
    return _PALETTES.get(name, _PALETTES[DEFAULT_THEME])[key]