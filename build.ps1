# Windows 用ビルドスクリプト: dist/FileRenameGUI/ と配布 zip を作る（onedir）。
# 前提: uv がインストール済みで、../mv2title が sibling に存在すること。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

uv sync
uv run pyinstaller file_rename_gui.spec --noconfirm

# 配布用の同梱物（接続設定の雛形と利用手順）
Copy-Item .env.example dist/FileRenameGUI/
Copy-Item README_dist.md dist/FileRenameGUI/

# フォルダごと zip 化（展開すると FileRenameGUI/ が出てくる形）。Compress-Archive は
# Windows PowerShell 5.1 だとエントリ区切りが "\" になり他ツールでの展開が崩れるうえ、
# PySide6 を含む大きなフォルダでは遅いので、ZipArchive に "/" 区切りで直接書き込む
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$src = (Resolve-Path dist/FileRenameGUI).Path
$zipPath = Join-Path (Split-Path $src -Parent) "FileRenameGUI-win.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath }
$prefixLen = (Split-Path $src -Parent).Length + 1
$archive = [System.IO.Compression.ZipFile]::Open($zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    Get-ChildItem $src -Recurse -File -Force | ForEach-Object {
        $entry = $_.FullName.Substring($prefixLen).Replace("\", "/")
        [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $_.FullName, $entry, [System.IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $archive.Dispose()
}

Write-Host "done: dist/FileRenameGUI/ / dist/FileRenameGUI-win.zip"
# GUI サブシステムの exe は端末に出力できないため、診断はファイルへリダイレクトする
Write-Host "動作確認: dist/FileRenameGUI/FileRenameGUI.exe --selftest > selftest.txt"
