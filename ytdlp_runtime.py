#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt-dlp を exe に同梱せず、実行時にユーザー領域へ取得・更新する。

なぜ同梱しないか
================
YouTube の仕様変更で yt-dlp は数か月で使えなくなる（署名・n チャレンジが
解けなくなり全 DL が 403 になる）。PYZ に焼き込むと、その都度 exe を再ビルド
して配り直すことになるため、実体を配布物の外に置いて GUI から更新できるようにする。
`file_rename_gui.spec` は yt_dlp を **excludes せず**、解析だけさせて PYZ から
抜く（excludes すると yt-dlp だけが必要とする標準ライブラリ — optparse など —
まで exe から落ちて ModuleNotFoundError になる）。

置き場所
========
`data_root()`（Windows: %LOCALAPPDATA%、macOS: ~/Library/Application Support）
の下にバージョン名のディレクトリを作って wheel を展開する:

    <data_root>/runtime/2026.8.19/yt_dlp/...

配布物の隣（`core.app_dir()/runtime`）に同名の構成があればそちらを優先する
（オフライン配布用の事前同梱）。アプリの隣を既定にしないのは macOS の
App Translocation 対策 — quarantine 付きの .app は読み取り専用の
ランダムパスへ複製されて実行されるため、隣は書き込めるとは限らない。

wheel は `py3-none-any`（純 Python・必須依存ゼロ）なので、pip を使わず
urllib と zipfile だけで展開でき、Windows / macOS で同じファイルが使える。
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "FileRenameGUI"
PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
# 取得・展開のタイムアウト（秒）。wheel は 3MB 程度
_META_TIMEOUT = 30.0
_WHEEL_TIMEOUT = 180.0
# 残す世代数。不良リリースを引いたときに 1 つ前へ戻せるようにする
_KEEP_GENERATIONS = 2


class YtdlpUnavailable(RuntimeError):
    """yt-dlp の実体が無く、ロードできない。"""


# ---------------------------------------------------------------------------
# バージョン
# ---------------------------------------------------------------------------


def parse_version(value: str) -> tuple[int, ...]:
    """バージョン文字列を比較可能なタプルにする。

    PyPI は "2026.8.19"、yt_dlp/version.py は "2026.08.19" と桁揃えが違うため、
    文字列のままだと同じ版を別物と判定してしまう。数値化して吸収する。
    数値でない部分（dev 版など）が混じったら (-1,) を返して常に「古い」扱いにする。
    """
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return (-1,)


# ---------------------------------------------------------------------------
# 置き場所
# ---------------------------------------------------------------------------


def data_root() -> Path:
    """OS ごとのユーザーデータ領域（アプリ専用）。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / APP_NAME


def _bundled_root() -> Path | None:
    """配布物に事前同梱された runtime/（オフライン配布用）。無ければ None。"""
    # core は実行時にこのモジュールを読むため、循環を避けて遅延 import する
    import core

    path = core.app_dir() / "runtime"
    return path if path.is_dir() else None


def runtime_root() -> Path:
    """yt-dlp を展開する親ディレクトリ。事前同梱があればそれを優先する。"""
    bundled = _bundled_root()
    if bundled is not None and _versions_in(bundled):
        return bundled
    return data_root() / "runtime"


def _versions_in(root: Path) -> list[tuple[tuple[int, ...], Path]]:
    """root 直下の「展開済み yt-dlp」を (バージョン, パス) の昇順で返す。"""
    if not root.is_dir():
        return []
    found = []
    for child in root.iterdir():
        if not (child / "yt_dlp" / "version.py").is_file():
            continue  # 展開途中の残骸や無関係なディレクトリは無視する
        found.append((parse_version(child.name), child))
    return sorted(found)


def installed_dir() -> Path | None:
    """使用する（= 最も新しい）展開済みディレクトリ。無ければ None。"""
    found = _versions_in(runtime_root())
    return found[-1][1] if found else None


def installed_version() -> str | None:
    """展開済み yt-dlp のバージョン文字列。無ければ None。

    ディレクトリ名ではなく yt_dlp/version.py の実値を読む（表示用）。
    """
    path = installed_dir()
    if path is None:
        return None
    return _read_version(path)


def _read_version(extracted: Path) -> str | None:
    """展開済みツリーの yt_dlp/version.py から __version__ を読む。"""
    source = extracted / "yt_dlp" / "version.py"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    # version.py は自動生成の定数だけなので exec して読む（import すると
    # まだ sys.path に載っていないパッケージを解決できない）
    namespace: dict = {}
    try:
        exec(compile(text, str(source), "exec"), namespace)  # noqa: S102
    except Exception:  # noqa: BLE001 - 構造が変わっても表示不能で済ませる
        return None
    version = namespace.get("__version__")
    return version if isinstance(version, str) else None


# ---------------------------------------------------------------------------
# 取得・更新
# ---------------------------------------------------------------------------


def latest_release() -> tuple[str, str]:
    """PyPI の最新版 (バージョン, wheel の URL) を返す。

    Raises:
        YtdlpUnavailable: 通信に失敗した、または wheel が見つからない場合。
    """
    try:
        with urllib.request.urlopen(PYPI_JSON, timeout=_META_TIMEOUT) as resp:
            data = json.load(resp)
        version = data["info"]["version"]
        url = next(f["url"] for f in data["urls"] if f["packagetype"] == "bdist_wheel")
    except Exception as e:  # noqa: BLE001 - 通信・構造変更をまとめて扱う
        raise YtdlpUnavailable(f"最新版の情報を取得できませんでした: {e}") from e
    return version, url


def install(version: str, url: str) -> Path:
    """wheel を取得して <runtime_root>/<version>/ へ展開し、そのパスを返す。

    展開は一時ディレクトリへ行ってから rename する。途中で失敗しても
    使用中のバージョンを壊さない（rename は同一ボリューム内なので原子的）。

    Raises:
        YtdlpUnavailable: 取得・展開に失敗した場合。
    """
    root = runtime_root()
    target = root / version
    if (target / "yt_dlp" / "version.py").is_file():
        return target  # 既に展開済み

    staging = root / f".staging-{os.getpid()}-{version}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
        with urllib.request.urlopen(url, timeout=_WHEEL_TIMEOUT) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            archive.extractall(staging)
        if not (staging / "yt_dlp" / "version.py").is_file():
            raise YtdlpUnavailable("wheel に yt_dlp パッケージが含まれていません。")
        staging.rename(target)
    except YtdlpUnavailable:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001 - 通信・IO をまとめて扱う
        shutil.rmtree(staging, ignore_errors=True)
        if (target / "yt_dlp" / "version.py").is_file():
            return target  # 併走した別プロセスが先に置いた
        raise YtdlpUnavailable(f"yt-dlp の取得に失敗しました: {e}") from e

    prune()
    return target


def prune(keep: int = _KEEP_GENERATIONS) -> None:
    """古い世代を削除する（新しい方から keep 個を残す）。"""
    found = _versions_in(runtime_root())
    for _, path in found[:-keep] if keep > 0 else found:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# ロード
# ---------------------------------------------------------------------------


def is_available() -> bool:
    """ロードできる状態か（展開済み、または通常の import で解決できる）。

    load() と違い sys.path も import も動かさない（起動時の判定用）。
    """
    if installed_dir() is not None:
        return True
    return importlib.util.find_spec("yt_dlp") is not None


def load() -> str:
    """展開済み yt-dlp を sys.path へ載せ、そのバージョンを返す。

    展開済みが無ければ、通常の import で解決できる yt_dlp（開発環境の venv）に
    委ねる。どちらも無ければ YtdlpUnavailable。

    Raises:
        YtdlpUnavailable: yt-dlp が見つからない場合。
    """
    path = installed_dir()
    if path is not None:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        return _read_version(path) or path.name

    # 開発環境（凍結していない venv）では素の import で解決できる
    try:
        import yt_dlp
    except ImportError as e:
        raise YtdlpUnavailable(
            "yt-dlp が見つかりません。[設定] から取得してください。"
        ) from e
    return getattr(yt_dlp.version, "__version__", "unknown")
