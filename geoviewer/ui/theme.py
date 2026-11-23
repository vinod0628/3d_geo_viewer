"""Dark theme: Fusion style, a dark palette and a stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BG = "#1b1d21"
PANEL = "#22252a"
PANEL_ALT = "#282c32"
INPUT = "#2d3138"
BORDER = "#363b43"
TEXT = "#e3e6ea"
MUTED = "#9aa3ad"
ACCENT = "#3d9bff"
ACCENT_DIM = "#24476f"
HOVER = "#323740"
DANGER = "#ff6b6b"

STYLESHEET = f"""
* {{ font-family: "Segoe UI"; font-size: 9pt; color: {TEXT}; }}
QMainWindow, QWidget#central {{ background: {BG}; }}
QWidget#leftPanel {{ background: {PANEL}; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background: {ACCENT_DIM}; }}

QLabel#panelTitle {{
    color: {MUTED}; font-size: 8pt; font-weight: 600; letter-spacing: 1px;
    padding: 2px 0px;
}}
QFrame#panelHeader {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}

QTreeWidget, QListWidget {{
    background: {PANEL}; border: none; outline: 0;
    show-decoration-selected: 1;
}}
QTreeWidget::item, QListWidget::item {{ height: 26px; padding-left: 2px; border-radius: 4px; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {HOVER}; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: {ACCENT_DIM}; color: {TEXT}; }}
QTreeWidget::indicator, QCheckBox::indicator {{ width: 14px; height: 14px; }}

QScrollArea {{ background: {PANEL}; border: none; }}
QScrollArea > QWidget > QWidget {{ background: {PANEL}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #454b54; border-radius: 4px; min-height: 24px; margin: 2px; }}
QScrollBar::handle:vertical:hover {{ background: #5a616b; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #454b54; border-radius: 4px; min-width: 24px; margin: 2px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 6px; margin-top: 14px; padding: 8px 6px 6px 6px;
    background: {PANEL_ALT};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {MUTED};
    font-size: 8pt; font-weight: 600;
}}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background: {INPUT}; border: 1px solid {BORDER}; border-radius: 4px;
    padding: 3px 6px; min-height: 18px; selection-background-color: {ACCENT_DIM};
}}
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover, QLineEdit:hover {{ border-color: #4a515b; }}
QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {INPUT}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_DIM};
}}

QPushButton {{
    background: {INPUT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 10px;
}}
QPushButton:hover {{ background: {HOVER}; border-color: #4a515b; }}
QPushButton:pressed {{ background: {ACCENT_DIM}; }}
QPushButton#colorButton {{ min-width: 46px; min-height: 18px; padding: 2px; }}

QToolButton#headerButton {{
    background: transparent; border: none; border-radius: 4px; padding: 3px;
}}
QToolButton#headerButton:hover {{ background: {HOVER}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT}; width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
}}

QToolTip {{
    background: {PANEL_ALT}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px;
    border-radius: 4px;
}}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 6px; }}
QMenu {{ background: {PANEL_ALT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; }}
QMessageBox {{ background: {PANEL}; }}

/* Floating toolbar */
QFrame#floatingBar {{
    background: rgba(34, 37, 42, 235); border: 1px solid {BORDER}; border-radius: 12px;
}}
QFrame#floatingBar QToolButton {{
    background: transparent; border: none; border-radius: 8px; padding: 0px;
    min-width: 36px; max-width: 36px; min-height: 34px; max-height: 34px;
}}
QFrame#floatingBar QToolButton:hover {{ background: {HOVER}; }}
QFrame#floatingBar QToolButton:checked {{ background: {ACCENT_DIM}; }}
QFrame#floatingBar QToolButton:disabled {{ background: transparent; }}
QFrame#barSeparator {{ background: {BORDER}; min-height: 1px; max-height: 1px; margin: 3px 6px; }}
QLabel#barGrip {{ padding: 2px; }}

/* Identify result panel */
QFrame#identifyPanel {{
    background: rgba(30, 33, 38, 242); border: 1px solid {ACCENT_DIM}; border-radius: 10px;
}}
QLabel#identifyTitle {{ color: {TEXT}; font-weight: 600; }}
QLabel#identifySub {{ color: {MUTED}; font-size: 8pt; }}
QScrollArea#identifyScroll, QScrollArea#identifyScroll > QWidget > QWidget {{ background: transparent; }}
QLabel#identifyBody {{ background: transparent; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = QPalette()
    c = QColor
    pal.setColor(QPalette.Window, c(BG))
    pal.setColor(QPalette.WindowText, c(TEXT))
    pal.setColor(QPalette.Base, c(PANEL))
    pal.setColor(QPalette.AlternateBase, c(PANEL_ALT))
    pal.setColor(QPalette.ToolTipBase, c(PANEL_ALT))
    pal.setColor(QPalette.ToolTipText, c(TEXT))
    pal.setColor(QPalette.Text, c(TEXT))
    pal.setColor(QPalette.Button, c(INPUT))
    pal.setColor(QPalette.ButtonText, c(TEXT))
    pal.setColor(QPalette.BrightText, c(DANGER))
    pal.setColor(QPalette.Highlight, c(ACCENT_DIM))
    pal.setColor(QPalette.HighlightedText, c(TEXT))
    pal.setColor(QPalette.Link, c(ACCENT))
    pal.setColor(QPalette.PlaceholderText, c(MUTED))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, c("#5f666f"))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)
