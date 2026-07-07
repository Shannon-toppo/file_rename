#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller 用エントリポイント（`python -m gui` と同じ）。

PyInstaller はモジュール指定（-m gui）で解析できないため、
file_rename_gui.spec がこのスクリプトを起点にする。
"""
from gui.__main__ import main

if __name__ == "__main__":
    main()
