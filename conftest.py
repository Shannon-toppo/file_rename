# -*- coding: utf-8 -*-
"""pytest 設定: リポジトリ直下を sys.path に載せる（package = false のため）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
