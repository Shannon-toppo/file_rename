# -*- coding: utf-8 -*-
"""フェーズ 4「Excel 風操作感」のオフラインテスト。

コピー/ペースト(TSV)・clear_title・undo/redo・列ソート・右クリック
メニューを検証する。LLM・yt-dlp・ネットワークは一切使わない。
Qt はオフスクリーン（conftest.py で QT_QPA_PLATFORM=offscreen）。
"""
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, Qt

from core import Status, Track
from gui.clipboard import resolve_paste_targets, selection_to_tsv
from gui.commands import ClearTitleCommand, EditTitleCommand
from gui.model import (
    COL_CHANNEL,
    COL_STEM,
    COL_TITLE,
    TrackTableModel,
)


def _make_model(tracks):
    return TrackTableModel(list(tracks))


@pytest.fixture
def main_window(qtbot):
    """MainWindow を生成し、テスト後に確実に破棄するフィクスチャ。

    複数テストで MainWindow を作ると、qtbot の遅延破棄だけでは C++ 側の
    ウィジェットが残り、後続テストの pytest-qt のイベント処理で破棄途中の
    オブジェクトに触れてアクセス違反することがある。ここで close →
    deleteLater → イベント処理まで行い、境界で完全に解放する。
    QSettings は汚さないよう restore_settings=False で作る。
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


# ---------------------------------------------------------------------------
# selection_to_tsv
# ---------------------------------------------------------------------------


def test_selection_to_tsv_rectangle():
    tracks = [
        Track(stem="s1", channel="c1", guessed_title="t1"),
        Track(stem="s2", channel="c2", guessed_title="t2"),
    ]
    model = _make_model(tracks)
    # 2 行 × 2 列（元タイトル・チャンネル）の矩形
    indexes = [
        model.index(0, COL_STEM),
        model.index(0, COL_CHANNEL),
        model.index(1, COL_STEM),
        model.index(1, COL_CHANNEL),
    ]
    tsv = selection_to_tsv(model, indexes)
    assert tsv == "s1\tc1\ns2\tc2"


def test_selection_to_tsv_excludes_manual_prefix():
    # 手動行はタイトル列に "✎ " が付くが、コピーは素の値であること
    t = Track(stem="s", guessed_title="曲名", manual=True)
    model = _make_model([t])
    tsv = selection_to_tsv(model, [model.index(0, COL_TITLE)])
    assert tsv == "曲名"
    assert "✎" not in tsv


def test_selection_to_tsv_empty():
    model = _make_model([Track(stem="s")])
    assert selection_to_tsv(model, []) == ""


# ---------------------------------------------------------------------------
# resolve_paste_targets（貼り付け対象の判定。反映は undo コマンド経由）
# ---------------------------------------------------------------------------


def test_resolve_paste_targets_title_column():
    model = _make_model([Track(stem="s1"), Track(stem="s2")])
    # 開始セルをタイトル列にし、1 列分の TSV を 2 行分展開
    targets = resolve_paste_targets(model, model.index(0, COL_TITLE), "aaa\nbbb")
    assert targets == [(0, COL_TITLE, "aaa"), (1, COL_TITLE, "bbb")]


def test_resolve_paste_targets_ignores_non_editable_columns():
    model = _make_model([Track(stem="orig", channel="ch")])
    # 開始セルを元タイトル列にする → 編集可能列に落ちる分だけが対象
    # 行: [stem][channel][title] にまたがる 3 列 TSV
    targets = resolve_paste_targets(model, model.index(0, COL_STEM), "X\tY\tZ")
    # 対象はタイトル列（3 列目 = COL_TITLE）に落ちる "Z" のみ
    assert targets == [(0, COL_TITLE, "Z")]


def test_resolve_paste_targets_trailing_blank_line_ignored():
    model = _make_model([Track(stem="s1")])
    targets = resolve_paste_targets(model, model.index(0, COL_TITLE), "only\n")
    assert targets == [(0, COL_TITLE, "only")]


def test_resolve_paste_targets_out_of_range_rows_skipped():
    model = _make_model([Track(stem="s1")])
    # 2 行分あるが行は 1 つしかない → 1 件のみ対象
    targets = resolve_paste_targets(model, model.index(0, COL_TITLE), "a\nb")
    assert targets == [(0, COL_TITLE, "a")]


# ---------------------------------------------------------------------------
# clear_title
# ---------------------------------------------------------------------------


def test_clear_title_resets_fields():
    t = Track(
        stem="s",
        guessed_title="曲名",
        manual=True,
        valid=True,
        status=Status.DONE,
        error="x",
    )
    model = _make_model([t])
    model.clear_title(0)
    assert t.guessed_title == ""
    assert t.manual is False
    assert t.valid is None
    assert t.status is Status.QUEUED
    assert t.error == ""


def test_clear_title_emits_datachanged():
    t = Track(stem="s", guessed_title="x")
    model = _make_model([t])
    seen = []
    model.dataChanged.connect(lambda tl, br, roles=None: seen.append((tl.row(), br.row())))
    model.clear_title(0)
    assert seen == [(0, 0)]


# ---------------------------------------------------------------------------
# undo/redo
# ---------------------------------------------------------------------------


def test_edit_command_undo_restores_full_state():
    t = Track(stem="s", guessed_title="orig", manual=False, valid=True, status=Status.DONE)
    model = _make_model([t])
    cmd = EditTitleCommand(model, 0, "新タイトル")
    # 生成時点で編集が適用される（manual/PENDING 化）
    assert t.guessed_title == "新タイトル"
    assert t.manual is True
    assert t.status is Status.PENDING
    # undo で編集前へ完全復元（manual/valid/status 含む）
    cmd.undo()
    assert t.guessed_title == "orig"
    assert t.manual is False
    assert t.valid is True
    assert t.status is Status.DONE
    # redo で再適用
    cmd.redo()
    assert t.guessed_title == "新タイトル"
    assert t.manual is True
    assert t.status is Status.PENDING


def test_clear_command_undo_restores_state():
    t = Track(stem="s", guessed_title="曲名", manual=True, valid=False, status=Status.PENDING)
    model = _make_model([t])
    cmd = ClearTitleCommand(model, 0)
    assert t.guessed_title == ""
    assert t.status is Status.QUEUED
    cmd.undo()
    assert t.guessed_title == "曲名"
    assert t.manual is True
    assert t.valid is False
    assert t.status is Status.PENDING


def test_paste_macro_single_undo(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="s1"), Track(stem="s2")])

    start = win._model.index(0, COL_TITLE)
    n = win.paste_tsv_via_commands(start, "aaa\nbbb")
    assert n == 2
    assert win._model.track_at(0).guessed_title == "aaa"
    assert win._model.track_at(1).guessed_title == "bbb"
    # 1 回の undo で貼り付け全体が戻る（macro）
    win._undo.undo()
    assert win._model.track_at(0).guessed_title == ""
    assert win._model.track_at(1).guessed_title == ""
    # 1 回の redo で全部戻る
    win._undo.redo()
    assert win._model.track_at(0).guessed_title == "aaa"
    assert win._model.track_at(1).guessed_title == "bbb"


# ---------------------------------------------------------------------------
# sort
# ---------------------------------------------------------------------------


def test_sort_reorders_tracks():
    tracks = [
        Track(stem="banana"),
        Track(stem="apple"),
        Track(stem="cherry"),
    ]
    model = _make_model(tracks)
    model.sort(COL_STEM, Qt.SortOrder.AscendingOrder)
    stems = [model.track_at(r).stem for r in range(model.rowCount())]
    assert stems == ["apple", "banana", "cherry"]
    model.sort(COL_STEM, Qt.SortOrder.DescendingOrder)
    stems = [model.track_at(r).stem for r in range(model.rowCount())]
    assert stems == ["cherry", "banana", "apple"]


# ---------------------------------------------------------------------------
# 右クリックメニュー
# ---------------------------------------------------------------------------


def test_context_menu_items_present(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="s", url="http://u")])
    win._view.selectRow(0)
    menu = win.build_context_menu()
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "選択行を再推定" in texts
    assert "選択行を書き込み" in texts
    assert "行削除" in texts
    assert "URL をブラウザで開く" in texts


def test_context_menu_open_url_disabled_without_url(main_window):
    win = main_window
    # url を持たないローカルファイル行のみ
    win._model.add_tracks([Track(stem="s", filepath=Path("s.mp3"))])
    win._view.selectRow(0)
    menu = win.build_context_menu()
    open_url = next(a for a in menu.actions() if a.text() == "URL をブラウザで開く")
    assert not open_url.isEnabled()


def test_context_menu_open_url_enabled_with_url(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="s", url="http://u")])
    win._view.selectRow(0)
    menu = win.build_context_menu()
    open_url = next(a for a in menu.actions() if a.text() == "URL をブラウザで開く")
    assert open_url.isEnabled()


def test_sort_with_history_notifies_undo_clear(main_window):
    """編集履歴がある状態でソートすると、履歴破棄がステータスバーへ通知される。"""
    win = main_window
    win._model.add_tracks([Track(stem="b"), Track(stem="a")])
    win.push_edit(0, COL_TITLE, "edited")
    assert win._undo.count() == 1
    win._model.sort(COL_STEM)
    assert win._undo.count() == 0
    assert "編集履歴" in win.statusBar().currentMessage()


def test_context_menu_retry_enabled_only_for_error_rows(main_window):
    win = main_window
    win._model.add_tracks(
        [
            Track(stem="err", url="http://u", status=Status.ERROR, error="x"),
            Track(stem="ok", url="http://u2"),
        ]
    )
    win._view.selectRow(0)
    menu = win.build_context_menu()
    retry = next(a for a in menu.actions() if a.text() == "エラー行を再試行待ちに戻す")
    assert retry.isEnabled()
    # ERROR 行が選択に含まれなければ無効
    win._view.selectRow(1)
    menu = win.build_context_menu()
    retry = next(a for a in menu.actions() if a.text() == "エラー行を再試行待ちに戻す")
    assert not retry.isEnabled()


def test_reset_errors_resets_selected_rows(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="err", url="http://u", status=Status.ERROR, error="x")])
    win._view.selectRow(0)
    win._on_reset_errors()
    t = win._model.track_at(0)
    assert t.status is Status.QUEUED and t.error == ""


def test_open_urls_calls_desktop_services(main_window, monkeypatch):
    from gui import main_window as mw

    win = main_window
    win._model.add_tracks([Track(stem="s", url="http://example.com")])
    win._view.selectRow(0)

    opened = []
    monkeypatch.setattr(
        mw.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString()))
    )
    win._on_open_urls()
    assert opened == ["http://example.com"]


# ---------------------------------------------------------------------------
# undo スタックと行構成変更・実行中ガードの回帰テスト（Fable レビュー起票）
# ---------------------------------------------------------------------------


def test_undo_stack_cleared_on_row_removal(main_window):
    """undo コマンドは行番号を保持するため、行削除後の undo は別行に復元されて
    しまう。行構成が変わったらスタックが破棄されること。"""
    win = main_window
    win._model.add_tracks([Track(stem="s1"), Track(stem="s2")])
    win.push_edit(1, COL_TITLE, "編集")
    assert win._undo.canUndo()
    win._model.remove_rows([0])  # 行 1 が行 0 へずれる
    assert not win._undo.canUndo()


def test_undo_stack_cleared_on_sort(main_window):
    from gui.model import COL_STEM

    win = main_window
    win._model.add_tracks([Track(stem="b"), Track(stem="a")])
    win.push_edit(0, COL_TITLE, "編集")
    assert win._undo.canUndo()
    win._model.sort(COL_STEM)  # layoutChanged → スタック破棄
    assert not win._undo.canUndo()


def test_undo_stack_kept_on_append(main_window):
    """末尾への行追加は既存行がずれないため undo は維持される。"""
    win = main_window
    win._model.add_tracks([Track(stem="s1")])
    win.push_edit(0, COL_TITLE, "編集")
    win._model.add_tracks([Track(stem="s2")])
    assert win._undo.canUndo()


def test_editing_disabled_while_running(main_window):
    """実行中はワーカーが Track を触るため、直接セル編集も禁止されること。"""
    from PySide6.QtWidgets import QAbstractItemView

    win = main_window
    win._set_running(True)
    assert win._view.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    win._set_running(False)
    assert win._view.editTriggers() == win._edit_triggers
