# file_rename ⇄ mv2title 実装移動の調査

調査日: 2026-09-12（file_rename `f94520d` / mv2title 0.4.1）

移動すべきものは 2 件。どちらも mv2title のバージョンアップを伴うので、まとめて実施するのが効率的。

## 移動すべき実装

### 1. モデル名の解決と差し替え検出（file_rename → mv2title）

対象は [core.py:447-602](../core.py#L447) の次の関数群。

- `_fetch_model_ids`（`GET {base_url}/models`、ボディが `data` 配列かまで確認）
- `_model_aliases` / `resolve_model`（`gemma-4-e2b` → `google/gemma-4-e2b` の解決）
- `_ModelCheckedClient` / `ModelMismatchError`（応答の `model` 欄を照合する）
- `check_connection` の判定部分

移すべき理由は 3 つある。

- **LM Studio がモデルを黙って差し替える問題は、mv2title 単体でも起きる。** `uv run mv2title --model gemma-4-e2b` は今も別のモデルに答えさせてしまう。これは音声やタグとは関係なく、OpenAI 互換エンドポイントへの接続そのものの問題なので、`connect.py` が持つのが自然。
- **`_ModelCheckedClient` は mv2title の内部挙動に依存している。** 「一度不一致を見たら送らずに例外を投げる」のは、`send_batches` が送信時の例外を「`response_format` が拒否された」と見なしてプレーンで再送するから（[pipeline.py:72-81](../../mv2title/src/mv2title/pipeline.py#L72)）。別リポジトリの非公開の実装を前提にしているので、mv2title 側でこのフォールバックを変えると file_rename が静かに壊れる。ライブラリ側に置けば、`send_batches` が不一致の例外を再送しないように直接書ける。
- mv2title の CLI と `connect._selftest` もそのまま使えるようになる。

移すときの注意点:

- 今のエラー文言は「[設定] の MODEL を〜」と GUI 向けに書かれている。ライブラリ側の文言は中立にして、GUI 向けの言い回しは core が付け足す形に分ける。
- 通信は urllib で書かれているが、ライブラリ側では `self._client.models.list()`（openai SDK）でも書ける。ただし「LM Studio は存在しないパスにも 200 でエラー JSON を返す」への対策は、そのまま残す必要がある。
- テストも一緒に移る（[tests/test_core.py:1485-1690](../tests/test_core.py#L1485) の約 15 件）。mv2title は minor バージョンを上げ、`__init__` から export する。

### 2. `Config.from_env()` の暗黙の `load_dotenv()`（mv2title 本体 → mv2title の CLI）

[connect.py:60](../../mv2title/src/mv2title/connect.py#L60) は引数なしで `load_dotenv()` を呼んでいる。python-dotenv はこの場合、**呼び出し元のソースファイルの場所から上へ `.env` を探す**。そのため、どこから使っても `mv2title/.env` が勝手に読み込まれる。

- file_rename の core は `.env` を自前で管理している（`find_env_file` と `apply_env_overrides` で、上書き > プロセス環境 > `.env` の優先順位）。ところがライブラリがもう一つ別の `.env` を足してしまう。例えば `file_rename/.env` に MODEL が無いと、`mv2title/.env` の MODEL が黙って効く。設定画面のプレースホルダ（`env_defaults()`）にはこの値が出ない。
- 凍結ビルドでは探し始める場所がカレントディレクトリになるので、起動の仕方によって結果が変わる。
- `.env` の読み込みは `cli.main()` に移し、ライブラリ本体は環境変数だけを読む形にするのが筋。mv2title を使う側から見ると挙動が変わるので、CHANGELOG に書いておく必要がある。

## 検討したうえで移さないもの

- **yt-dlp / YouTube innertube / mutagen の処理**（`download_tracks`、`_fetch_ytmusic_song`、`read_tags` / `write_title` など）: どれもダウンロードとタグ付けの話で、タイトル推定ライブラリの範囲外。
- **`infer_titles` の件数チェックと `EMPTY_TITLE_ERROR`**: `Track` の状態を管理するための防御なので、利用側に置くのが正しい。
- **`USE_SCHEMA` と `--no-structured-output`**: `extract_titles(use_schema=)` をそのまま渡しているだけで、重複はない。

## 移動ではないが、調査中に見つけた整理候補

- **`_retry_missing_titles` のコメントアウト**（[core.py:1399-1465](../core.py#L1399)）: mv2title の CHANGELOG に「将来的に削除してよい」とある。ただし CLAUDE.md では意図してメモとして残していると明記されているので、消すかどうかは要判断。
- **`mv2title-gui.spec`**（file_rename に git 管理されたまま）: 古い spec。ffmpeg を同梱し、`collect_submodules('yt_dlp')` で yt-dlp も同梱する設定で、今の方針（どちらも同梱しない）と逆。現行は `file_rename_gui.spec` なので、誤ってこちらでビルドすると事故になる。
- **mv2title の CLAUDE.md「Consumer scripts」節**: 内容が古い。`core.py` や GUI、opus 対応に触れておらず、「download.py が rename.py の write_title を再利用する」という今は無い構成を説明している。
- **`.claude/worktrees/elated-robinson-977df4`**: 古い worktree（detached HEAD）が登録されたまま残っている。

## 実施状況（2026-09-12）

- 移動 1・2: mv2title 0.5.0（Shannon-toppo/mv2title#11）と file_rename 側の追従（#25）で完了。
- 整理候補: `mv2title-gui.spec` と古い worktree は #24 で整理。`_retry_missing_titles` は #25 で削除。mv2title の CLAUDE.md「Consumer scripts」節は #11 で書き直し。
