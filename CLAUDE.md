# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This folder holds **consumer scripts for the `mv2title` library** (which lives in the sibling `../mv2title/` package and is its own git repo — this folder is *not* part of that repo). The scripts take real audio files, infer a clean song title with `mv2title`, and write it into the file's metadata.

- **`rename.py`** — scans `files/` for audio, infers titles, writes metadata tags.
- **`download.py`** — downloads audio from a URL (or playlist) with **yt-dlp**, then reuses `rename.py` to tag the result.
- **`files/`** — working directory for audio files (`.mp3`, `.wav`, `.m4a`).

## How it wires to mv2title

Both scripts bootstrap the same way (see top of `rename.py`):

1. `load_dotenv(_ROOT / "mv2title" / ".env")` is called **before** importing `mv2title.connect`, so the env (`BASE_URL`, `API_KEY`, `MODEL`, `SYSTEM_PROMPT`) is loaded ahead of `connect.py`'s own module-level `load_dotenv()`. Keep this ordering.
2. `sys.path.insert(0, str(_ROOT))` (parent of this folder) makes `from mv2title import connect, main_json` resolve.
3. `connect.init()` **must be called before** any inference (the library does not call it itself).

`download.py` imports `rename` directly and reuses `rename.write_title`, `rename.FILES_DIR`, `rename.connect`, and `rename.main_json` — so importing `rename` is what triggers the bootstrap above. Don't duplicate the env/path setup in new scripts; import `rename` instead.

## Pipeline specifics

- Inference uses `main_json.main(stems, batch_size=5, bypass_check=True, ...)` — `bypass_check=True` means results are returned even if validation fails, so callers **must guard against length mismatch themselves**: both scripts compare `len(results) == len(files)` and abort on mismatch to avoid mis-assigning titles to the wrong file (results are returned in input order, matched positionally).
- `write_title` is format-specific: `TIT2` frame for `.mp3`/`.wav` (mutagen ID3), `\xa9nam` atom for `.m4a` (MP4). `SUPPORTED_EXTS` / `download.py`'s `--format choices` are limited to these three.

## Running

- `python rename.py` — tags everything in `files/`.
- `python download.py <URL> [-f mp3|wav|m4a]` — single URL (default format `mp3`).
- `python download.py -a urls.txt [-f ...]` — one URL per line (blank lines and `#` comments ignored).
- A playlist URL downloads all entries; a `watch?v=...&list=...` URL downloads only the single video (`noplaylist=True`). Failed playlist entries are skipped (`ignoreerrors=True`).

## Dependencies / gotchas

- Needs **mutagen** and **yt-dlp** installed, plus **ffmpeg on PATH** for `download.py`'s mp3/wav conversion. None of these are declared in `../mv2title/pyproject.toml` — they must be present in the active environment.
- Requires a running OpenAI-compatible LLM endpoint configured via `../mv2title/.env` (see that package's CLAUDE.md).
