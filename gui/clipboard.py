# -*- coding: utf-8 -*-
"""コピー/ペーストのロジック（TSV で Excel と相互運用）。

view/clipboard から切り離した純粋関数として置き、単体テスト可能にする。
- selection_to_tsv: 選択セル範囲を TSV 文字列へ（表示値。手動 "✎ " は含めない）。
- resolve_paste_targets: TSV を展開して貼り付け対象の (行, 列, 値) を求める。
  実際の反映は MainWindow.paste_tsv_via_commands が undo コマンド経由で行う。
"""
from collections.abc import Sequence

from PySide6.QtCore import QModelIndex, Qt

from .model import EDITABLE_COLUMNS


def selection_to_tsv(model, indexes: Sequence[QModelIndex]) -> str:
    """選択セル範囲を TSV 文字列にする。

    選択された行×列の矩形を、行=改行・列=タブで並べる（飛び飛び選択は
    含まれる行・列だけで矩形を作る）。値は EditRole（素の値。手動行の
    "✎ " プレフィックスは含まない）を使う。空選択なら空文字を返す。
    """
    cells = [i for i in indexes if i.isValid()]
    if not cells:
        return ""
    rows = sorted({i.row() for i in cells})
    cols = sorted({i.column() for i in cells})
    lines = []
    for row in rows:
        values = []
        for col in cols:
            idx = model.index(row, col)
            value = model.data(idx, Qt.ItemDataRole.EditRole)
            values.append("" if value is None else str(value))
        lines.append("\t".join(values))
    return "\n".join(lines)


def resolve_paste_targets(
    model, start_index: QModelIndex, text: str
) -> list[tuple[int, int, str]]:
    """TSV を start_index 左上で展開し、反映対象の (行, 列, 値) 一覧を返す。

    編集可能列（推定タイトル / アーティスト列）に落ちるセルのみを対象にする。
    範囲外の行・列や編集不可セルは除外する。末尾の空行・CR は無視する。
    """
    if not start_index.isValid() or not text:
        return []
    start_row = start_index.row()
    start_col = start_index.column()
    # 末尾の空行は無視（Excel からのコピーは末尾改行が付きやすい）
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()

    targets: list[tuple[int, int, str]] = []
    for r_offset, line in enumerate(lines):
        line = line.rstrip("\r")  # \r\n 由来の CR を除去
        for c_offset, value in enumerate(line.split("\t")):
            row = start_row + r_offset
            col = start_col + c_offset
            if row >= model.rowCount() or col >= model.columnCount():
                continue
            # 編集可能列（推定タイトル / アーティスト）のみ反映。それ以外は無視。
            if col not in EDITABLE_COLUMNS:
                continue
            idx = model.index(row, col)
            if not (model.flags(idx) & Qt.ItemFlag.ItemIsEditable):
                continue
            targets.append((row, col, value))
    return targets
