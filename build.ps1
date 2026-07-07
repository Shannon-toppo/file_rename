# Windows 用ビルドスクリプト: dist/FileRenameGUI/ を作る（onedir）。
# 前提: uv がインストール済みで、../mv2title が sibling に存在すること。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

uv sync
uv run pyinstaller file_rename_gui.spec --noconfirm

# 配布用の同梱物（接続設定の雛形と利用手順）
Copy-Item .env.example dist/FileRenameGUI/
Copy-Item README_dist.md dist/FileRenameGUI/

Write-Host "done: dist/FileRenameGUI/ （zip に固めて配布してください）"
