# -*- coding: utf-8 -*-
"""TrackTableModel と MainWindow のオフラインテスト。

LLM・yt-dlp・ネットワークは一切使わない。Qt はオフスクリーン
（conftest.py で QT_QPA_PLATFORM=offscreen）。
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from core import Status, Track
from gui.model import (
    COL_ARTIST,
    COL_CHANNEL,
    COL_FORMAT,
    COL_STATUS,
    COL_STEM,
    COL_TITLE,
    TrackTableModel,
)


def _idx(model, row, col):
    return model.index(row, col)


# ---------------------------------------------------------------------------
# カラム・見出し
# ---------------------------------------------------------------------------


def test_columns_and_headers():
    model = TrackTableModel()
    assert model.columnCount() == 6
    headers = [
        model.headerData(c, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
        for c in range(6)
    ]
    assert headers == ["元タイトル", "チャンネル", "推定タイトル", "アーティスト", "状態", "形式"]


def test_display_values():
    t = Track(
        stem="Artist - Song [MV]",
        channel="ArtistCh",
        guessed_title="Song",
        filepath=Path("x/Song.mp3"),
    )
    model = TrackTableModel([t])
    role = Qt.ItemDataRole.DisplayRole
    assert model.data(_idx(model, 0, COL_STEM), role) == "Artist - Song [MV]"
    assert model.data(_idx(model, 0, COL_CHANNEL), role) == "ArtistCh"
    assert model.data(_idx(model, 0, COL_TITLE), role) == "Song"
    assert model.data(_idx(model, 0, COL_FORMAT), role) == "mp3"


def test_format_column_dash_when_no_file():
    model = TrackTableModel([Track(stem="x", url="http://u")])
    assert model.data(_idx(model, 0, COL_FORMAT), Qt.ItemDataRole.DisplayRole) == "-"


def test_error_row_shows_error_in_title_column():
    """ERROR 行はエラー内容を推定タイトル列に表示する（ホバー不要で読める）。"""
    t = Track(stem="x", status=Status.ERROR, error="ダウンロードに失敗しました: boom")
    model = TrackTableModel([t])
    role = Qt.ItemDataRole.DisplayRole
    assert model.data(_idx(model, 0, COL_TITLE), role) == "ダウンロードに失敗しました: boom"
    # 編集時は素の guessed_title のまま（エラー文をエディタに載せない）
    assert model.data(_idx(model, 0, COL_TITLE), Qt.ItemDataRole.EditRole) == ""


def test_reset_error_returns_to_queue_or_pending():
    """reset_error: タイトル無し→QUEUED、有り→PENDING、ERROR 以外は不変。"""
    no_title = Track(stem="a", status=Status.ERROR, error="x")
    with_title = Track(stem="b", guessed_title="t", status=Status.ERROR, error="y")
    not_error = Track(stem="c", status=Status.DONE)
    model = TrackTableModel([no_title, with_title, not_error])
    for row in range(3):
        model.reset_error(row)
    assert no_title.status is Status.QUEUED and no_title.error == ""
    assert with_title.status is Status.PENDING and with_title.error == ""
    assert not_error.status is Status.DONE


# ---------------------------------------------------------------------------
# 編集可否 / setData
# ---------------------------------------------------------------------------


def test_only_title_column_editable():
    model = TrackTableModel([Track(stem="x")])
    editable_flag = Qt.ItemFlag.ItemIsEditable
    assert model.flags(_idx(model, 0, COL_TITLE)) & editable_flag
    # チャンネル・状態・形式は編集不可。元タイトルは本文コピー用に
    # ItemIsEditable を持つが、setData では書き換わらない（別テストで担保）。
    for col in (COL_CHANNEL, COL_STATUS, COL_FORMAT):
        assert not (model.flags(_idx(model, 0, col)) & editable_flag)
    assert model.flags(_idx(model, 0, COL_STEM)) & editable_flag


def test_setdata_marks_manual_and_pending():
    t = Track(stem="x", guessed_title="old", status=Status.DONE, valid=True)
    model = TrackTableModel([t])
    ok = model.setData(_idx(model, 0, COL_TITLE), "  手動タイトル  ", Qt.ItemDataRole.EditRole)
    assert ok
    assert t.guessed_title == "手動タイトル"  # 前後空白は除去
    assert t.manual is True
    assert t.status is Status.PENDING
    assert t.error == ""


def test_setdata_rejects_non_title_columns():
    model = TrackTableModel([Track(stem="x")])
    assert not model.setData(_idx(model, 0, COL_STEM), "y", Qt.ItemDataRole.EditRole)


def test_manual_row_gets_pencil_prefix():
    t = Track(stem="x", guessed_title="曲名", manual=True)
    model = TrackTableModel([t])
    shown = model.data(_idx(model, 0, COL_TITLE), Qt.ItemDataRole.DisplayRole)
    assert shown == "✎ 曲名"
    # 編集時（EditRole）はプレフィックスなしの素の値
    edit = model.data(_idx(model, 0, COL_TITLE), Qt.ItemDataRole.EditRole)
    assert edit == "曲名"


# ---------------------------------------------------------------------------
# 背景色
# ---------------------------------------------------------------------------


def _bg(model, row):
    return model.data(_idx(model, row, COL_STATUS), Qt.ItemDataRole.BackgroundRole)


def test_background_colors():
    done = Track(stem="a", status=Status.DONE)
    error = Track(stem="b", status=Status.ERROR)
    warn = Track(stem="c", status=Status.PENDING, valid=False)
    busy_dl = Track(stem="d", status=Status.DOWNLOADING)
    busy_inf = Track(stem="e", status=Status.INFERRING)
    plain = Track(stem="f", status=Status.QUEUED)
    model = TrackTableModel([done, error, warn, busy_dl, busy_inf, plain])
    assert isinstance(_bg(model, 0), QColor)  # DONE 薄緑
    assert isinstance(_bg(model, 1), QColor)  # ERROR 薄赤
    assert isinstance(_bg(model, 2), QColor)  # 要確認 薄黄
    assert isinstance(_bg(model, 3), QColor)  # DL 薄青
    assert isinstance(_bg(model, 4), QColor)  # 推定 薄青
    assert _bg(model, 5) is None  # QUEUED は無色
    # 色が状態ごとに区別されていること
    assert _bg(model, 0) != _bg(model, 1)
    assert _bg(model, 1) != _bg(model, 2)


def test_fetching_uses_busy_color():
    """情報取得中（MODE_FETCH）も DL 中と同じ薄青で表示する。"""
    t = Track(stem="a", status=Status.FETCHING)
    model = TrackTableModel([t])
    assert isinstance(_bg(model, 0), QColor)


def test_pending_valid_true_no_warn_color():
    ok_pending = Track(stem="a", status=Status.PENDING, valid=True)
    model = TrackTableModel([ok_pending])
    assert _bg(model, 0) is None


def test_foreground_fixed_on_tinted_rows():
    """淡色背景の行はダークテーマの白文字と衝突するため、文字色を固定する。"""
    done = Track(stem="a", status=Status.DONE)
    plain = Track(stem="b", status=Status.QUEUED)
    model = TrackTableModel([done, plain])
    role = Qt.ItemDataRole.ForegroundRole
    fg = model.data(_idx(model, 0, COL_STEM), role)
    assert isinstance(fg, QColor)
    assert fg.lightness() < 128  # 淡色背景に載せる濃い文字色
    # 背景を塗らない行はテーマ既定に任せる
    assert model.data(_idx(model, 1, COL_STEM), role) is None


# ---------------------------------------------------------------------------
# 状態列の進捗表示
# ---------------------------------------------------------------------------


def test_status_shows_download_percent():
    t = Track(stem="a", status=Status.DOWNLOADING)
    model = TrackTableModel([t])
    model.set_percent(t, 45.0)
    assert model.data(_idx(model, 0, COL_STATUS), Qt.ItemDataRole.DisplayRole) == "DL中 45%"


def test_status_shows_playlist_position():
    """再生リスト DL 中は「DL中 2/5 30%」のように何番目かも表示する。"""
    t = Track(stem="a", status=Status.DOWNLOADING)
    model = TrackTableModel([t])
    model.set_percent(t, 30.0, label="2/5")
    assert model.data(_idx(model, 0, COL_STATUS), Qt.ItemDataRole.DisplayRole) == "DL中 2/5 30%"


def test_artist_column_editable_and_setdata():
    """アーティスト列は編集可。manual フラグは触らず、DONE 行は確認待ちへ戻す。"""
    done = Track(stem="a", guessed_title="song", valid=True, status=Status.DONE)
    queued = Track(stem="b")
    model = TrackTableModel([done, queued])

    assert model.flags(_idx(model, 0, COL_ARTIST)) & Qt.ItemFlag.ItemIsEditable
    assert model.setData(_idx(model, 0, COL_ARTIST), " ArtistName ", Qt.ItemDataRole.EditRole)
    assert done.artist == "ArtistName"  # strip される
    assert done.manual is False  # タイトルの再推定保護には影響しない
    assert done.status is Status.PENDING  # 書き込み済み行は再書き込み対象へ

    model.setData(_idx(model, 1, COL_ARTIST), "X", Qt.ItemDataRole.EditRole)
    assert queued.artist == "X"
    assert queued.status is Status.QUEUED  # 未処理行の状態は変えない

    assert model.data(_idx(model, 0, COL_ARTIST), Qt.ItemDataRole.DisplayRole) == "ArtistName"


def test_percent_role_for_progress_delegate():
    """PERCENT_ROLE は DL 中かつ進捗既知のときだけ数値を返す（進捗バー用）。"""
    from gui.model import PERCENT_ROLE

    t = Track(stem="a", status=Status.DOWNLOADING)
    model = TrackTableModel([t])
    assert model.data(_idx(model, 0, COL_STATUS), PERCENT_ROLE) is None  # 進捗未受信
    model.set_percent(t, 45.0)
    assert model.data(_idx(model, 0, COL_STATUS), PERCENT_ROLE) == 45.0
    assert model.data(_idx(model, 0, COL_STEM), PERCENT_ROLE) is None  # 状態列のみ
    t.status = Status.INFERRING  # DL が終わったらバーは消える
    assert model.data(_idx(model, 0, COL_STATUS), PERCENT_ROLE) is None


def test_error_tooltip():
    t = Track(stem="a", status=Status.ERROR, error="失敗の理由")
    model = TrackTableModel([t])
    assert model.data(_idx(model, 0, COL_STEM), Qt.ItemDataRole.ToolTipRole) == "失敗の理由"


# ---------------------------------------------------------------------------
# CRUD: add / replace / remove
# ---------------------------------------------------------------------------


def test_add_tracks():
    model = TrackTableModel()
    model.add_tracks([Track(stem="a"), Track(stem="b")])
    assert model.rowCount() == 2


def test_replace_track_expands_placeholder():
    placeholder = Track(stem="url", url="http://u")
    other = Track(stem="keep")
    model = TrackTableModel([placeholder, other])
    real = [Track(stem="v1"), Track(stem="v2"), Track(stem="v3")]
    model.replace_track(placeholder, real)
    stems = [model.track_at(r).stem for r in range(model.rowCount())]
    assert stems == ["v1", "v2", "v3", "keep"]


def test_replace_track_not_found_appends():
    model = TrackTableModel([Track(stem="a")])
    ghost = Track(stem="ghost", url="http://g")
    model.replace_track(ghost, [Track(stem="b")])
    assert [model.track_at(r).stem for r in range(model.rowCount())] == ["a", "b"]


def test_replace_track_uses_identity_not_equality():
    # 同じ URL を 2 回追加するとフィールドが同値の別プレースホルダになる。
    # __eq__（値比較）ではなく同一性(is)で 2 個目の行を差し替えること。
    ph1 = Track(stem="http://u", url="http://u")
    ph2 = Track(stem="http://u", url="http://u")
    assert ph1 == ph2 and ph1 is not ph2
    model = TrackTableModel([ph1, ph2])
    model.replace_track(ph2, [Track(stem="real")])
    assert model.track_at(0) is ph1
    assert model.track_at(1).stem == "real"


def test_remove_rows():
    model = TrackTableModel([Track(stem=s) for s in ("a", "b", "c", "d")])
    model.remove_rows([1, 3])
    assert [model.track_at(r).stem for r in range(model.rowCount())] == ["a", "c"]


def test_tracks_snapshot_is_copy():
    model = TrackTableModel([Track(stem="a")])
    snap = model.tracks()
    snap.append(Track(stem="b"))
    assert model.rowCount() == 1  # 元は変わらない


def test_refresh_track_emits_datachanged():
    t = Track(stem="a")
    model = TrackTableModel([t])
    seen = []
    model.dataChanged.connect(lambda tl, br, roles=None: seen.append((tl.row(), br.row())))
    t.status = Status.DONE
    model.refresh_track(t)
    assert seen == [(0, 0)]


# ---------------------------------------------------------------------------
# MainWindow スモーク
#
# 必ず restore_settings=False（テストモード）で構築すること。実 QSettings を
# 汚さないためと、ffmpeg 未検出時の起動モーダルを抑制するため — CI にはモーダル
# を閉じる者がおらず、既定引数だとジョブが 6 時間タイムアウトまでハングする。
# ---------------------------------------------------------------------------


def test_main_window_add_urls_and_files(qtbot, tmp_path):
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    assert win._model.rowCount() == 0

    added = win.add_urls(["http://a", "http://b"])
    assert added == 2
    assert win._model.rowCount() == 2
    # URL 行はプレースホルダ（url=stem, QUEUED）
    t = win._model.track_at(0)
    assert t.url == "http://a" and t.status is Status.QUEUED

    f = tmp_path / "song.mp3"
    f.write_bytes(b"\x00")
    assert win.add_files([f]) == 1
    assert win._model.rowCount() == 3
    assert win._model.track_at(2).filepath == f


def test_main_window_add_files_skips_duplicates(qtbot, tmp_path):
    """同じファイルの再追加はスキップされる（[files/ 取り込み] 連打で行が増えない）。"""
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(b"\x00")
    b.write_bytes(b"\x00")

    assert win.add_files([a]) == 1
    # 再取り込み: 既存の a は除外され、新規の b だけ追加される
    assert win.add_files([a, b]) == 1
    assert win._model.rowCount() == 2
    # 同一バッチ内の重複も 1 行にまとまる
    assert win.add_files([a, a]) == 0
    assert win._model.rowCount() == 2


def test_main_window_delete_and_dropped_paths(qtbot, tmp_path):
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    good = tmp_path / "ok.mp3"
    good.write_bytes(b"\x00")
    bad = tmp_path / "note.flac"
    bad.write_bytes(b"\x00")
    # 非対応拡張子は無視される
    win.handle_dropped_paths([good, bad])
    assert win._model.rowCount() == 1


def test_main_window_dropped_txt_is_url_list(qtbot, tmp_path):
    """.txt のドロップは URL リストとして読み込まれ、URL 行が追加される。"""
    from gui.main_window import MainWindow

    win = MainWindow(restore_settings=False)
    qtbot.addWidget(win)
    lst = tmp_path / "urls.txt"
    lst.write_text("http://a\n# コメント\nhttp://b\n", encoding="utf-8")
    win.handle_dropped_paths([lst])
    assert win._model.rowCount() == 2
    t = win._model.track_at(0)
    assert t.url == "http://a" and t.status is Status.QUEUED
