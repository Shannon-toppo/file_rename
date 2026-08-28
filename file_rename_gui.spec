# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ビルド定義（Windows / macOS 共通・onedir）。

使い方: uv run pyinstaller file_rename_gui.spec --noconfirm
（通常は build.ps1 / build.sh 経由で実行する。ffmpeg も yt-dlp も同梱しない —
 ffmpeg は利用者が PATH に用意し、yt-dlp はアプリが実行時に取得する。
 CLAUDE.md の「パッケージング」参照）
"""
import sys
from pathlib import Path

import PySide6

APP_NAME = "FileRenameGUI"

# Qt 標準文言の日本語カタログ（テキスト欄の右クリックメニュー等）。
# PyInstaller の PySide6 フックは translations を丸ごとは持ってこないため、
# 使う 1 ファイルだけ明示的に同梱する（gui/i18n.py が読み込む）
_qt_translations = Path(PySide6.__file__).resolve().parent / "Qt" / "translations"
_datas = [
    (str(_qt_translations / "qtbase_ja.qm"), "PySide6/Qt/translations")
] if (_qt_translations / "qtbase_ja.qm").exists() else []

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=[],
    datas=_datas,
    # QtMultimedia（試聴機能）は gui/player.py で遅延 import されるため明示する。
    # yt-dlp は自前の PyInstaller フックを同梱しており追加指定は不要（解析だけ
    # させて下の _strip_ytdlp で PYZ から抜く。標準ライブラリの依存はここで拾う）
    hiddenimports=["PySide6.QtMultimedia"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
# yt-dlp は同梱しない（YouTube の仕様変更で数か月ごとに使えなくなり、その都度
# 再ビルド・再配布になるため）。実体は実行時にユーザー領域へ展開する — ytdlp_runtime 参照。
#
# ここで excludes=["yt_dlp"] を使ってはいけない: 解析が yt-dlp の import を辿らなく
# なるため、yt-dlp だけが必要とする標準ライブラリ（optparse など）まで exe から
# 落ちて、外部の yt-dlp を読み込んだ時点で ModuleNotFoundError になる。
# 解析は通常どおり行わせ、収集結果から yt_dlp.* だけを抜く。
def _strip_ytdlp(toc):
    return [e for e in toc if not (e[0] == "yt_dlp" or e[0].startswith("yt_dlp."))]


_before = len(a.pure)
a.pure = _strip_ytdlp(a.pure)
a.datas = _strip_ytdlp(a.datas)
print(f"[spec] yt_dlp を PYZ から除外: {_before - len(a.pure)} モジュール")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    upx=False,
    console=False,  # GUI アプリ（コンソール窓を出さない）
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name=APP_NAME,
)

if sys.platform == "darwin":
    # mac は .app バンドルにする。codesign_identity 未指定 → arm64 では
    # PyInstaller が自動で ad-hoc 署名する（身内配布前提。公証はしない）
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier="com.mv2title.file-rename-gui",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleDevelopmentRegion": "ja",
        },
    )
