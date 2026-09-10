# -*- coding: utf-8 -*-
"""置換（[検索・置換] / Ctrl+H・mac は ⌃H）とワイルドカード検索のオフラインテスト。

- gui/textmatch.py の純粋関数（ワイルドカード変換・置換）
- TrackTableModel.matches のワイルドカード / 大小文字オプション
- MainWindow の置換欄: 1 件ずつ置換・すべて置換・次を検索・undo・ショートカット

修飾キー付きの実キー（qtbot.keyClick）は撃たない（test_gui_actions.py の
test_ctrl_f_opens_search 参照）。キー操作は QKeyEvent を eventFilter へ直接渡す。
"""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence

from core import Status, Track
from gui.model import COL_ALBUM, COL_ARTIST, COL_STEM, COL_TITLE, TrackTableModel
from gui.textmatch import compile_pattern, has_match, replace_in, wildcard_to_regex

# ---------------------------------------------------------------------------
# textmatch（純粋関数）
# ---------------------------------------------------------------------------


def test_plain_pattern_treats_regex_symbols_literally():
    p = compile_pattern("a.b(")
    assert has_match("xa.b(y", p)
    assert not has_match("axb(", p)


def test_empty_query_has_no_pattern():
    assert compile_pattern("") is None


def test_case_sensitivity_option():
    assert has_match("MiMi", compile_pattern("mimi"))
    assert not has_match("MiMi", compile_pattern("mimi", False, True))


def test_wildcard_star_and_question():
    star = compile_pattern("a*c", True)
    assert has_match("abbbc", star)
    assert has_match("ac", star)  # * は 0 文字も含む
    q = compile_pattern("a?c", True)
    assert has_match("abc", q)
    assert not has_match("ac", q)
    assert not has_match("abbc", q)


def test_wildcard_escape_with_tilde():
    assert wildcard_to_regex("~*") == r"\*"
    p = compile_pattern("a~?", True)
    assert has_match("a?", p)
    assert not has_match("ab", p)
    assert has_match("a~b", compile_pattern("a~~b", True))


def test_wildcard_off_treats_star_literally():
    p = compile_pattern("a*c")
    assert has_match("a*c", p)
    assert not has_match("abc", p)


def test_replace_in_counts_occurrences():
    assert replace_in("a-b-c", compile_pattern("-"), "_") == ("a_b_c", 2)
    assert replace_in("abc", compile_pattern("z"), "_") == ("abc", 0)


def test_replace_in_is_literal():
    # 後方参照は解釈しない（置換後の文字列はそのまま入る）
    assert replace_in("abc", compile_pattern("b"), r"\1") == (r"a\1c", 1)


def test_replace_in_wildcard_brackets_and_trailing_star():
    p = compile_pattern(" (*)", True)
    assert replace_in("Pale (Official MV)", p, "") == ("Pale", 1)
    # 末尾の * は最長一致（以降を丸ごと）
    p = compile_pattern(" feat.*", True)
    assert replace_in("Pale feat. 初音ミク", p, "") == ("Pale", 1)


def test_replace_in_skips_empty_matches():
    """「*」単独は空一致を含むが、置換は 1 文字以上の一致だけ。"""
    p = compile_pattern("*", True)
    assert replace_in("abc", p, "X") == ("X", 1)
    assert replace_in("", p, "X") == ("", 0)
    assert not has_match("", p)


# ---------------------------------------------------------------------------
# TrackTableModel.matches（ワイルドカード / 大小文字）
# ---------------------------------------------------------------------------


def test_matches_with_wildcard():
    model = TrackTableModel([Track(stem="MIMI『Pale』feat.初音ミク")])
    assert model.matches(0, "『*』", wildcard=True)
    assert not model.matches(0, "『*』")  # ワイルドカード OFF なら * は文字


def test_matches_wildcard_does_not_span_columns():
    """ワイルドカードは列の境目をまたがない（セル単位で一致を見る）。"""
    model = TrackTableModel([Track(stem="alpha", channel="beta")])
    assert not model.matches(0, "alpha*beta", wildcard=True)
    assert model.matches(0, "a*a", wildcard=True)


def test_matches_case_sensitive():
    model = TrackTableModel([Track(stem="MiMi")])
    assert model.matches(0, "mimi")
    assert not model.matches(0, "mimi", case_sensitive=True)
    assert model.matches(0, "MiMi", case_sensitive=True)


# ---------------------------------------------------------------------------
# 置換欄（MainWindow）
# ---------------------------------------------------------------------------


def _titles(win):
    return [win._model.track_at(r).guessed_title for r in range(win._model.rowCount())]


def _visible(win):
    return [win._model.track_at(r).stem for r in win._visible_rows()]


def _setup(win, tracks, query, replacement=""):
    win._model.add_tracks(tracks)
    win.open_replace()
    win._search_edit.setText(query)
    win._replace_edit.setText(replacement)


def test_open_search_and_replace_share_one_bar(main_window):
    """検索・置換は 1 本のバー。Ctrl+F は検索欄、Ctrl+H は置換欄へフォーカスする。"""
    win = main_window
    win.open_search()
    assert not win._search_bar.isHidden()
    assert win.focusWidget() is win._search_edit

    win._search_edit.setText("x")
    win.open_replace()
    assert win.focusWidget() is win._replace_edit

    win.close_search()
    assert win._search_bar.isHidden()
    # 検索語が空なら、置換の前にまず検索欄へ
    win.open_replace()
    assert win.focusWidget() is win._search_edit


def test_no_duplicate_replace_buttons(main_window):
    """「置換」とだけ書かれたボタンは置かない（役割の違うボタンは名前で区別する）。"""
    from PySide6.QtWidgets import QAbstractButton

    labels = [b.text() for b in main_window.findChildren(QAbstractButton)]
    assert "置換" not in labels
    assert labels.count("検索・置換") == 1
    assert {"次へ", "置換して次へ", "すべて置換"} <= set(labels)


def test_replace_all_replaces_titles_and_undo_reverts_in_one_step(main_window):
    win = main_window
    _setup(
        win,
        [
            Track(stem="s1", guessed_title="Pale (MV)"),
            Track(stem="s2", guessed_title="Rouge (MV) (MV)"),
            Track(stem="s3", guessed_title="Other"),
        ],
        " (MV)",
    )
    assert win.replace_all() == 2
    assert _titles(win) == ["Pale", "Rouge", "Other"]
    # タイトルの置換は手動編集扱い（再推定で上書きされないように）
    assert win._model.track_at(0).manual is True
    assert "3 箇所" in win.statusBar().currentMessage()

    win._undo.undo()
    assert _titles(win) == ["Pale (MV)", "Rouge (MV) (MV)", "Other"]
    assert win._model.track_at(0).manual is False


def test_replaced_rows_stay_visible_after_they_stop_matching(main_window):
    """置換で検索語に一致しなくなった行も、消えずに表示に残る。"""
    win = main_window
    _setup(
        win,
        [Track(stem="s1", guessed_title="Pale (MV)"), Track(stem="s2", guessed_title="x")],
        "(MV)",
    )
    assert _visible(win) == ["s1"]
    win.replace_all()
    assert _visible(win) == ["s1"]  # 置換後も隠れない
    assert win._selected_rows() == [0]

    # 検索語を変えたら「残す」扱いは解除される
    win._search_edit.setText("zzz")
    assert _visible(win) == []


def test_replace_all_respects_target_column(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="foo", artist="foo", album="foo")], "foo", "bar")
    win._replace_target.setCurrentIndex(
        next(i for i in range(win._replace_target.count())
             if win._replace_target.itemText(i) == "アーティスト")
    )
    assert win.replace_all() == 1
    t = win._model.track_at(0)
    assert (t.guessed_title, t.artist, t.album) == ("foo", "bar", "foo")


def test_replace_all_never_touches_non_editable_columns(main_window):
    win = main_window
    _setup(win, [Track(stem="foo", channel="foo", guessed_title="foo")], "foo", "bar")
    assert win.replace_all() == 1
    t = win._model.track_at(0)
    assert (t.stem, t.channel, t.guessed_title) == ("foo", "foo", "bar")


def test_replace_all_with_wildcard(main_window):
    win = main_window
    _setup(
        win,
        [Track(stem="s", guessed_title="MIMI『Pale』feat.初音ミク", artist="MIMI feat.初音ミク")],
        " feat.*",
    )
    win._wildcard_check.setChecked(True)
    assert win.replace_all() == 1
    assert win._model.track_at(0).artist == "MIMI"
    # 先頭に空白の無いタイトル側は一致しない
    assert win._model.track_at(0).guessed_title == "MIMI『Pale』feat.初音ミク"


def test_replace_all_case_sensitive_option(main_window):
    win = main_window
    _setup(win, [Track(stem="s", artist="Mimi"), Track(stem="t", artist="mimi")], "mimi", "MIMI")
    win._case_check.setChecked(True)
    assert win.replace_all() == 1
    assert [win._model.track_at(r).artist for r in range(2)] == ["Mimi", "MIMI"]


def test_replace_all_skips_rows_hidden_by_filter(main_window):
    """空白だけの検索語は絞り込みをしないが、置換の検索語としては使える。"""
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="a  b")], "  ", " ")
    assert win.replace_all() == 1
    assert _titles(win) == ["a b"]


def test_replace_all_without_query_or_match(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="abc")], "")
    assert win.replace_all() == 0
    win._search_edit.setText("zzz")
    assert win.replace_all() == 0
    assert "ありません" in win.statusBar().currentMessage()


def test_replace_current_moves_first_then_replaces_one_by_one(main_window):
    """Excel と同じ: 現在セルが一致していなければ移動だけ、一致していれば置換して次へ。"""
    win = main_window
    _setup(
        win,
        [
            Track(stem="s1", guessed_title="a-1"),
            Track(stem="s2", guessed_title="none"),
            Track(stem="s3", artist="a-3"),
        ],
        "a-",
        "b-",
    )
    win._view.setCurrentIndex(win._model.index(0, COL_STEM))

    assert win.replace_current() is False  # 1 回目は一致セルへ移動するだけ
    assert (win._view.currentIndex().row(), win._view.currentIndex().column()) == (0, COL_TITLE)
    assert win._model.track_at(0).guessed_title == "a-1"

    assert win.replace_current() is True
    assert win._model.track_at(0).guessed_title == "b-1"
    assert (win._view.currentIndex().row(), win._view.currentIndex().column()) == (2, COL_ARTIST)

    assert win.replace_current() is True
    assert win._model.track_at(2).artist == "b-3"
    assert "ほかに一致はありません" in win.statusBar().currentMessage()

    # 1 件ずつの置換は 1 回の undo で 1 件戻る
    win._undo.undo()
    assert win._model.track_at(2).artist == "a-3"
    assert win._model.track_at(0).guessed_title == "b-1"


def test_find_next_wraps_and_goes_backward(main_window):
    win = main_window
    _setup(
        win,
        [
            Track(stem="s1", guessed_title="x"),
            Track(stem="s2", artist="x", album="x"),
        ],
        "x",
    )
    win._view.setCurrentIndex(win._model.index(0, COL_TITLE))
    pos = lambda: (win._view.currentIndex().row(), win._view.currentIndex().column())  # noqa: E731

    assert win.find_next() and pos() == (1, COL_ARTIST)
    assert win.find_next() and pos() == (1, COL_ALBUM)
    assert win.find_next() and pos() == (0, COL_TITLE)  # 末尾から先頭へ回り込む
    assert win.find_next(backward=True) and pos() == (1, COL_ALBUM)


def test_find_next_only_visits_target_columns(main_window):
    """[次へ] の移動先は置換対象の列だけ（元タイトルの一致には止まらない）。"""
    win = main_window
    _setup(win, [Track(stem="alpha"), Track(stem="s", artist="alpine")], "alp")
    win._view.setCurrentIndex(win._model.index(0, COL_STEM))
    assert win.find_next()
    assert (win._view.currentIndex().row(), win._view.currentIndex().column()) == (1, COL_ARTIST)


def test_replace_label_counts_target_cells(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="x", artist="x")], "x")
    assert win._replace_label.text() == "2 セル一致"
    win._search_edit.setText("zzz")
    assert win._replace_label.text() == "一致なし"


def test_replace_is_blocked_while_running(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="abc", status=Status.PENDING)], "abc", "x")
    win._set_running(True)
    try:
        assert not win._replace_all_btn.isEnabled()
        assert not win._replace_one_btn.isEnabled()
        assert win.replace_all() == 0
        assert win.replace_current() is False
        assert _titles(win) == ["abc"]
    finally:
        win._set_running(False)
    assert win._replace_all_btn.isEnabled()


def test_wildcard_option_refilters_rows(main_window):
    win = main_window
    win._model.add_tracks([Track(stem="a1c"), Track(stem="a*c")])
    win.open_search()
    win._search_edit.setText("a*c")
    assert _visible(win) == ["a*c"]
    win._wildcard_check.setChecked(True)
    assert _visible(win) == ["a1c", "a*c"]
    win._wildcard_check.setChecked(False)
    assert _visible(win) == ["a*c"]


# ---------------------------------------------------------------------------
# キー操作・ショートカット
# ---------------------------------------------------------------------------


def _key(key, mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QEvent.Type.KeyPress, key, mods)


def test_enter_in_replace_box_replaces_one_and_ctrl_enter_all(main_window):
    win = main_window
    _setup(
        win,
        [Track(stem="s1", guessed_title="ab"), Track(stem="s2", guessed_title="ab")],
        "a",
        "x",
    )
    win._view.setCurrentIndex(win._model.index(0, COL_TITLE))
    assert win.eventFilter(win._replace_edit, _key(Qt.Key.Key_Return)) is True
    assert _titles(win) == ["xb", "ab"]

    ctrl = Qt.KeyboardModifier.ControlModifier
    assert win.eventFilter(win._replace_edit, _key(Qt.Key.Key_Return, ctrl)) is True
    assert _titles(win) == ["xb", "xb"]


def test_escape_in_replace_box_closes_bar(main_window):
    win = main_window
    _setup(win, [Track(stem="s")], "zzz")
    assert win.eventFilter(win._replace_edit, _key(Qt.Key.Key_Escape)) is True
    assert win._search_bar.isHidden()
    assert _visible(win) == ["s"]


def test_enter_in_search_box_moves_focus_to_table(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="x")], "x")
    win._search_edit.setFocus()
    assert win.eventFilter(win._search_edit, _key(Qt.Key.Key_Return)) is True
    assert win.focusWidget() is win._view


def _action_with(win, seq: QKeySequence):
    return next(a for a in win.actions() if seq in a.shortcuts())


def test_replace_shortcut_opens_replace(main_window):
    from gui.main_window import replace_shortcut

    win = main_window
    win._search_edit.setText("x")
    _action_with(win, replace_shortcut()).trigger()
    assert not win._search_bar.isHidden()
    assert win.focusWidget() is win._replace_edit


def test_replace_shortcut_is_control_h_on_mac(monkeypatch):
    """mac は Control+H（Qt では Meta+H）。Ctrl+H のままだと ⌘H = OS の「隠す」になる。"""
    import gui.main_window as mw

    monkeypatch.setattr(mw.sys, "platform", "darwin")
    assert mw.replace_shortcut() == QKeySequence("Meta+H")
    monkeypatch.setattr(mw.sys, "platform", "win32")
    assert mw.replace_shortcut() == QKeySequence("Ctrl+H")


def test_find_next_and_previous_shortcuts(main_window):
    win = main_window
    _setup(win, [Track(stem="s", guessed_title="x", artist="x")], "x")
    win._view.setCurrentIndex(win._model.index(0, COL_TITLE))
    next_seq = QKeySequence.keyBindings(QKeySequence.StandardKey.FindNext)[0]
    prev_seq = QKeySequence.keyBindings(QKeySequence.StandardKey.FindPrevious)[0]
    _action_with(win, next_seq).trigger()
    assert win._view.currentIndex().column() == COL_ARTIST
    _action_with(win, prev_seq).trigger()
    assert win._view.currentIndex().column() == COL_TITLE
