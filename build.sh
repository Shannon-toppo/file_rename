#!/usr/bin/env bash
# macOS 用ビルドスクリプト: dist/FileRenameGUI.app と配布 zip を作る（onedir/.app）。
# 前提: uv がインストール済みで、../mv2title が sibling に存在すること。
# PyInstaller はクロスコンパイル不可のため、Apple Silicon の mac 実機で実行する。
set -euo pipefail
cd "$(dirname "$0")"

uv sync
uv run pyinstaller file_rename_gui.spec --noconfirm

# 配布用の同梱物（接続設定の雛形と利用手順）。.env は .app の「隣」に置く運用
cp .env.example README_dist.md dist/

# 拡張属性・署名を保ったまま zip 化（Finder 圧縮と同等。unzip は Finder 推奨）
ditto -c -k --keepParent dist/FileRenameGUI.app dist/FileRenameGUI-mac.zip

echo "done: dist/FileRenameGUI.app / dist/FileRenameGUI-mac.zip"
echo "受け取り側の初回起動: 右クリック→開く（または xattr -cr FileRenameGUI.app）"
