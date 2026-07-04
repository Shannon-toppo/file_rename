#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yt-dlp で URL の動画から音声をダウンロードし、推測したタイトルを
メタデータに書き込む CLI。

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

実処理（DL・推定・書き込み・スキップ方針）は core.py に共通化されている。
"""
import argparse
import sys
from pathlib import Path

# import 時に mv2title/.env の読み込みも行われる
import core


def process_url(url: str, fmt: str) -> None:
    """1 件の URL をダウンロードしてメタデータを書き込む。"""
    print(f"Downloading audio ({fmt}) from: {url}")
    try:
        tracks = core.download_tracks(url, fmt)
    except Exception as e:
        print(f"Error: ダウンロードに失敗しました: {e}")
        return

    print(f"Downloaded {len(tracks)} file(s):")
    for t in tracks:
        assert t.filepath is not None
        print(f"  {t.filepath.name}")

    print("Inferring titles...")
    try:
        core.infer_titles(tracks)
    except Exception as e:
        print(f"Error: {e}")
        return

    print("Writing titles to metadata:")
    core.write_tags(tracks, on_result=lambda t: print(core.describe_result(t)))


def read_urls_from_file(path: str) -> list[str]:
    """テキストファイルから URL を 1 行ずつ読み込む（空行と # 始まりの行は無視）。"""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in lines]
    return [u for u in urls if u and not u.startswith("#")]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="yt-dlp で音声をDLし、メタデータにタイトルを書き込む。"
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
        choices=core.SUPPORTED_FORMATS,
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
