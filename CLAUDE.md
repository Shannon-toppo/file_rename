# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This folder holds **consumer scripts for the `mv2title` library** (which lives in the sibling `../mv2title/` package and is its own git repo — this folder is *not* part of that repo). The scripts take real audio files, infer a clean song title with `mv2title`, and write it into the file's metadata.

- **`core.py`** — GUI-agnostic shared core (never prints): loads `../mv2title/.env`, defines `Track`/`Status`, and implements `download_tracks` (yt-dlp, progress callback + cancel `threading.Event`), `infer_titles` (batched, protects `manual=True` rows unless `force=True`), `write_tags` (skip policy lives here), `write_title`, `describe_result`.
- **`rename.py`** — thin CLI: scans `files/` for audio, then `core.infer_titles` + `core.write_tags`.
- **`download.py`** — thin CLI: `core.download_tracks` per URL (channel name rides along as an artist hint), then infer + write via core.
- **`gui/`** — PySide6 GUI (table-based) on top of `core.py`. See the GUI section below.
- **`files/`** — working directory for audio files (`.mp3`, `.wav`, `.m4a`).

## How it wires to mv2title

Dependencies are declared in this folder's own `pyproject.toml` (uv-managed, `package = false`): **mutagen**, **yt-dlp**, **python-dotenv**, and **mv2title as an editable path dependency** (`[tool.uv.sources] mv2title = { path = "../mv2title", editable = true }`). Run `uv sync` here to create the venv — no `sys.path` manipulation anywhere.

- `core.py` calls `load_dotenv(_ROOT / "mv2title" / ".env")` at import so the sibling package's `.env` (`BASE_URL`, `API_KEY`, `MODEL`, `SYSTEM_PROMPT`) is on `os.environ` before `Config.from_env()` reads it. Don't duplicate env setup in new scripts; import `core` instead.
- Inference uses the mv2title 0.3.0 API: `client = LLMClient(Config.from_env())` (wrapped in `core.make_client()`) and `extract_titles(inputs, client, batch_size=5, bypass_check=True)` with `TitleInput(stem, channel=...)` items; results are `TitleResult` objects (`.title` / `.valid`).
- `core.download_tracks` extracts `entry["channel"]` (falling back to `entry["uploader"]`) from yt-dlp into `Track.channel`, so the LLM prompt gets a `[チャンネル名]` artist hint.

## Pipeline specifics

- `bypass_check=True` means results are returned even if validation fails (no in-library retry/raise), so core **guards itself**: `infer_titles` raises `CoreError` (marking all targets `ERROR`) on `len(results) != len(targets)` to avoid mis-assigning titles (results are returned in input order, matched positionally). `write_tags` skips `valid=False` and empty-title rows — except `manual=True` rows, which are written on the user's authority.
- `write_title` is format-specific: `TIT2` frame for `.mp3`/`.wav` (mutagen ID3), `\xa9nam` atom for `.m4a` (MP4); other extensions raise `ValueError`. `SUPPORTED_EXTS` / `download.py`'s `--format` choices are limited to these three.

## Running

- `uv sync` — once, to create the venv with all dependencies.
- `uv run python rename.py` — tags everything in `files/`.
- `uv run python download.py <URL> [-f mp3|wav|m4a]` — single URL (default format `mp3`).
- `uv run python download.py -a urls.txt [-f ...]` — one URL per line (blank lines and `#` comments ignored).
- A playlist URL downloads all entries; a `watch?v=...&list=...` URL downloads only the single video (`noplaylist=True`). Failed playlist entries are skipped (`ignoreerrors=True`).

## GUI (`gui/`)

A PySide6 table UI that runs the full **URL → download → infer → write** flow (plus manual correction). Launch with `uv run python -m gui`. It is a thin view over `core.py`; the GUI never re-implements pipeline logic.

- **Structure**: `__main__.py` (QApplication + MainWindow), `model.py` (`TrackTableModel`, a `QAbstractTableModel`), `workers.py` (`PipelineWorker` as a `QRunnable` + `WorkerSignals`), `main_window.py` (`MainWindow`).
- **Threading contract (strict — spelled out in `workers.py`'s docstring)**: one pipeline at a time (`QThreadPool.globalInstance()`; MainWindow disables the run buttons while running). The worker **never touches QWidgets or the model** — it calls `core` functions, mutates `Track` dataclass fields, and emits signals. All model updates happen in MainWindow slots on the **main thread** via **queued connections** (`track_updated` → `model.refresh_track`, `tracks_ready` → `model.replace_track`, `progress` → `model.set_percent`, plus `error`/`finished`). UI reads a `Track` only after receiving its signal (relying on the queued-connection happens-before). Cancellation is a `threading.Event` (`set()` by the stop button, passed to `core.download_tracks`; infer/write check it at stage boundaries and raise `CancelledError`).
- **Row lifecycle**: adding a URL inserts a placeholder `Track` (`stem=url`, `url=url`, `status=QUEUED`) immediately; on download completion `tracks_ready` replaces that placeholder with the real `Track` row(s) (a playlist expands to several rows). Inference batches all `QUEUED`/`PENDING`, non-`manual` rows into **one** `core.infer_titles` call (never per-row). Auto-write ON → `core.write_tags`; OFF → stops at `PENDING`.
- **Model specifics**: columns `#` / 元タイトル(stem) / チャンネル / 推定タイトル / アーティスト / 状態 / 形式. Title and artist columns are editable (`EDITABLE_COLUMNS`); `setData` on the title column (delegating to `set_title`) sets `guessed_title`, `manual=True`, `status=PENDING`, and manual rows render with a `✎ ` prefix. The artist column (`set_artist`) is **never inferred** — typed manually or bulk-filled from `Track.channel` via the [チャンネル名→アーティスト] button (undoable macro); editing it doesn't touch `manual`, but flips `DONE` rows back to `PENDING` so they get rewritten (`core.write_title` writes `TPE1`/`©ART` only when artist is non-empty). After any write, the worker emits `write_summary(done, skipped, errors)` shown in the status bar. Playlist downloads show "DL中 2/5 45%" (yt-dlp `playlist_index`/`n_entries` flow through `on_progress` → the `progress` signal's label arg → the percent dict's `(percent, label)` tuple). `clear_title(row)` resets a row (empty title / `manual=False` / `valid=None` / `QUEUED`) for re-inference. `sort(column, order)` reorders `self._tracks` in place (no `QSortFilterProxyModel`; `#` column is a no-op, percent dict cleared). Download percent lives in a `row→percent` dict inside the model (no display field added to `Track`). Background colors: DONE=green / ERROR=red / PENDING+`valid is False`=yellow / DOWNLOADING·INFERRING=blue; tinted rows also force a dark `ForegroundRole` (OS dark themes default to white text, unreadable on pastels). While DOWNLOADING, the status column exposes `PERCENT_ROLE` and `_ProgressDelegate` (in `main_window.py`) paints a determinate progress bar instead of text.
- **Excel-style ops (phase 4)**: `gui/clipboard.py` (`selection_to_tsv` / `paste_tsv` / `resolve_paste_targets` — pure, unit-tested) drives `Ctrl+C`/`Ctrl+V` (TSV, title column only on paste); `gui/commands.py` has `QUndoCommand` subclasses (`EditTitleCommand` / `ClearTitleCommand`) so **all UI title edits route through a `QUndoStack`** (`Ctrl+Z`/`Ctrl+Y`; paste is one macro). The title-column edit delegate (`_UndoEditDelegate`) pushes a command instead of calling `setData`, avoiding double-apply. Right-click menu (`build_context_menu`), header-click sort (disabled while running), and `QSettings("mv2title", "file_rename_gui")` window/column/toggle persistence (`closeEvent`; skipped when `MainWindow(restore_settings=False)`). Tests create MainWindow via a `main_window` fixture that closes+deletes it (multiple undestroyed `QMainWindow`s crash pytest-qt's event processing on Windows).
- **Robustness (phase 5)**: `core.check_connection()` does a lightweight `GET {BASE_URL}/models` via `urllib` (3 s timeout, no LLM completion) — `PipelineWorker` calls it at the start of `MODE_FULL` and **degrades to download-only** (`connection_failed` signal, rows stay `QUEUED`) when the endpoint is unreachable; `skip_infer=True` forces this. `gui/settings_dialog.py` edits out_dir / default format / batch size / auto-write default (persisted via `QSettings`; `.env` values shown read-only + a connection-test button). `gui/logpanel.py` has `QtLogHandler(QObject, logging.Handler)` which **only emits a signal** (handlers run on worker threads — never touch widgets there); `LogPanel` receives it via a queued connection; attached to the `mv2title`/`core`/`yt_dlp` loggers and detached in `closeEvent`. **yt-dlp** doesn't use `logging` (it prints to stdout), so `PipelineWorker` passes `logger=logging.getLogger("yt_dlp")` (with `quiet=True`) into `core.download_tracks` to route its output through logging — GUI DLs thus show in the log panel. **Level filtering is done in one place — on the handler** (`handler.setLevel`, default WARNING, chosen via the [設定] ログレベル combo, `LOG_LEVELS` / `options/log_level` persisted); the target loggers themselves are set to DEBUG in `attach_handler` so records reach the handler. Missing ffmpeg → startup status-bar warning.
- **Tests**: fully offline. `tests/conftest.py` sets `QT_QPA_PLATFORM=offscreen` before any Qt import. `test_gui_model.py` covers columns/editability/manual prefix/colors/CRUD + a MainWindow smoke test; `test_gui_workers.py` monkeypatches `core.download_tracks`/`infer_titles`/`write_tags` and uses `qtbot.waitSignal` to verify the pipeline (auto-write, pending stop, per-row error isolation, cancellation, force re-infer, degraded mode) — an **autouse fixture stubs `core.check_connection`** so `MODE_FULL` never hits the network; when adding fakes for `core.download_tracks`, keep the signature in sync (`out_dir=None` included) or the worker's per-row `except` will silently absorb the `TypeError`. Run everything with `uv run pytest tests/ -q`.

## Dependencies / gotchas

- **ffmpeg on PATH** is still required for `download.py`'s mp3/wav conversion (not pip-installable).
- Requires a running OpenAI-compatible LLM endpoint configured via `../mv2title/.env` (see that package's CLAUDE.md).
- Indentation here is **4 spaces** (unlike the mv2title package, which uses tabs); comments/docstrings are Japanese.
