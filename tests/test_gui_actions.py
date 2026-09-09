# -*- coding: utf-8 -*-
"""メイン画面の追加操作のオフラインテスト。

- 検索（行の絞り込み。[検索] ボタン / Ctrl+F）
- [再生リストを無視] チェックボックス（設定ダイアログの展開設定と表裏）
- 右クリックの「ファイルを Finder/エクスプローラーで開く」

LLM・yt-dlp・ネットワーク・実際のファイルマネージャーは一切使わない。
Qt はオフスクリーン（conftest.py で QT_QPA_PLATFORM=offscreen）。
MainWindow は conftest の main_window フィクスチャ（restore_settings=False）
経由で作る。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from core import Status, Track
from gui.model import COL_TITLE, TrackTableModel


def _bar_shown(win) -> bool:
    """検索バーが出ているか。

    ウィンドウを show() しないオフスクリーンのテストでは isVisible() が常に
    False になるため、明示的な hide フラグ（= setVisible が触る側）を見る。
    """
    return not win._search_bar.isHidden()


def _rows(win):
    """絞り込みで表示されている行の「元タイトル」一覧。"""
    return [win._model.track_at(r).stem for r in win._visible_rows()]


# ---------------------------------------------------------------------------
# TrackTableModel.matches（純粋なマッチ判定）
# ---------------------------------------------------------------------------


def test_matches_searches_every_column():
    model = TrackTableModel(
        [
            Track(stem="video-a", channel="ChA", guessed_title="曲A", artist="作者A"),
            Track(stem="video-b", channel="ChB", guessed_title="曲B", album="アルバムB"),
        ]
    )
    assert model.matches(0, "video-a")
    assert model.matches(0, "ChA")
    assert model.matches(0, "曲A")
    assert model.matches(0, "作者A")
    assert model.matches(1, "アルバムB")
    assert not model.matches(1, "作者A")


def test_matches_is_case_insensitive_and_empty_matches_all():
    model = TrackTableModel([Track(stem="MiMi - Pale")])
    assert model.matches(0, "mimi")
    assert model.matches(0, "PALE")
    assert model.matches(0, "")  # 空の検索語は絞り込み無し
    assert model.matches(0, "   ")


def test_matches_covers_status_and_error_text():
    """状態列・エラー文言（推定タイトル列に出る）も検索対象。"""
    model = TrackTableModel(
        [Track(stem="s", status=Status.ERROR, error="Music Premium のメンバーのみ")]
    )
    assert model.matches(0, "エラー")
    assert model.matches(0, "Premium")


# ---------------------------------------------------------------------------
# 検索バー（絞り込み）
# ---------------------------------------------------------------------------


def test_search_hides_non_matching_rows(main_window):
    win = main_window
    win._model.add_tracks(
        [Track(stem="alpha"), Track(stem="beta"), Track(stem="alpaca")]
    )
    win.open_search()
    assert _bar_shown(win)

    win._search_edit.setText("alp")
    assert _rows(win) == ["alpha", "alpaca"]
    assert win._view.isRowHidden(1)
    assert win._search_label.text() == "2 / 3 件"


def test_search_without_match_reports_it(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="alpha")])
    win._search_edit.setText("zzz")
    assert _rows(win) == []
    assert win._search_label.text() == "一致なし"


def test_close_search_restores_all_rows(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="alpha"), Track(stem="beta")])
    win.open_search()
    win._search_edit.setText("alpha")
    assert _rows(win) == ["alpha"]

    win.close_search()
    assert not _bar_shown(win)
    assert _rows(win) == ["alpha", "beta"]
    assert win._search_label.text() == ""


def test_search_follows_row_content_changes(main_window):
    """行の内容が変わったら絞り込みも追従する（推定結果が入った行など）。"""
    win = main_window
    win._model.add_tracks([Track(stem="s1"), Track(stem="s2")])
    win._search_edit.setText("Pale")
    assert _rows(win) == []

    win._model.set_title(1, "Pale")  # dataChanged → 該当行を再評価
    assert _rows(win) == ["s2"]


def test_search_follows_row_additions_and_removals(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="alpha"), Track(stem="beta")])
    win._search_edit.setText("alp")
    assert _rows(win) == ["alpha"]

    win._model.add_tracks([Track(stem="alpine")])
    assert _rows(win) == ["alpha", "alpine"]

    win._model.remove_rows([0])  # 行番号がずれても隠す対象は追従する
    assert _rows(win) == ["alpine"]


def test_selected_rows_excludes_hidden_rows(main_window):
    """絞り込み中は、隠れている行は選択に残っていても操作対象にしない。"""
    win = main_window
    win._model.add_tracks([Track(stem="alpha"), Track(stem="beta")])
    win._view.selectAll()
    assert win._selected_rows() == [0, 1]

    win._search_edit.setText("beta")
    assert win._selected_rows() == [1]


def test_ctrl_f_opens_search(main_window):
    """Ctrl+F（mac は ⌘F）が検索欄を開くアクションに結線されている。

    実キー（QTest.keyClick）を撃たないのは、offscreen プラットフォームだと
    修飾キーの押下状態が QGuiApplication に残り、以後のテストの selectRow が
    「Ctrl を押しながらのクリック」扱いになって選択が加算されるため
    （実際にこれで別ファイルのテストが落ちた）。アクションの登録と結線を
    直接確かめる。
    """
    win = main_window
    find = next(
        a
        for a in win.actions()
        if a.shortcut() == QKeySequence(QKeySequence.StandardKey.Find)
    )
    assert not _bar_shown(win)
    find.trigger()
    assert _bar_shown(win)
    # 非表示ウィンドウでは hasFocus() が立たないので、フォーカス先で見る
    assert win.focusWidget() is win._search_edit


def test_escape_in_search_box_clears_filter(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="alpha"), Track(stem="beta")])
    win.open_search()
    win._search_edit.setText("alpha")
    assert _rows(win) == ["alpha"]

    # eventFilter 経由（QShortcut ではないのでフォーカス不要）
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert win.eventFilter(win._search_edit, event) is True
    assert not _bar_shown(win)
    assert _rows(win) == ["alpha", "beta"]


def test_fill_artists_targets_visible_rows_only(main_window):
    """未選択時の「全行」も、絞り込み中は見えている行だけを指す。"""
    win = main_window
    win._model.add_tracks(
        [Track(stem="alpha", channel="ChA"), Track(stem="beta", channel="ChB")]
    )
    win._search_edit.setText("beta")
    win._view.selectionModel().clearSelection()
    win._on_fill_artists()
    assert win._model.track_at(0).artist == ""
    assert win._model.track_at(1).artist == "ChB"


# ---------------------------------------------------------------------------
# [再生リストを無視] チェックボックス
# ---------------------------------------------------------------------------


def test_noplaylist_checkbox_defaults_to_ignoring_playlists(main_window):
    """既定は ON（= expand_playlist False。従来どおり動画 1 本だけ）。"""
    win = main_window
    assert win._noplaylist_check.isChecked() is True
    assert win._expand_playlist is False


def test_noplaylist_checkbox_toggles_expand_playlist(main_window):
    win = main_window
    win._noplaylist_check.setChecked(False)
    assert win._expand_playlist is True
    win._noplaylist_check.setChecked(True)
    assert win._expand_playlist is False


def test_settings_dialog_syncs_noplaylist_checkbox(main_window):
    """設定ダイアログ側で展開を ON にすると、チェックボックスも裏返る。"""
    win = main_window
    win.apply_settings(
        {
            "out_dir": None,
            "fmt": "mp3",
            "batch_size": 5,
            "auto_write": True,
            "expand_playlist": True,
        }
    )
    assert win._noplaylist_check.isChecked() is False
    assert win._expand_playlist is True


# ---------------------------------------------------------------------------
# 右クリック「ファイルを Finder/エクスプローラーで開く」
# ---------------------------------------------------------------------------


def _reveal_action(win):
    from gui.main_window import file_manager_name

    label = f"ファイルを {file_manager_name()} で開く"
    return next(a for a in win.build_context_menu().actions() if a.text() == label)


def test_context_menu_reveal_disabled_without_file(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="s", url="http://u")])  # 未 DL の URL 行
    win._view.selectRow(0)
    assert not _reveal_action(win).isEnabled()


def test_context_menu_reveal_enabled_with_file(main_window, tmp_path):
    win = main_window
    f = tmp_path / "song.mp3"
    f.write_bytes(b"\x00")
    win._model.add_tracks([Track(stem="song", filepath=f)])
    win._view.selectRow(0)
    assert _reveal_action(win).isEnabled()


def test_reveal_file_calls_file_manager(main_window, tmp_path, monkeypatch):
    import gui.main_window as mw

    win = main_window
    f = tmp_path / "song.mp3"
    f.write_bytes(b"\x00")
    win._model.add_tracks([Track(stem="song", filepath=f)])
    win._view.selectRow(0)

    called = {}
    monkeypatch.setattr(
        mw, "reveal_in_file_manager", lambda p: called.setdefault("path", Path(p)) or True
    )
    win._on_reveal_file()
    assert called["path"] == f
    assert "song.mp3" in win.statusBar().currentMessage()


def test_reveal_file_reports_missing_file(main_window, tmp_path, monkeypatch):
    import gui.main_window as mw

    win = main_window
    missing = tmp_path / "gone.mp3"
    win._model.add_tracks([Track(stem="gone", filepath=missing)])
    win._view.selectRow(0)

    def fail(_path):  # 呼ばれてはいけない
        raise AssertionError("存在しないファイルで開こうとした")

    monkeypatch.setattr(mw, "reveal_in_file_manager", fail)
    win._on_reveal_file()
    assert "見つかりません" in win.statusBar().currentMessage()


def test_reveal_in_file_manager_uses_platform_command(monkeypatch, tmp_path):
    """mac は `open -R`、Windows は `explorer /select,` を使う。"""
    import gui.main_window as mw

    f = tmp_path / "song.mp3"
    f.write_bytes(b"\x00")
    calls = []
    monkeypatch.setattr(mw.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    monkeypatch.setattr(mw.sys, "platform", "darwin")
    assert mw.reveal_in_file_manager(f) is True
    assert calls[-1] == ["open", "-R", str(f)]

    monkeypatch.setattr(mw.sys, "platform", "win32")
    assert mw.reveal_in_file_manager(f) is True
    assert calls[-1] == ["explorer", f"/select,{f}"]


def test_reveal_in_file_manager_returns_false_on_failure(monkeypatch, tmp_path):
    import gui.main_window as mw

    def boom(cmd, **kw):
        raise OSError("no such tool")

    monkeypatch.setattr(mw.sys, "platform", "darwin")
    monkeypatch.setattr(mw.subprocess, "run", boom)
    assert mw.reveal_in_file_manager(tmp_path / "x.mp3") is False


# ---------------------------------------------------------------------------
# 実行中でも検索は使える（表示だけの機能なので止めない）
# ---------------------------------------------------------------------------


def test_search_works_while_running(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="alpha"), Track(stem="beta")])
    win._set_running(True)
    try:
        win._search_edit.setText("beta")
        assert _rows(win) == ["beta"]
    finally:
        win._set_running(False)


def test_search_does_not_touch_row_numbers(main_window):
    """絞り込みはビューで隠すだけ。モデルの行番号・件数は変わらない。"""
    win = main_window
    tracks = [Track(stem="alpha"), Track(stem="beta")]
    win._model.add_tracks(tracks)
    win._search_edit.setText("beta")
    assert win._model.rowCount() == 2
    assert win._model.track_at(1) is tracks[1]
    assert win._model.data(win._model.index(1, COL_TITLE), Qt.ItemDataRole.EditRole) == ""
