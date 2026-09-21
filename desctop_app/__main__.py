"""Точка входа python -m desctop_app"""

import sys

from PySide6.QtWidgets import QApplication

from .ui.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
