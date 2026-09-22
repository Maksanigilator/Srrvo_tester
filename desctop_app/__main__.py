"""Точка входа python -m desctop_app"""

import sys

from PySide6.QtWidgets import QApplication

from .ui.main_window import MainWindow
from .ui.theme import apply_dark_theme


def main() -> int:
    app = QApplication(sys.argv)
    # без имени приложения QStandardPaths не знает, куда класть его данные.
    # Имя организации не задаём: Qt подставило бы его в путь ещё одним уровнем.
    app.setApplicationName("ServoTester")
    apply_dark_theme(app)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
