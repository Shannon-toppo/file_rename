# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This folder holds **consumer scripts for the `mv2title` library** (which lives in the sibling `../mv2title/` package and is its own git repo — this folder is *not* part of that repo). The scripts take real audio files, infer a clean song title with `mv2title`, and write it into the file's metadata.

- **`rename.py`** — scans `files/` for audio, infers titles, writes metadata tags.
- **`download.py`** — downloads audio from a URL (or playlist) with **yt-dlp**, then reuses `rename.py` to tag the result.
- **`files/`** — working directory for audio files (`.mp3`, `.wav`, `.m4a`).

## How it wires to mv2title

Dependencies are declared in this folder's own `pyproject.toml` (uv-managed, `package = false`): **mutagen**, **yt-dlp**, **python-dotenv**, and **mv2title as an editable path dependency** (`[tool.uv.sources] mv2title = { path = "../mv2title", editable = true }`). Run `uv sync` here to create the venv — no `sys.path` manipulation anywhere.

- `rename.py` calls `load_dotenv(_ROOT / "mv2title" / ".env")` at import so the sibling package's `.env` (`BASE_URL`, `API_KEY`, `MODEL`, `SYSTEM_PROMPT`) is on `os.environ` before `Config.from_env()` reads it.
- Inference uses the mv2title 0.3.0 API: `client = LLMClient(Config.from_env())` (wrapped in `rename.make_client()`) and `extract_titles(stems, client, batch_size=5, bypass_check=True)`, which returns `TitleResult` objects (`.title` / `.valid`).
- `download.py` imports `rename` and reuses `rename.write_title`, `rename.FILES_DIR`, and `rename.make_client` — importing `rename` also triggers the dotenv load above. Don't duplicate env setup in new scripts; import `rename` instead.

## Pipeline specifics

- `bypass_check=True` means results are returned even if validation fails, so callers **must guard against length mismatch themselves**: both scripts compare `len(results) == len(files)` and abort on mismatch to avoid mis-assigning titles to the wrong file (results are returned in input order, matched positionally).
- `write_title` is format-specific: `TIT2` frame for `.mp3`/`.wav` (mutagen ID3), `\xa9nam` atom for `.m4a` (MP4). `SUPPORTED_EXTS` / `download.py`'s `--format choices` are limited to these three.

## Running

- `uv sync` — once, to create the venv with all dependencies.
- `uv run python rename.py` — tags everything in `files/`.
- `uv run python download.py <URL> [-f mp3|wav|m4a]` — single URL (default format `mp3`).
- `uv run python download.py -a urls.txt [-f ...]` — one URL per line (blank lines and `#` comments ignored).
- A playlist URL downloads all entries; a `watch?v=...&list=...` URL downloads only the single video (`noplaylist=True`). Failed playlist entries are skipped (`ignoreerrors=True`).

## Dependencies / gotchas

- **ffmpeg on PATH** is still required for `download.py`'s mp3/wav conversion (not pip-installable).
- Requires a running OpenAI-compatible LLM endpoint configured via `../mv2title/.env` (see that package's CLAUDE.md).
