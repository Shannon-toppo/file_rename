# -*- coding: utf-8 -*-
"""GUI テスト用の共通設定。

Qt をヘッドレス（オフスクリーン）で動かすため、import より前に
QT_QPA_PLATFORM を設定する。この行はファイル最上部に置くこと。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402  （QT_QPA_PLATFORM の設定より後に import する）
from PySide6.QtCore import QCoreApplication  # noqa: E402


@pytest.fixture
def main_window(qtbot):
    """MainWindow を生成し、テスト後に確実に破棄するフィクスチャ。

    複数テストで MainWindow を作ると、qtbot の遅延破棄だけでは C++ 側の
    ウィジェットが残り、後続テストの pytest-qt のイベント処理で破棄途中の
    オブジェクトに触れてアクセス違反することがある。ここで close →
    deleteLater → イベント処理まで行い、境界で完全に解放する。
    QSettings は汚さないよう restore_settings=False で作る（ffmpeg/deno の
    警告モーダルもこれで抑止される。CI での 6 時間ハングの原因だった）。
    """
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    yield win
    win.close()
    win.deleteLater()
    app = QCoreApplication.instance()
    if app is not None:
        app.processEvents()
