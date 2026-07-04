# -*- coding: utf-8 -*-
"""QUndoStack 用のコマンド（タイトル編集・ペースト・Delete クリアの undo/redo）。

設計方針:
- UI からのタイトル変更は「すべて QUndoStack に積む」構造にする。ビューの
  編集デリゲート確定（closeEditor）やキーボード操作（Delete）・ペーストを拾い、
  コマンド化してから model を触る。これで「stack 経由」と「直接 setData」の
  二重適用を防ぐ。
- old/new は model.title_state() のタプル (guessed_title, artist, manual,
  valid, status, error) を丸ごと保存し、restore で完全復元する（manual/status も戻る）。
- ペーストは複数セル分のコマンドを 1 つの macro にまとめる（beginMacro/endMacro）
  ので、1 回の undo で貼り付け全体が戻る。行削除の undo はスコープ外。
"""
from PySide6.QtGui import QUndoCommand


class _StateCommand(QUndoCommand):
    """行のタイトル状態タプルを old/new で入れ替えるコマンドの基底。

    redo で new 状態、undo で old 状態を復元する。old/new は
    model.title_state() が返す 5 要素タプル。
    """

    def __init__(self, model, row: int, old_state: tuple, new_state: tuple, text: str):
        super().__init__(text)
        self._model = model
        self._row = row
        self._old = old_state
        self._new = new_state

    def redo(self) -> None:
        self._model.restore_title_state(self._row, self._new)

    def undo(self) -> None:
        self._model.restore_title_state(self._row, self._old)


class EditTitleCommand(_StateCommand):
    """推定タイトルの手動編集を undo/redo するコマンド。

    row の現在状態を old として保存し、new_title を手動編集として適用した
    後の状態を new として保存する。生成時点で編集は適用済みになる。
    """

    def __init__(self, model, row: int, new_title: str):
        old_state = model.title_state(row)
        model.set_title(row, new_title)
        new_state = model.title_state(row)
        super().__init__(model, row, old_state, new_state, "タイトル編集")


class EditArtistCommand(_StateCommand):
    """アーティスト欄の編集を undo/redo するコマンド（生成時点で適用済み）。"""

    def __init__(self, model, row: int, new_artist: str):
        old_state = model.title_state(row)
        model.set_artist(row, new_artist)
        new_state = model.title_state(row)
        super().__init__(model, row, old_state, new_state, "アーティスト編集")


class ClearTitleCommand(_StateCommand):
    """Delete による推定タイトルのクリアを undo/redo するコマンド。"""

    def __init__(self, model, row: int):
        old_state = model.title_state(row)
        model.clear_title(row)
        new_state = model.title_state(row)
        super().__init__(model, row, old_state, new_state, "タイトルをクリア")
