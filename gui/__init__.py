# -*- coding: utf-8 -*-
"""file_rename の PySide6 製 GUI パッケージ。

`uv run python -m gui` で起動する。core.py（GUI 非依存のコア API）を
ワーカースレッドから呼び、テーブル（TrackTableModel）に結果を映す。
スレッド境界の規約は workers.py の docstring を参照。
"""
