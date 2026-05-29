#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yt-dlp で URL の動画から音声をダウンロードし、rename.py を用いて
推測したタイトルをメタデータに書き込むスクリプト。

使い方:
    python download.py <URL> [--format mp3|wav|m4a]
    python download.py -a urls.txt [--format mp3|wav|m4a]

    --format を省略した場合は mp3 でダウンロードします。
    -a / --batch-file にテキストファイルを指定すると、
    記入された URL を 1 行ずつ順に処理します
    （空行と # で始まる行は無視します）。

注意:
    mp3 / wav への変換には ffmpeg が PATH 上に必要です。
    （見つからない場合は yt-dlp が変換時にエラーを出します）
"""
import argparse
import sys
from pathlib import Path

from yt_dlp import YoutubeDL

# rename.py（同階層）を再利用する。import 時に rename 側の
# load_dotenv / sys.path 設定 / mv2title import がまとめて行われる。
import rename


# rename.py が対応しているフォーマットのみを許可する（write_title の対応に合わせる）
SUPPORTED_FORMATS = ("mp3", "wav", "m4a")
# 既存ファイルと同じ命名規則（タイトル [動画ID].拡張子）に合わせる
OUTTMPL = str(rename.FILES_DIR / "%(title)s [%(id)s].%(ext)s")


def download_audio(url: str, fmt: str) -> list[Path]:
    """URL の音声を指定形式でダウンロードし、保存先パスのリストを返す。

    再生リスト URL の場合は含まれる各動画をダウンロードする。
    （noplaylist=True のため、動画＋リスト混在 URL は動画1本のみ対象）
    """
    rename.FILES_DIR.mkdir(parents=True, exist_ok=True)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": OUTTMPL,
        "noplaylist": True,
        "ignoreerrors": True,  # 一部の動画が失敗してもリスト全体を止めない
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
            }
        ],
    }

    downloaded: list[Path] = []
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if not info:
            raise RuntimeError("情報を取得できませんでした（URL を確認してください）。")

        # 再生リストなら entries を、単一動画ならそれ自身を対象にする
        entries = info["entries"] if "entries" in info else [info]
        for entry in entries:
            if not entry:
                # ignoreerrors により失敗した項目は None になる
                continue
            # ダウンロード前の拡張子のままのパスが返るため、変換後の拡張子に差し替える
            path = Path(ydl.prepare_filename(entry)).with_suffix(f".{fmt}")
            if path.exists():
                downloaded.append(path)
            else:
                print(f"  [WARN] 出力ファイルが見つかりません: {path.name}")

    if not downloaded:
        raise FileNotFoundError("ダウンロードした音声ファイルが見つかりません。")
    return downloaded


def tag_with_rename(filepaths: list[Path]) -> None:
    """rename.py のパイプラインを用いて、ファイル名からタイトルを推測しメタデータへ書き込む。"""
    if not filepaths:
        return

    rename.connect.init()
    stems = [f.stem for f in filepaths]
    try:
        results = rename.main_json.main(
            stems, batch_size=5, bypass_check=True, debug_mode=False
        )
    except ValueError as e:
        print(f"Error: {e}")
        return

    if not results:
        print("Error: mv2title returned an empty response.")
        return

    # 入力順を保ったまま返るので位置で対応付ける。長さ不一致は誤マッチを避けて中断。
    if len(results) != len(filepaths):
        print(
            f"Error: response length ({len(results)}) does not match file count "
            f"({len(filepaths)}); aborting to avoid mis-matching titles."
        )
        return

    for filepath, obj in zip(filepaths, results):
        title = obj.get("title") or None
        if not title:
            print(f"  [SKIP] {filepath.name}  ->  empty title")
            continue
        try:
            rename.write_title(filepath, title)
        except Exception as e:
            print(f"  [ERR] {filepath.name}  ->  {e}")
            continue
        print(f"  [OK] {filepath.name}  ->  {title}")


def read_urls_from_file(path: str) -> list[str]:
    """テキストファイルから URL を 1 行ずつ読み込む（空行と # 始まりの行は無視）。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in lines]
    return [u for u in urls if u and not u.startswith("#")]


def process_url(url: str, fmt: str) -> None:
    """1 件の URL をダウンロードしてメタデータを書き込む。"""
    print(f"Downloading audio ({fmt}) from: {url}")
    try:
        filepaths = download_audio(url, fmt)
    except Exception as e:
        print(f"Error: ダウンロードに失敗しました: {e}")
        return

    print(f"Downloaded {len(filepaths)} file(s):")
    for fp in filepaths:
        print(f"  {fp.name}")
    print("Writing titles to metadata:")
    tag_with_rename(filepaths)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="yt-dlp で音声をDLし、rename.py でメタデータにタイトルを書き込む。"
    )
    parser.add_argument("url", nargs="?", help="ダウンロードする動画の URL")
    parser.add_argument(
        "-a",
        "--batch-file",
        help="URL を 1 行ずつ記入したテキストファイル（空行と # 始まりの行は無視）",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=SUPPORTED_FORMATS,
        default="mp3",
        help="保存する音声形式（既定: mp3）",
    )
    args = parser.parse_args()

    if bool(args.url) == bool(args.batch_file):
        parser.error("URL または -a/--batch-file のどちらか一方を指定してください。")

    if args.batch_file:
        try:
            urls = read_urls_from_file(args.batch_file)
        except OSError as e:
            print(f"Error: ファイルを読み込めません: {e}")
            sys.exit(1)
        if not urls:
            print(f"Error: 有効な URL がありません: {args.batch_file}")
            sys.exit(1)
    else:
        urls = [args.url]

    total = len(urls)
    for i, url in enumerate(urls, 1):
        if total > 1:
            print(f"\n===== [{i}/{total}] =====")
        process_url(url, args.format)


if __name__ == "__main__":
    main()
