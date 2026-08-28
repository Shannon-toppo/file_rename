#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PyInstaller 用エントリポイント（`python -m gui` と同じ）。

PyInstaller はモジュール指定（-m gui）で解析できないため、
file_rename_gui.spec がこのスクリプトを起点にする。

`--selftest [URL] [--update] [--download]` を付けると GUI を出さずに
診断だけ行う（selftest.py 参照）。凍結ビルドは console=False で画面に何も
出ないため、mac / Windows で「配布物から yt-dlp を取得・ロードできるか」を
端末から確かめる口として使う。
"""
import sys


def main() -> None:
    if "--selftest" in sys.argv[1:]:
        from selftest import run_selftest

        raise SystemExit(run_selftest(sys.argv[1:]))
    from gui.__main__ import main as gui_main

    gui_main()


if __name__ == "__main__":
    main()
