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
# GUI サブシステムの exe は端末に出力できないため、診断はファイルへリダイレクトする
Write-Host "動作確認: dist/FileRenameGUI/FileRenameGUI.exe --selftest > selftest.txt"
