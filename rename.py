#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""files/ 内の音声ファイルを走査し、ファイル名から推測した曲名を
メタデータ（タイトルタグ）へ書き込む CLI。

実処理（推定・書き込み・スキップ方針）は core.py に共通化されている。
"""
import core


def main():
    files = core.list_music_files()
    if not files:
        print(f"No music files found in {core.FILES_DIR}")
        return

    print(f"Found {len(files)} file(s):")
    for f in files:
        print(f"  {f.name}")

    tracks = [core.track_from_file(f) for f in files]
    print("\nInferring titles...")
    try:
        core.infer_titles(tracks)
    except Exception as e:
        print(f"Error: {e}")
        return

    print("\nWriting titles to metadata:")
    core.write_tags(tracks, on_result=lambda t: print(core.describe_result(t)))


if __name__ == "__main__":
    main()
