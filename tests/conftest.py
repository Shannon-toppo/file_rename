# -*- coding: utf-8 -*-
"""GUI テスト用の共通設定。

Qt をヘッドレス（オフスクリーン）で動かすため、import より前に
QT_QPA_PLATFORM を設定する。この行はファイル最上部に置くこと。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
