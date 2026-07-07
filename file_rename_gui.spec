# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ビルド定義（Windows / macOS 共通・onedir）。

使い方: uv run pyinstaller file_rename_gui.spec --noconfirm
（通常は build.ps1 / build.sh 経由で実行する。ffmpeg は同梱しない —
 利用者が PATH に用意する。CLAUDE.md の「パッケージング」参照）
"""
import sys

APP_NAME = "FileRenameGUI"

a = Analysis(
    ["run_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # QtMultimedia（試聴機能）は gui/player.py で遅延 import されるため明示する。
    # yt-dlp は自前の PyInstaller フックを同梱しており追加指定は不要
    hiddenimports=["PySide6.QtMultimedia"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
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
