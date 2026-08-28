# FileRenameGUI 利用手順（配布版）

YouTube などの URL から音声をダウンロードし、LLM で曲名を推定してタグ（タイトル/アーティスト）を書き込むツールです。

## 1. ffmpeg を入れる（必須）

音声の変換・音量ノーマライズに ffmpeg が必要です（このアプリには同梱されていません）。

- **Windows**: `winget install Gyan.FFmpeg` （コマンドプロンプトで実行。終わったら PC を再起動するか、アプリを起動し直す）
- **macOS**: `brew install ffmpeg` （[Homebrew](https://brew.sh) が必要）

アプリ起動時に「ffmpeg が見つかりません」の警告が出なければ OK です。

## 2. yt-dlp を取得する（初回のみ・自動）

ダウンロードの実行部（yt-dlp）はアプリに同梱されていません。YouTube 側の仕様変更で
数か月ごとに使えなくなるため、アプリから更新できるように別で持つ形にしています。

初回起動時に「yt-dlp を取得しますか？」と聞かれるので [はい] を選んでください（数 MB）。
あとから [設定] → yt-dlp でも取得・更新できます。

**deno も入れておくことを推奨します。** 無くてもダウンロードはできますが、
YouTube 側の制限で速度が 10 分の 1 以下になります。

- **Windows**: `winget install DenoLand.Deno`
- **macOS**: `brew install deno`（[公式インストーラ](https://deno.land) の `~/.deno/bin` でも認識します）

## 3. LLM の接続設定

曲名の推定には OpenAI 互換の LLM エンドポイント（LM Studio・Ollama・各種 API など）が必要です。設定方法はどちらでも構いません:

- 同梱の `.env.example` を `.env` にリネームして中身を編集し、**exe（mac は .app）と同じフォルダ**に置く
- またはアプリの [設定] ボタン → 接続欄（BASE_URL など）に直接入力する（.env より優先されます）

[設定] の「接続テスト」ボタンで疎通確認できます。接続できない場合でもダウンロードだけは実行できます。

## 4. 起動

- **Windows**: `FileRenameGUI.exe` をダブルクリック
- **macOS**: 初回のみ `FileRenameGUI.app` を**右クリック → 開く**（「開発元を確認できない」と出た場合）。
  それでも「壊れている」と表示される場合はターミナルで
  `xattr -cr /path/to/FileRenameGUI.app` を実行してから開く

## 5. 使い方（概要）

1. URL を貼り付けて [追加] → [▶ 実行] でダウンロード → 曲名推定 → タグ書き込みまで自動で進みます
2. 推定タイトルはセルを直接編集できます（Ctrl+Z で元に戻す）
3. [情報取得] で再生リストの中身をダウンロード前に確認できます
4. 保存先フォルダや音量ノーマライズは [設定] から変更できます（既定はアプリの隣の `files/`）

## 補足

- YouTube 側の仕様変更でダウンロードが失敗するようになった場合は、[設定] → yt-dlp の [更新] を実行してください（アプリ本体の入れ替えは不要です。更新後は再起動してください）
- Windows Defender 等が誤検知した場合は、フォルダごと除外に登録してください

## うまく動かないとき（診断モード）

端末から `--selftest` を付けて起動すると、ffmpeg / deno の在り処、yt-dlp の版、
通信の可否をまとめて表示します（GUI は出ません）。

- **macOS**（ターミナル）:

      /path/to/FileRenameGUI.app/Contents/MacOS/FileRenameGUI --selftest

- **Windows**（コマンドプロンプト。画面には出ないのでファイルへ書き出します）:

      FileRenameGUI.exe --selftest > selftest.txt

オプション:

- `--update` — yt-dlp を取得・更新する（[設定] の [更新] と同じ。GUI が開けないときの手段）
- `--download <URL>` — 実際に 1 曲だけ落として消し、変換まで通るか確かめる
