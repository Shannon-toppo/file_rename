# -*- coding: utf-8 -*-
"""Qt 標準 UI の日本語化（gui/i18n.py）のテスト。

テキスト入力欄の右クリックメニューは Qt が持つ文言なので、qtbase_ja.qm を
読み込めているかどうかで日本語/英語が決まる。ネットワークは使わない。
"""
from PySide6.QtWidgets import QLineEdit, QPlainTextEdit

from gui.i18n import install_qt_translators, translation_dirs


def test_translation_dirs_exist():
    # 少なくとも 1 つは実在し、qtbase_ja.qm を含むこと
    assert any((d / "qtbase_ja.qm").exists() for d in translation_dirs())


def test_context_menu_is_japanese(qapp):
    translators = install_qt_translators(qapp)
    try:
        assert translators, "qtbase_ja.qm を読み込めていない"
        for widget in (QLineEdit(), QPlainTextEdit()):
            texts = [a.text() for a in widget.createStandardContextMenu().actions()]
            joined = "".join(texts)
            assert "コピー" in joined and "貼り付け" in joined
            assert "Copy" not in joined and "Paste" not in joined
    finally:
        # 他テストへ影響させないよう外す
        for t in translators:
            qapp.removeTranslator(t)
