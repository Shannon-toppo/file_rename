# -*- coding: utf-8 -*-
"""GUI のエントリポイント: `uv run python -m gui` で起動する。"""
import sys

from PySide6.QtWidgets import QApplication

from .i18n import install_qt_translators
from .main_window import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    # Qt 標準の文言（テキスト欄の右クリックメニュー等）を日本語にする
    install_qt_translators(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
