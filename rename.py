#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent

# mv2title/.env を先に読み込んでから connect.py のモジュールレベル load_dotenv() と競合させない
load_dotenv(_ROOT / "mv2title" / ".env")

sys.path.insert(0, str(_ROOT))

from mv2title import connect, main_json  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402
from mutagen.id3._frames import TIT2  # noqa: E402
from mutagen.id3._util import ID3NoHeaderError  # noqa: E402
from mutagen.wave import WAVE  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402

FILES_DIR = Path(__file__).parent / "files"
SUPPORTED_EXTS = (".mp3", ".wav", ".m4a")


def get_music_files():
    files = []
    for ext in SUPPORTED_EXTS:
        files.extend(FILES_DIR.glob(f"*{ext}"))
    return sorted(files)


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
    connect.init()
    try:
        results = main_json.main(filenames, batch_size=5, bypass_check=True, debug_mode=False)
    except ValueError as e:
        print(f"Error: {e}")
        return

    if not results:
        print("Error: mv2title returned an empty response.")
        return

    # main_json.send_batches_json は入力順を保ったまま結果を返すので、
    # 位置で対応付ける。長さ不一致時は誤マッチを避けるため中断する。
    if len(results) != len(music_files):
        print(
            f"Error: response length ({len(results)}) does not match file count "
            f"({len(music_files)}); aborting to avoid mis-matching titles."
        )
        return

    print("\nWriting titles to metadata:")
    for filepath, obj in zip(music_files, results):
        if not obj.get("valid", False):
            print(f"  [SKIP] {filepath.name}  ->  validation failed")
            continue
        title = obj.get("title") or None
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
