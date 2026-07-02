#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from pathlib import Path

from dotenv import load_dotenv
from mutagen.id3 import ID3
from mutagen.id3._frames import TIT2
from mutagen.id3._util import ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.wave import WAVE
from mv2title import Config, LLMClient, extract_titles

_ROOT = Path(__file__).parent.parent

# 接続設定は mv2title 側の .env を共用する（Config.from_env() が環境変数を読む前に載せる）
load_dotenv(_ROOT / "mv2title" / ".env")

FILES_DIR = Path(__file__).parent / "files"
SUPPORTED_EXTS = (".mp3", ".wav", ".m4a")


def get_music_files():
    files = []
    for ext in SUPPORTED_EXTS:
        files.extend(FILES_DIR.glob(f"*{ext}"))
    return sorted(files)


def make_client() -> LLMClient:
    """mv2title/.env の設定で LLMClient を作る（download.py からも使う）。"""
    return LLMClient(Config.from_env())


def write_title(filepath: Path, title: str):
    ext = filepath.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.save(str(filepath))
    elif ext == ".wav":
        audio = WAVE(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        audio.save(str(filepath))
    elif ext == ".m4a":
        audio = MP4(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags["\xa9nam"] = [title]
        audio.save()


def main():
    music_files = get_music_files()
    if not music_files:
        print(f"No music files found in {FILES_DIR}")
        return

    filenames = [f.stem for f in music_files]
    print(f"Found {len(filenames)} file(s):")
    for f in music_files:
        print(f"  {f.name}")

    print("\nSending to mv2title...")
    try:
        client = make_client()
        results = extract_titles(filenames, client, batch_size=5, bypass_check=True)
    except ValueError as e:
        print(f"Error: {e}")
        return

    if not results:
        print("Error: mv2title returned an empty response.")
        return

    # extract_titles は入力と同数・同順で返す契約だが、誤マッチはファイルを壊すため
    # 念のため長さを確認してから位置で対応付ける。
    if len(results) != len(music_files):
        print(
            f"Error: response length ({len(results)}) does not match file count "
            f"({len(music_files)}); aborting to avoid mis-matching titles."
        )
        return

    print("\nWriting titles to metadata:")
    for filepath, res in zip(music_files, results):
        if not res.valid:
            print(f"  [SKIP] {filepath.name}  ->  validation failed")
            continue
        title = res.title or None
        if not title:
            print(f"  [SKIP] {filepath.name}  ->  empty title")
            continue
        try:
            write_title(filepath, title)
        except Exception as e:
            print(f"  [ERR] {filepath.name}  ->  {e}")
            continue
        print(f"  [OK] {filepath.name}  ->  {title}")


if __name__ == "__main__":
    main()
