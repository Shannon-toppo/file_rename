# -*- coding: utf-8 -*-
"""試聴（プレビュー再生）のオフラインテスト。

実際の QMediaPlayer は生成しない（PreviewPlayer は遅延生成のため、
play() を呼ばなければマルチメディアバックエンドに触れない）。
MainWindow との結線はフェイクプレーヤへの差し替えで検証する。
"""
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core import Track
from gui.player import PreviewPlayer, format_time, tail_start_ms


@pytest.fixture
def main_window(qtbot):
    """MainWindow を生成し、テスト後に確実に破棄する（他テストと同方針）。"""
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    yield win
    win.close()
    win.deleteLater()
    QApplication.processEvents()


class FakePlayer:
    """呼び出しを記録するだけの試聴プレーヤ代役（再生状態も擬似的に持つ）。"""

    def __init__(self):
        self.calls = []
        self.is_playing = False
        self.is_paused = False
        self.current_path = None

    def play(self, path, tail_only=False):
        self.calls.append(("play", Path(path), tail_only))
        self.current_path = Path(path)
        self.is_playing = True
        self.is_paused = False

    def pause(self):
        self.calls.append(("pause",))
        self.is_playing = False
        self.is_paused = True

    def resume(self):
        self.calls.append(("resume",))
        self.is_playing = True
        self.is_paused = False

    def stop(self):
        self.calls.append(("stop",))
        self.is_playing = False
        self.is_paused = False

    def seek(self, position_ms):
        self.calls.append(("seek", position_ms))


def _add_file_row(win, tmp_path, name="a.mp3"):
    f = tmp_path / name
    f.write_bytes(b"\x00")
    win._model.add_tracks([Track(stem=name, filepath=f)])
    return f


# ---------------------------------------------------------------------------
# PreviewPlayer 単体
# ---------------------------------------------------------------------------


def test_tail_start_ms():
    assert tail_start_ms(60_000, 10.0) == 50_000
    assert tail_start_ms(5_000, 10.0) == 0  # 曲が短ければ先頭から


def test_format_time():
    assert format_time(0) == "0:00"
    assert format_time(30_000) == "0:30"
    assert format_time(61_000) == "1:01"
    assert format_time(-5) == "0:00"  # 負値は 0 扱い


def test_preview_player_safe_before_first_play(qtbot):
    """play() 前は実プレーヤ未生成でも各操作が安全に使える。"""
    p = PreviewPlayer()
    assert p.is_playing is False
    assert p.is_paused is False
    assert p.current_path is None
    p.stop()  # すべて no-op（例外にならない）
    p.pause()
    p.resume()
    p.seek(1000)


# ---------------------------------------------------------------------------
# MainWindow との結線（フェイクプレーヤ）
# ---------------------------------------------------------------------------


def test_play_button_plays_selected_row(main_window, tmp_path):
    win = main_window
    win._player = FakePlayer()
    f = _add_file_row(win, tmp_path)
    win._view.selectRow(0)

    win._on_preview()
    assert win._player.calls == [("play", f, False)]
    assert "試聴中" in win.statusBar().currentMessage()


def test_play_button_toggles_pause_and_resume(main_window, tmp_path):
    """同じファイルなら ▶ は一時停止/再開のトグル（先頭からやり直さない）。"""
    win = main_window
    win._player = FakePlayer()
    _add_file_row(win, tmp_path)
    win._view.selectRow(0)

    win._on_preview()  # 再生開始
    win._on_preview()  # 再生中 → 一時停止
    assert win._player.calls[-1] == ("pause",)
    win._on_preview()  # 一時停止中 → 再開
    assert win._player.calls[-1] == ("resume",)


def test_play_button_switches_to_newly_selected_file(main_window, tmp_path):
    """別の行を選択して ▶ を押すと、そのファイルを最初から再生する。"""
    win = main_window
    win._player = FakePlayer()
    f1 = _add_file_row(win, tmp_path, "a.mp3")
    f2 = _add_file_row(win, tmp_path, "b.mp3")
    win._view.selectRow(0)
    win._on_preview()
    win._view.selectRow(1)
    win._on_preview()
    assert win._player.calls == [("play", f1, False), ("play", f2, False)]


def test_preview_tail_plays_tail_only(main_window, tmp_path):
    win = main_window
    win._player = FakePlayer()
    f = _add_file_row(win, tmp_path)
    win._view.selectRow(0)

    win._on_preview_tail()
    assert win._player.calls == [("play", f, True)]
    assert "末尾" in win.statusBar().currentMessage()


def test_preview_skips_rows_without_file(main_window):
    """filepath 未設定（URL のみ）の行は試聴できない旨を表示する。"""
    win = main_window
    win._player = FakePlayer()
    win._model.add_tracks([Track(stem="http://u", url="http://u")])
    win._view.selectRow(0)

    win._on_preview()
    assert win._player.calls == []
    assert "試聴できる行がありません" in win.statusBar().currentMessage()


def test_preview_missing_file_shows_message(main_window, tmp_path):
    win = main_window
    win._player = FakePlayer()
    win._model.add_tracks([Track(stem="gone", filepath=tmp_path / "gone.mp3")])
    win._view.selectRow(0)

    win._on_preview()
    assert win._player.calls == []
    assert "見つかりません" in win.statusBar().currentMessage()


def test_seek_slider_moves_player_position(main_window):
    win = main_window
    win._player = FakePlayer()
    win._on_seek(12_345)  # sliderMoved 相当
    assert win._player.calls == [("seek", 12_345)]


def test_position_and_duration_update_slider_and_label(main_window):
    win = main_window
    win._on_player_duration(60_000)
    assert win._seek_slider.maximum() == 60_000
    win._on_player_position(30_000)
    assert win._seek_slider.value() == 30_000
    assert win._time_label.text() == "0:30 / 1:00"


def test_running_stops_preview_and_disables_controls(main_window):
    win = main_window
    win._player = FakePlayer()
    win._player.is_playing = True

    win._set_running(True)
    assert ("stop",) in win._player.calls
    assert not win._play_btn.isEnabled()
    assert not win._tail_btn.isEnabled()
    assert not win._seek_slider.isEnabled()

    win._set_running(False)
    assert win._play_btn.isEnabled()
    assert win._tail_btn.isEnabled()
    assert win._seek_slider.isEnabled()


def test_playing_changed_updates_button_text(main_window):
    win = main_window
    win._player = FakePlayer()
    win._on_playing_changed(True)
    assert win._play_btn.text() == "⏸"
    win._on_playing_changed(False)
    assert win._play_btn.text() == "▶"


def test_pause_keeps_status_message(main_window, tmp_path):
    """一時停止では「試聴中」表示を保ち、停止時のみ「準備完了」へ戻す。"""
    win = main_window
    win._player = FakePlayer()
    _add_file_row(win, tmp_path)
    win._view.selectRow(0)

    win._on_preview()  # 再生開始 → 「試聴中: ...」
    win._player.pause()
    win._on_playing_changed(False)  # 一時停止による状態変化
    assert "試聴中" in win.statusBar().currentMessage()

    win._player.stop()
    win._on_playing_changed(False)  # 停止による状態変化
    assert win.statusBar().currentMessage() == "準備完了"
