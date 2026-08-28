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

EJS（yt-dlp-ejs）
=================
YouTube の署名・n チャレンジを解く JavaScript は yt-dlp 本体には入っておらず、
`yt-dlp-ejs` パッケージか、実行時の遠隔取得（--remote-components ejs:github /
ejs:npm）で供給する。ここでは前者を採り、yt-dlp と同じバージョンディレクトリへ
一緒に展開する（install_ejs）。ダウンロードのたびに GitHub / npm を叩かずに
済み、版は yt-dlp の METADATA のピンで決まり、スクリプトは yt-dlp 自身が
ハッシュ照合する。
"""
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "FileRenameGUI"
PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
# yt-dlp-ejs（EJS）: YouTube の署名・n チャレンジを解く JavaScript 一式。
# 無いと yt-dlp は GitHub / npm から都度取りに行こうとし（--remote-components）、
# それも無ければ解けずに速度が落ちたり一部の形式が落ちたりする。
# パッケージとして置いてあれば最優先で使われ、yt-dlp が持つハッシュ一覧で
# 検証される（実行のたびに外部から取ってこない）。版は yt-dlp 側が
# `Requires-Dist: yt-dlp-ejs==X` で厳密に固定しているのでそれに従う。
EJS_PACKAGE = "yt_dlp_ejs"
EJS_PYPI_JSON = "https://pypi.org/pypi/yt-dlp-ejs/{version}/json"
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
    return _module_version(extracted / "yt_dlp" / "version.py", "__version__")


def _module_version(source: Path, *names: str) -> str | None:
    """バージョン定数だけのモジュールを exec して、最初に見つかった名前を返す。

    import せずに読むのは、まだ sys.path に載っていないパッケージを解決
    できないため。読めない・構造が変わったときは None（表示不能で済ませる）。
    """
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    namespace: dict = {}
    try:
        exec(compile(text, str(source), "exec"), namespace)  # noqa: S102
    except Exception:  # noqa: BLE001 - 構造が変わっても表示不能で済ませる
        return None
    for name in names:
        value = namespace.get(name)
        if isinstance(value, str):
            return value
    return None


# ---------------------------------------------------------------------------
# EJS（yt-dlp-ejs）
# ---------------------------------------------------------------------------


def ejs_version(extracted: Path | None = None) -> str | None:
    """展開済み yt-dlp-ejs のバージョン。無ければ None。

    Args:
        extracted: 見に行くディレクトリ。省略時は使用中の yt-dlp と同じ場所。
    """
    path = extracted if extracted is not None else installed_dir()
    if path is None:
        return None
    # setuptools-scm 生成の _version.py（__version__ と version の両方を持つ）
    return _module_version(path / EJS_PACKAGE / "_version.py", "__version__", "version")


def required_ejs_version(extracted: Path) -> str | None:
    """展開済み yt-dlp が要求する yt-dlp-ejs のバージョン。判別できなければ None。

    yt-dlp の wheel の METADATA には `Requires-Dist: yt-dlp-ejs==0.8.0` の形で
    完全固定のピンが入っている。スクリプト側でも版が合わないと弾かれる
    （yt-dlp が同梱のハッシュ一覧と突き合わせる）ので、勝手に最新を入れず
    ここを唯一の基準にする。
    """
    for metadata in sorted(extracted.glob("yt_dlp-*.dist-info/METADATA")):
        try:
            text = metadata.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = re.search(r"^Requires-Dist:\s*yt[-_]dlp[-_]ejs\s*==\s*([^\s;]+)", text, re.M)
        if found:
            return found.group(1)
    return None


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


def install_ejs(target: Path) -> str:
    """target（展開済み yt-dlp）の隣へ、対応する yt-dlp-ejs を展開して版を返す。

    yt-dlp と同じディレクトリに置くので sys.path のエントリは 1 つのままで済み、
    prune() で世代ごと一緒に消える（版の組み合わせがずれない）。
    取得する版は required_ejs_version()（yt-dlp の METADATA のピン）に従う。

    既に同じ版が入っていれば何もしない。差し替えは新しいツリーを staging へ
    展開してから入れ替えるので、失敗しても既存の EJS は残る。

    Raises:
        YtdlpUnavailable: 版を判別できない、取得・展開に失敗した場合。
    """
    version = required_ejs_version(target)
    if version is None:
        raise YtdlpUnavailable(
            "yt-dlp が要求する yt-dlp-ejs の版を判別できませんでした（METADATA が読めません）。"
        )
    if ejs_version(target) == version:
        return version  # 既に対応版が入っている

    url = _ejs_wheel_url(version)
    staging = target.parent / f".staging-ejs-{os.getpid()}-{version}"
    package = target / EJS_PACKAGE
    retired = target.parent / f".old-ejs-{os.getpid()}-{version}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        with urllib.request.urlopen(url, timeout=_WHEEL_TIMEOUT) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            archive.extractall(staging)
        if not (staging / EJS_PACKAGE / "__init__.py").is_file():
            raise YtdlpUnavailable(f"wheel に {EJS_PACKAGE} パッケージが含まれていません。")
        # 旧版を退避 → 新版を配置 → 退避を削除（入れ替え中に消えている時間を作らない）
        if package.is_dir():
            package.rename(retired)
        (staging / EJS_PACKAGE).rename(package)
    except YtdlpUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - 通信・IO をまとめて扱う
        if not package.is_dir() and retired.is_dir():
            retired.rename(package)  # 入れ替えに失敗したら旧版へ戻す
        raise YtdlpUnavailable(f"yt-dlp-ejs の取得に失敗しました: {e}") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)
    return version


def _ejs_wheel_url(version: str) -> str:
    """指定バージョンの yt-dlp-ejs wheel の URL を PyPI から引く。"""
    try:
        with urllib.request.urlopen(
            EJS_PYPI_JSON.format(version=version), timeout=_META_TIMEOUT
        ) as resp:
            data = json.load(resp)
        return next(f["url"] for f in data["urls"] if f["packagetype"] == "bdist_wheel")
    except Exception as e:  # noqa: BLE001 - 通信・構造変更をまとめて扱う
        raise YtdlpUnavailable(
            f"yt-dlp-ejs {version} の情報を取得できませんでした: {e}"
        ) from e


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
