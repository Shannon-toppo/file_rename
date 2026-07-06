# -*- coding: utf-8 -*-
"""TrackTableModel: Track のリストをテーブル表示する QAbstractTableModel。

このモデルはメインスレッド（UI スレッド）でのみ操作する前提。ワーカーは
モデルに触らず、シグナル経由でメインスレッドのスロットから更新する
（詳細は workers.py の docstring を参照）。
"""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from core import Status, Track

# カラム定義（順序が表示順）
COL_STEM = 0  # 元タイトル（ファイル名 stem / URL）
COL_CHANNEL = 1  # チャンネル
COL_TITLE = 2  # 推定タイトル（編集可）
COL_ARTIST = 3  # アーティスト（編集可。推定はしない）
COL_STATUS = 4  # 状態
COL_FORMAT = 5  # 形式（拡張子）
# 行番号は Qt 標準の縦ヘッダ（headerData の Vertical）が担うため、専用の # 列は持たない
_HEADERS = ("元タイトル", "チャンネル", "推定タイトル", "アーティスト", "状態", "形式")
# 編集可能な列（クリップボード貼り付け・デリゲートで共用）
EDITABLE_COLUMNS = (COL_TITLE, COL_ARTIST)
# 値は書き換えないが、読み取り専用エディタを開いて本文の部分選択・コピーを許す列。
# EDITABLE_COLUMNS とは別扱い（貼り付け対象にはしない）。
COPYABLE_COLUMNS = (COL_STEM,)

# 状態別の背景色（QColor 直書き）
_COLOR_DONE = QColor(213, 245, 213)  # 薄緑
_COLOR_ERROR = QColor(250, 214, 214)  # 薄赤
_COLOR_WARN = QColor(250, 244, 199)  # 薄黄（PENDING かつ valid=False）
_COLOR_BUSY = QColor(214, 233, 250)  # 薄青（DL 中 / 推定中）
# 淡色背景の行に使う文字色。OS がダークテーマだと既定の文字色が白のままになり
# パステル背景に白文字が乗って読めないため、背景を塗る行は文字色も固定する。
_COLOR_TEXT_ON_TINT = QColor(32, 32, 32)

# 状態列の DL 進捗（0-100 の float、DL 中以外は None）を返すカスタムロール。
# main_window の進捗バーデリゲート(_ProgressDelegate)がこれを読む。
PERCENT_ROLE = int(Qt.ItemDataRole.UserRole) + 1

# 手動編集された行の推定タイトルに付けるプレフィックス
_MANUAL_PREFIX = "✎ "


class TrackTableModel(QAbstractTableModel):
    """Track のリストを 1 行 1 曲で表示する。

    行ごとの DL 進捗（%）は Track に持たせず、モデル内の row→percent の
    dict で保持する（Track に表示用フィールドを足さない方針）。
    """

    def __init__(self, tracks: list[Track] | None = None):
        super().__init__()
        self._tracks: list[Track] = list(tracks) if tracks else []
        # 行インデックス → (DL 進捗 0-100, 「2/5」等のリスト内番号ラベル)。
        # DL 中の行だけ入る
        self._percent: dict[int, tuple[float, str]] = {}

    # -- Qt モデルの必須オーバーライド ---------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._tracks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(_HEADERS)

    def headerData(self, section: int, orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section]
        return section + 1  # 縦ヘッダは 1-based 行番号

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        # 推定タイトル・アーティストは編集可。元タイトルは本文コピー用に
        # 読み取り専用エディタを開けるよう、同じく ItemIsEditable を付ける
        # （実際に書き換わるかはデリゲート側で決まる）。
        if index.column() in EDITABLE_COLUMNS or index.column() in COPYABLE_COLUMNS:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        track = self._tracks[index.row()]
        col = index.column()

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._display(index.row(), track, col, edit=role == Qt.ItemDataRole.EditRole)
        if role == Qt.ItemDataRole.ToolTipRole:
            return track.error or None
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._background(track)
        if role == Qt.ItemDataRole.ForegroundRole:
            # 背景を塗る行だけ文字色を固定（それ以外はテーマ既定に任せる）
            return _COLOR_TEXT_ON_TINT if self._background(track) is not None else None
        if role == PERCENT_ROLE and col == COL_STATUS:
            if track.status is Status.DOWNLOADING and index.row() in self._percent:
                return self._percent[index.row()][0]
            return None
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        """推定タイトル / アーティスト列の編集。"""
        if role != Qt.ItemDataRole.EditRole:
            return False
        if index.column() == COL_TITLE:
            # 手動編集の副作用（manual/PENDING 化・再描画）は set_title に集約する
            self.set_title(index.row(), str(value))
            return True
        if index.column() == COL_ARTIST:
            self.set_artist(index.row(), str(value))
            return True
        return False

    # -- 表示ヘルパ ----------------------------------------------------------

    def _display(self, row: int, track: Track, col: int, edit: bool = False):
        if col == COL_STEM:
            return track.stem
        if col == COL_CHANNEL:
            return track.channel or ""
        if col == COL_TITLE:
            # 編集時は素の値、表示時は手動行にプレフィックスを付ける
            if edit:
                return track.guessed_title
            if track.manual and track.guessed_title:
                return _MANUAL_PREFIX + track.guessed_title
            return track.guessed_title
        if col == COL_ARTIST:
            return track.artist
        if col == COL_STATUS:
            return self._status_text(row, track)
        if col == COL_FORMAT:
            if track.filepath is not None:
                return track.filepath.suffix.lstrip(".").lower() or "-"
            return "-"
        return None

    def _status_text(self, row: int, track: Track) -> str:
        text = track.status.value
        if track.status is Status.DOWNLOADING and row in self._percent:
            pct, label = self._percent[row]
            # 再生リストなら「DL中 2/5 45%」、単一なら「DL中 45%」
            return f"{text} {label} {pct:.0f}%" if label else f"{text} {pct:.0f}%"
        return text

    def _background(self, track: Track):
        if track.status is Status.DONE:
            return _COLOR_DONE
        if track.status is Status.ERROR:
            return _COLOR_ERROR
        if track.status in (Status.FETCHING, Status.DOWNLOADING, Status.INFERRING):
            return _COLOR_BUSY
        if track.status is Status.PENDING and track.valid is False:
            return _COLOR_WARN
        return None

    def _emit_row(self, row: int) -> None:
        top = self.index(row, 0)
        bottom = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(top, bottom)

    # -- 公開メソッド（メインスレッドからのみ呼ぶ）--------------------------

    def add_tracks(self, tracks: list[Track]) -> None:
        """末尾に行を追加する。"""
        if not tracks:
            return
        start = len(self._tracks)
        self.beginInsertRows(QModelIndex(), start, start + len(tracks) - 1)
        self._tracks.extend(tracks)
        self.endInsertRows()

    def replace_track(self, placeholder: Track, tracks: list[Track]) -> None:
        """プレースホルダ行を 1 個以上の実 Track 行へ差し替える。

        再生リストの DL 完了時に、プレースホルダ 1 行を複数行へ展開する。
        置換対象が見つからなければ末尾に追加する（フォールバック）。
        """
        # Track は dataclass で __eq__ が値比較のため、同じ URL を複数追加した
        # 場合に index() では別の行を誤って差し替える。必ず同一性(is)で探す。
        row = next((i for i, t in enumerate(self._tracks) if t is placeholder), None)
        if row is None:
            self.add_tracks(tracks)
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._tracks[row]
        self.endRemoveRows()
        # 進捗辞書は行削除でインデックスがずれるので作り直す
        self._reindex_percent()
        if not tracks:
            return
        self.beginInsertRows(QModelIndex(), row, row + len(tracks) - 1)
        self._tracks[row:row] = tracks
        self.endInsertRows()

    def refresh_track(self, track: Track) -> None:
        """該当行を再描画する（ワーカーが Track を書き換えた後に呼ぶ）。"""
        for row, t in enumerate(self._tracks):
            if t is track:
                self._emit_row(row)
                return

    def set_percent(self, track: Track, percent: float, label: str = "") -> None:
        """DL 進捗（% と「2/5」等のリスト内番号ラベル）を設定し、状態列を再描画する。"""
        for row, t in enumerate(self._tracks):
            if t is track:
                self._percent[row] = (percent, label)
                idx = self.index(row, COL_STATUS)
                self.dataChanged.emit(idx, idx)
                return

    def remove_rows(self, rows: list[int]) -> None:
        """指定行をテーブルから外す（ファイルは削除しない）。"""
        for row in sorted(set(rows), reverse=True):
            if 0 <= row < len(self._tracks):
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._tracks[row]
                self.endRemoveRows()
        self._reindex_percent()

    # -- undo/クリア用の状態スナップショット --------------------------------

    def title_state(self, row: int) -> tuple:
        """タイトル/アーティスト編集に関わる状態を undo 用タプルで取り出す。

        タプルの並び: (guessed_title, artist, manual, valid, status, error)。
        Edit 系コマンドが old/new を丸ごと保存し restore で復元する。
        """
        t = self._tracks[row]
        return (t.guessed_title, t.artist, t.manual, t.valid, t.status, t.error)

    def restore_title_state(self, row: int, state: tuple) -> None:
        """title_state で取り出したタプルを行へ書き戻し、再描画する。"""
        if not (0 <= row < len(self._tracks)):
            return
        t = self._tracks[row]
        t.guessed_title, t.artist, t.manual, t.valid, t.status, t.error = state
        self._emit_row(row)

    def set_title(self, row: int, title: str) -> None:
        """推定タイトルを手動編集として設定する（setData と同じ副作用）。

        手動フラグを立て確認待ちへ戻す。undo コマンド経由で呼ぶ想定。
        """
        if not (0 <= row < len(self._tracks)):
            return
        t = self._tracks[row]
        t.guessed_title = title.strip()
        t.manual = True
        t.status = Status.PENDING
        t.error = ""
        self._emit_row(row)

    def set_artist(self, row: int, artist: str) -> None:
        """アーティスト欄を設定する（推定はしないので manual フラグは触らない）。

        書き込み済み（DONE）の行は再書き込みが必要になるため確認待ちへ戻す。
        """
        if not (0 <= row < len(self._tracks)):
            return
        t = self._tracks[row]
        t.artist = artist.strip()
        if t.status is Status.DONE:
            t.status = Status.PENDING
            t.error = ""
        self._emit_row(row)

    def clear_title(self, row: int) -> None:
        """推定タイトルをクリアし、再推定対象へ戻す。

        編集（手動確定）とは意味が異なるため manual は立てない。
        guessed_title="" / manual=False / valid=None / QUEUED / error="" に
        リセットし、次回実行で再推定される状態にする。
        """
        if not (0 <= row < len(self._tracks)):
            return
        t = self._tracks[row]
        t.guessed_title = ""
        t.manual = False
        t.valid = None
        t.status = Status.QUEUED
        t.error = ""
        self._emit_row(row)

    def tracks(self) -> list[Track]:
        """現在の Track リストのスナップショット（浅いコピー）。"""
        return list(self._tracks)

    def track_at(self, row: int) -> Track:
        """行番号から Track を取得する。"""
        return self._tracks[row]

    def _reindex_percent(self) -> None:
        # 行構成が変わったら進捗辞書は捨てる（DL 中の並びは維持されないため）
        self._percent.clear()

    # -- ソート --------------------------------------------------------------

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """ヘッダクリックによる列ソート。self._tracks 自体を並べ替える。

        QSortFilterProxyModel を使わないのは、ワーカーが行を同一性(is)で探し、
        進捗 dict を行番号で持つため（proxy の行マッピングと相性が悪い）。
        進捗 dict は並びが崩れるためクリアする（_reindex_percent と同様）。
        """
        reverse = order == Qt.SortOrder.DescendingOrder
        self.layoutAboutToBeChanged.emit()
        self._tracks.sort(key=lambda t: self._sort_key(t, column), reverse=reverse)
        self._reindex_percent()
        self.layoutChanged.emit()

    def _sort_key(self, track: Track, column: int) -> str:
        """ソート用キー。表示文字列（プレフィックス・進捗なし）を使う。"""
        if column == COL_STEM:
            return track.stem
        if column == COL_CHANNEL:
            return track.channel or ""
        if column == COL_TITLE:
            return track.guessed_title
        if column == COL_ARTIST:
            return track.artist
        if column == COL_STATUS:
            return track.status.value
        if column == COL_FORMAT:
            if track.filepath is not None:
                return track.filepath.suffix.lstrip(".").lower()
            return ""
        return ""
