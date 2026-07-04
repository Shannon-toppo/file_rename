# file_rename 第 2 期計画: UX 修正・GitHub 公開・Mac 対応

作成日: 2026-07-05 / 前提: GUI 版フェーズ 0〜5 完了([gui-plan.md](gui-plan.md))、テスト 103 件全緑

## 背景

1. [チャンネル名→アーティスト] 押下後に行選択が解除されて見え、続けて [選択行を書き込み] を押せない(UX)
2. GitHub プライベートリポジトリへの公開(origin は設定済み・da91671 まで push 済み。GUI 化の作業は未コミット)
3. Mac 対応(ユーザーは Mac 開発初心者、実機あり)

## 委譲可否の凡例

[gui-plan.md](gui-plan.md) と同一: 【S】Sonnet 委譲可 / 【O】Opus 委譲可 / 【F】Fable(メインセッション)で実施。

## 決定記録

| 日付 | 決定 |
|---|---|
| 2026-07-05 | list.txt(個人 URL、追跡中・リモート履歴にも存在)は**追跡だけ外す**(`git rm --cached`)。履歴の残存はプライベートリポジトリのため許容。**公開リポジトリ化する場合は `git filter-repo` での履歴書き換えが必要** |
| 2026-07-05 | コミットは**フェーズごとに分割**。ただし作業が同一ファイルに累積しているため、分割はファイル境界まで(コア / GUI / ドキュメント / リポジトリ衛生の 4 コミット) |
| 2026-07-05 | LICENSE はプライベートリポジトリのため追加しない(公開時に再検討) |
| 2026-07-05 | Mac 実機あり。CI(macOS ランナー)+ 実機チェックリストの二段構え |

## GitHub 公開前の監査結果(2026-07-05 実施)

- ✅ `.env`(接続設定)・`files/`(音源、著作物)は gitignore 済みで履歴にも無し
- ✅ コミットメールは GitHub noreply(`62864827+Shannon-toppo@users.noreply.github.com`)
- ⚠ `list.txt` が追跡中(gitignore エントリはあるが追跡済みのため無効)→ フェーズ B で解除
- ⚠ `.gitattributes` 無し(mv2title で CRLF × フォーマッタ衝突を踏んだ)→ フェーズ B で追加
- ℹ mv2title への editable パス依存のため、**利用側は 2 リポジトリを同じ親フォルダに clone する必要がある**(README 記載済み。CI でも同じ配置を再現する)

---

## フェーズ A: 選択状態の維持(UX 修正) ✅ 実施済み (2026-07-05)

| タスク | 委譲 | 状態 |
|---|---|---|
| `_on_fill_artists` 後に対象行を再選択し、テーブルへフォーカスを戻す(`_select_rows` 新設)。ボタン押下でフォーカスが外れると選択が非アクティブ色(ダークテーマではほぼ不可視)になるのが「解除された」ように見える原因 | 【F】(小規模のため直接) | ✅ |
| 回帰テスト: 選択行が維持される / 未選択→全行のケースでは埋めた行が選択される | 【S】 | ✅ 104 テスト全緑 |

## フェーズ B: GitHub 公開準備 ✅ 実施済み (2026-07-05)

| タスク | 委譲 | 状態 |
|---|---|---|
| `git rm --cached list.txt`(ファイルは残す)+ README の記述更新 | 【F】git 操作 | ✅ |
| `.gitattributes`(`* text=auto eol=lf`)追加 + 作業コピーの LF 正規化 | 【F】git 操作 | ✅ |
| 未コミット作業を 4 コミットへ分割: ① コア分離(core.py / CLI / tests/test_core.py / pyproject) ② GUI 本体(gui/ + tests) ③ ドキュメント(README / CLAUDE.md / docs/) ④ リポジトリ衛生(list.txt 解除 + .gitattributes) | 【F】git 操作 | ✅ 15bab34 / 25007ce / 050f00d / 6ee32d1 |
| コミット一覧を提示 → push | 【F】 | ✅ |

## フェーズ C: CI(GitHub Actions) ✅ 実施済み (2026-07-05)

| タスク | 委譲 | 状態 |
|---|---|---|
| `.github/workflows/ci.yml`: windows-latest + macos-latest で `QT_QPA_PLATFORM=offscreen uv run pytest`。mv2title を隣接 path に checkout(パス依存の再現。**mv2title は公開と確認済み**なので PAT 不要) | 【F】(小規模のため直接) | ✅ 初回実行 success(両 OS 緑) |
| Linux は Qt の追加システムライブラリが必要なため対象外(必要になったら追加) | — | 記録のみ |

## フェーズ D: Mac 対応

| タスク | 委譲 | 状態 |
|---|---|---|
| コード監査: pathlib 統一済み / ショートカットは `QKeySequence.StandardKey`(Mac では自動で Cmd 割当)/ Delete・Backspace 両対応済み / QSettings・offscreen テストはクロスプラットフォーム → コード変更は原則不要の見込み | 【F】 | ✅ 変更不要を確認(CI の macOS ジョブ緑) |
| `docs/macos-setup.md` 新設: Mac 初心者向け手順書(Homebrew → uv/ffmpeg/git → 2 リポジトリ clone → LM Studio → .env → uv sync → 起動)+ トラブルシュート | 【O】委譲(Fable レビュー) | ✅ レビューで誤字 1 件修正 |
| README のショートカット表記に Mac(Cmd)を併記 | 【S】(上記と同一エージェント) | ✅ ペースト対象列の古い記述も同時に修正 |
| 実機確認チェックリスト(下記)をユーザーが Mac で実施 | 【F】ユーザー | ⏸ ユーザー実施待ち |

### Mac 実機チェックリスト

1. `uv run pytest tests/ -q` が全緑(オフライン)
2. `uv run python -m gui` で起動、テーマ(システム/ライト/ダーク)切替
3. URL 1 件で DL → 推定 → 書き込みの一気通貫(LM Studio 起動状態)
4. Cmd+C / Cmd+V / Cmd+Z がテーブルで機能する
5. Delete(Mac の delete = Backspace)でタイトルクリア
6. ログパネルに yt-dlp ログが出る(レベル「詳細」で確認)
7. 書き込んだ m4a/mp3 のタグを Finder の情報 / Music.app で確認
