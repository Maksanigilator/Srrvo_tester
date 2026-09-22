"""Тёмная тема и общие цвета интерфейса."""

import pyqtgraph as pg
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

C_BG = "#232629"
C_BASE = "#1b1e20"
C_TEXT = "#dcdcdc"
C_DIM = "#8a9199"
C_ACCENT = "#4ea1ff"
C_OK = "#5cb85c"
C_ERR = "#ef5350"
C_WARN = "#e0a030"

CONSOLE_COLORS = {">": "#7fc27f", "<": C_ACCENT, "error": C_ERR, "info": C_DIM}
MARKERS = {">": "→", "<": "←", "error": "!", "info": "·"}


def apply_dark_theme(app: QApplication) -> None:
    """Fusion плюс тёмная палитра: одинаково выглядит на всех платформах."""
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(C_BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(C_TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(C_BASE))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(C_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(C_TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor("#31363b"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(C_TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(C_ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#101010"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(C_BASE))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(C_TEXT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
                     QColor("#5a5f65"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
                     QColor("#5a5f65"))
    app.setPalette(palette)
    # Палитры мало: Fusion рисует подсказки системным жёлтым, пока не задан
    # стиль именно для QToolTip.
    app.setStyleSheet(f"""
        QToolTip {{
            background-color: {C_BASE};
            color: {C_TEXT};
            border: 1px solid #454b52;
            padding: 6px;
        }}
    """)
    pg.setConfigOptions(antialias=True, background=C_BASE, foreground=C_DIM)
