# file_rename

[mv2title](https://github.com/Shannon-toppo/mv2title) ライブラリを利用して、音声ファイルの**曲名をメタデータ（タイトルタグ）に書き込む**ためのスクリプト集です。

YouTube などの MV はファイル名・タイトル（アーティスト名・`feat.`・`Official Music Video`・括弧類）をローカル LLM で曲名に整形し、ファイルのタグへ書き込みます。

## 構成

| ファイル | 役割 |
|:--|:--|
| `rename.py` | `files/` 内の音声を走査し、ファイル名から曲名を推測してメタデータに書き込む |
| `download.py` | yt-dlp で URL（または再生リスト）から音声をDLし、続けて `rename` のロジックでタグ付け |
| `files/` | 処理対象の音声を置く作業フォルダ（中身はバージョン管理対象外） |
| `list.txt` | `download.py -a` に渡す URL 一覧の例 |

> このリポジトリは隣接する `../mv2title/` パッケージに依存します。別環境で使う場合は、`mv2title` を同じ親フォルダ直下に clone してください（`download.py` / `rename.py` が `../mv2title` を `sys.path` 経由で参照します）。

## 必要なもの

- **Python >= 3.12**
- Python パッケージ: `mutagen`, `yt-dlp`, `openai`, `python-dotenv`
  ```bash
  pip install mutagen yt-dlp openai python-dotenv
  ```
- **ffmpeg**（`download.py` の mp3 / wav 変換に必要。PATH を通しておくこと）
- **OpenAI 互換の LLM エンドポイント**（LM Studio / llama.cpp など）。設定は `../mv2title/.env` で行います（`BASE_URL` / `API_KEY` / `MODEL` / `SYSTEM_PROMPT`）。詳細は mv2title 側の README を参照。

## 使い方

### 既存ファイルにタグ付け

`files/` に音声（`.mp3` / `.wav` / `.m4a`）を置いて実行します。

```bash
python rename.py
```

ファイル名から曲名を推測し、各ファイルのタイトルタグに書き込みます。

### URL からダウンロードしてタグ付け

```bash
# 単一URL（形式の既定は mp3）
python download.py "https://www.youtube.com/watch?v=XXXX"

# 形式を指定
python download.py "https://www.youtube.com/watch?v=XXXX" -f wav

# テキストファイルから一括（1行1URL、空行と # 始まりの行は無視）
python download.py -a list.txt -f mp3
```

| オプション | 既定 | 説明 |
|:--|:--:|:--|
| `url`（位置引数） | — | ダウンロードする動画 URL |
| `-a`, `--batch-file` | — | URL を1行ずつ記入したテキストファイル |
| `-f`, `--format` | `mp3` | 保存形式（`mp3` / `wav` / `m4a`） |

`url` と `-a` はどちらか一方を指定します。

#### 再生リストの扱い

- `playlist?list=...`（純粋な再生リスト）→ 含まれる全動画をDL
- `watch?v=...&list=...`（動画＋リスト混在）→ その**動画1本のみ**DL
- 再生リスト中の取得失敗（非公開・地域制限など）はスキップして続行します

## 仕組み

両スクリプトとも処理の流れは `ファイル名 → mv2title で曲名推測 → 形式に応じたタグへ書き込み` です。

- 推測は `mv2title` の JSON モード（`main_json`）を使用します。
- タグ書き込み（`write_title`）は形式別です: `.mp3` / `.wav` は ID3 の `TIT2` フレーム、`.m4a` は MP4 の `\xa9nam` アトム。
- LLM 応答とファイル数が一致しない場合は、誤ったファイルにタイトルを付けるのを避けるため処理を中断します。

