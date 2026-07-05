# -*- coding: utf-8 -*-
"""フェーズ 5 のオフラインテスト: 設定ダイアログ・ログパネル・縮退通知・ffmpeg 警告。"""
import logging
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

import core
from gui.settings_dialog import SettingsDialog


@pytest.fixture
def main_window(qtbot):
    """MainWindow を生成し、テスト後に確実に破棄する（test_gui_excel.py と同方針）。"""
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    yield win
    win.close()
    win.deleteLater()
    QApplication.processEvents()


# ---------------------------------------------------------------------------
# SettingsDialog
# ---------------------------------------------------------------------------


def test_settings_dialog_roundtrip(qtbot, tmp_path):
    dlg = SettingsDialog(
        out_dir=tmp_path,
        fmt="wav",
        batch_size=9,
        auto_write=False,
        expand_playlist=True,
        normalize=False,
        theme="dark",
        log_level="DEBUG",
    )
    qtbot.addWidget(dlg)
    assert dlg.values() == {
        "out_dir": tmp_path,
        "fmt": "wav",
        "batch_size": 9,
        "auto_write": False,
        "expand_playlist": True,
        "normalize": False,
        "theme": "dark",
        "log_level": "DEBUG",
    }


def test_settings_dialog_defaults(qtbot):
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    v = dlg.values()
    assert v["out_dir"] == core.FILES_DIR
    assert v["fmt"] == "mp3"
    assert v["batch_size"] == core.BATCH_SIZE
    assert v["auto_write"] is True
    assert v["expand_playlist"] is False  # 既定は現行どおり動画 1 本のみ
    assert v["normalize"] is True  # 既定で音量ノーマライズ ON
    assert v["theme"] == "system"  # 既定は OS テーマに追従
    assert v["log_level"] == "WARNING"  # 既定は警告レベル


def test_settings_dialog_connection_test(qtbot, monkeypatch):
    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (True, "接続 OK"))
    dlg = SettingsDialog()
    qtbot.addWidget(dlg)
    dlg._on_test_connection()
    assert dlg._test_result.text().startswith("OK:")

    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (False, "拒否"))
    dlg._on_test_connection()
    assert dlg._test_result.text().startswith("NG:")


def test_apply_settings_updates_window(main_window, tmp_path):
    win = main_window
    win.apply_settings(
        {
            "out_dir": tmp_path,
            "fmt": "m4a",
            "batch_size": 12,
            "auto_write": False,
            "expand_playlist": True,
            "normalize": False,
        }
    )
    assert win._out_dir == tmp_path
    assert win._batch_size == 12
    assert win._fmt_combo.currentText() == "m4a"
    assert win._auto_write.isChecked() is False
    assert win._expand_playlist is True
    assert win._normalize is False


def test_apply_settings_default_dir_becomes_none(main_window):
    """保存先が既定の FILES_DIR なら None（core 既定）として扱う。"""
    win = main_window
    win.apply_settings(
        {"out_dir": Path(core.FILES_DIR), "fmt": "mp3", "batch_size": 5, "auto_write": True}
    )
    assert win._out_dir is None


# ---------------------------------------------------------------------------
# ログパネル / 縮退通知 / ffmpeg 警告
# ---------------------------------------------------------------------------


def test_log_panel_receives_mv2title_warning(main_window, qtbot):
    win = main_window
    assert not win._log_panel.isVisible()  # 既定は非表示
    logging.getLogger("mv2title").warning("パースフォールバック発動")
    # ハンドラ → QueuedConnection → パネルなのでイベントループを回して待つ
    qtbot.waitUntil(
        lambda: "パースフォールバック発動" in win._log_panel.toPlainText(), timeout=2000
    )
    assert "WARNING" in win._log_panel.toPlainText()


def test_attach_handler_targets_three_loggers_at_debug():
    """attach_handler は mv2title/core/yt_dlp に付き、各ロガーを DEBUG へ下げる。
    detach_handler は変更前のレベルを復元する（グローバル状態を戻す）。"""
    from gui.logpanel import QtLogHandler, attach_handler, detach_handler

    names = ("mv2title", "core", "yt_dlp")
    before = {n: logging.getLogger(n).level for n in names}
    handler = QtLogHandler()
    try:
        attach_handler(handler)
        for name in names:
            logger = logging.getLogger(name)
            assert handler in logger.handlers
            # フィルタはハンドラ側で行うため、ロガー自体は DEBUG まで通す
            assert logger.level == logging.DEBUG
    finally:
        detach_handler(handler)
    for name in names:
        assert handler not in logging.getLogger(name).handlers
        assert logging.getLogger(name).level == before[name]  # レベル復元


def test_handler_level_filters_records(main_window, qtbot):
    """ハンドラのレベルで表示を絞る。既定 WARNING では INFO は落ち、
    INFO に下げると通る（フィルタはハンドラ 1 箇所という設計の検証）。"""
    win = main_window
    # 既定は WARNING → INFO レコードはパネルに出ない
    assert win._log_handler.level == logging.WARNING
    logging.getLogger("yt_dlp").info("info-below-warning")
    logging.getLogger("mv2title").warning("warn-passes")
    qtbot.waitUntil(lambda: "warn-passes" in win._log_panel.toPlainText(), timeout=2000)
    assert "info-below-warning" not in win._log_panel.toPlainText()

    # INFO へ下げると INFO も通る
    win.apply_settings(
        {
            "out_dir": Path(core.FILES_DIR),
            "fmt": "mp3",
            "batch_size": 5,
            "auto_write": True,
            "log_level": "INFO",
        }
    )
    assert win._log_handler.level == logging.INFO
    logging.getLogger("yt_dlp").info("info-now-visible")
    qtbot.waitUntil(lambda: "info-now-visible" in win._log_panel.toPlainText(), timeout=2000)


def test_apply_settings_persists_log_level(qtbot, monkeypatch):
    """apply_settings がハンドラレベルを変え、QSettings へ log_level を保存する。"""
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    # restore 無効でも QSettings 永続化の呼び出しパターンを検証するため差し込む
    saved = {}
    win._settings = type("S", (), {"setValue": lambda self, k, v: saved.__setitem__(k, v)})()
    win.apply_settings(
        {
            "out_dir": Path(core.FILES_DIR),
            "fmt": "mp3",
            "batch_size": 5,
            "auto_write": True,
            "log_level": "ERROR",
        }
    )
    assert win._log_level == "ERROR"
    assert win._log_handler.level == logging.ERROR
    assert saved["options/log_level"] == "ERROR"
    win.close()
    win.deleteLater()
    QApplication.processEvents()


def test_log_handler_detached_on_close(qtbot):
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    handler = win._log_handler
    assert handler in logging.getLogger("mv2title").handlers
    win.close()
    assert handler not in logging.getLogger("mv2title").handlers
    win.deleteLater()
    QApplication.processEvents()


def test_connection_failed_shows_degraded_message(main_window):
    win = main_window
    win._on_connection_failed("接続できません (http://x): refused")
    assert "DL のみ実行します" in win.statusBar().currentMessage()


def test_apply_settings_switches_theme(main_window, monkeypatch):
    from gui import main_window as mw

    applied = []
    monkeypatch.setattr(mw, "apply_color_scheme", applied.append)
    win = main_window
    win.apply_settings(
        {
            "out_dir": Path(core.FILES_DIR),
            "fmt": "mp3",
            "batch_size": 5,
            "auto_write": True,
            "theme": "dark",
        }
    )
    assert win._theme == "dark"
    assert applied == ["dark"]
    # 同じテーマなら再適用しない
    win.apply_settings(
        {
            "out_dir": Path(core.FILES_DIR),
            "fmt": "mp3",
            "batch_size": 5,
            "auto_write": True,
            "theme": "dark",
        }
    )
    assert applied == ["dark"]


def test_apply_color_scheme_mapping(qtbot, monkeypatch):
    """名前 → Qt.ColorScheme のマッピングを検証する。

    offscreen プラットフォームは setColorScheme を反映しない（colorScheme()
    が Unknown のまま）ため、フェイクの styleHints で呼び出し内容を捕捉する。
    """
    from PySide6.QtCore import Qt

    from gui import main_window as mw

    class FakeHints:
        scheme = None

        def setColorScheme(self, s):
            FakeHints.scheme = s

    monkeypatch.setattr(mw.QGuiApplication, "styleHints", staticmethod(FakeHints))
    mw.apply_color_scheme("dark")
    assert FakeHints.scheme == Qt.ColorScheme.Dark
    mw.apply_color_scheme("light")
    assert FakeHints.scheme == Qt.ColorScheme.Light
    mw.apply_color_scheme("system")
    assert FakeHints.scheme == Qt.ColorScheme.Unknown
    mw.apply_color_scheme("unknown-name")  # 不明な値もシステム扱い
    assert FakeHints.scheme == Qt.ColorScheme.Unknown


def test_toolbar_buttons_have_uniform_width(main_window):
    """追加 / ファイル追加 / files/ 取り込み / 設定 の 4 ボタンは同じ幅。"""
    from PySide6.QtWidgets import QPushButton

    win = main_window
    labels = {"追加", "ファイル追加", "files/ 取り込み", "設定"}
    buttons = [b for b in win.findChildren(QPushButton) if b.text() in labels]
    assert len(buttons) == 4
    widths = {b.width() for b in buttons}
    assert len(widths) == 1  # 全て同じ固定幅


def test_fill_artists_from_channel(main_window):
    """[チャンネル名→アーティスト] は選択行（未選択なら全行）へコピーし、undo 可能。"""
    from core import Track

    win = main_window
    win._model.add_tracks(
        [
            Track(stem="a", channel="Ch1"),
            Track(stem="b"),  # チャンネル無し → スキップ
            Track(stem="c", channel="Ch3"),
        ]
    )
    win._on_fill_artists()  # 未選択 → 全行が対象
    assert win._model.track_at(0).artist == "Ch1"
    assert win._model.track_at(1).artist == ""
    assert win._model.track_at(2).artist == "Ch3"
    # macro なので 1 回の undo で全部戻る
    win._undo.undo()
    assert win._model.track_at(0).artist == ""
    assert win._model.track_at(2).artist == ""


def test_fill_artists_keeps_selection(main_window):
    """[チャンネル名→アーティスト] 後も対象行が選択されたままになる。

    続けて [選択行を書き込み] を押せるようにする UX 修正の回帰テスト。
    """
    from core import Track

    win = main_window
    win._model.add_tracks(
        [Track(stem="a", channel="Ch1"), Track(stem="b", channel="Ch2"), Track(stem="c")]
    )
    win._view.selectRow(0)
    win._on_fill_artists()
    assert win._selected_rows() == [0]  # 選択していた行が選択のまま

    # 未選択 → 全行対象。チャンネルを持つ行(=埋めた行)が選択される
    win._view.clearSelection()
    win._on_fill_artists()
    assert win._selected_rows() == [0, 1]


def test_write_summary_shown_in_statusbar(main_window):
    win = main_window
    win._on_write_summary(3, 1, 0)
    assert "完了 3 件" in win.statusBar().currentMessage()
    assert "スキップ 1 件" in win.statusBar().currentMessage()


def test_progress_delegate_installed_on_status_column(main_window):
    from gui.main_window import _ProgressDelegate
    from gui.model import COL_STATUS

    win = main_window
    assert isinstance(win._view.itemDelegateForColumn(COL_STATUS), _ProgressDelegate)


def test_ffmpeg_warning_on_startup(qtbot, monkeypatch):
    from gui import main_window as mw

    monkeypatch.setattr(mw.shutil, "which", lambda name: None)
    win = mw.MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    assert "ffmpeg" in win.statusBar().currentMessage()
    win.close()
    win.deleteLater()
    QApplication.processEvents()
