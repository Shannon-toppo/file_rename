# -*- coding: utf-8 -*-
"""ytdlp_runtime のオフラインテスト（ネットワークには一切触らない）。

wheel の取得は urlopen を差し替えて、その場で組んだ zip を返させる。
"""
import io
import json
import zipfile

import pytest

import core
import ytdlp_runtime


def _wheel_bytes(version: str, ejs: str | None = "0.8.0") -> bytes:
    """yt-dlp の wheel を模した zip を作る（yt_dlp/version.py だけ入っていれば足りる）。

    METADATA には本物と同じ形の EJS のピンを入れる（required_ejs_version が読む）。
    """
    metadata = "Name: yt-dlp\n"
    if ejs is not None:
        metadata += f"Requires-Dist: yt-dlp-ejs=={ejs}; extra == 'default'\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "")
        archive.writestr("yt_dlp/version.py", f"__version__ = {version!r}\n")
        archive.writestr(f"yt_dlp-{version}.dist-info/METADATA", metadata)
    return buffer.getvalue()


def _ejs_wheel_bytes(version: str, package: str = "yt_dlp_ejs") -> bytes:
    """yt-dlp-ejs の wheel を模した zip を作る。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "from ._version import version\n")
        archive.writestr(f"{package}/_version.py", f"__version__ = version = {version!r}\n")
    return buffer.getvalue()


def _extract(root, version: str, reported: str | None = None, ejs: str | None = "0.8.0") -> None:
    """<root>/<version>/ に展開済みツリーを直接作る（取得を経ずに用意する）。"""
    package = root / version / "yt_dlp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "version.py").write_text(
        f"__version__ = {(reported or version)!r}\n", encoding="utf-8"
    )
    if ejs is not None:
        info = root / version / f"yt_dlp-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(
            f"Name: yt-dlp\nRequires-Dist: yt-dlp-ejs=={ejs}; extra == 'default'\n",
            encoding="utf-8",
        )


def _extract_ejs(root, version: str, ytdlp_version: str) -> None:
    """展開済み yt-dlp の隣に EJS を直接置く。"""
    package = root / ytdlp_version / "yt_dlp_ejs"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from ._version import version\n", encoding="utf-8")
    (package / "_version.py").write_text(
        f"__version__ = version = {version!r}\n", encoding="utf-8"
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """runtime_root() を一時ディレクトリへ向ける。"""
    root = tmp_path / "runtime"
    monkeypatch.setattr(ytdlp_runtime, "runtime_root", lambda: root)
    return root


# ---------------------------------------------------------------------------
# バージョン比較
# ---------------------------------------------------------------------------


def test_parse_version_absorbs_zero_padding():
    """PyPI の "2026.8.19" と version.py の "2026.08.19" を同じ版とみなす。

    ここを文字列比較にすると、最新版を入れた直後にまた「更新あり」と出る。
    """
    assert ytdlp_runtime.parse_version("2026.8.19") == ytdlp_runtime.parse_version("2026.08.19")


def test_parse_version_orders_numerically():
    assert ytdlp_runtime.parse_version("2026.8.19") > ytdlp_runtime.parse_version("2026.6.9")


def test_parse_version_treats_unparsable_as_oldest():
    """dev 版など数値化できない版は常に「古い」扱い（更新を妨げない）。"""
    assert ytdlp_runtime.parse_version("2026.8.27.3630.dev0") < ytdlp_runtime.parse_version("1.0")


# ---------------------------------------------------------------------------
# 展開済みの探索
# ---------------------------------------------------------------------------


def test_installed_dir_is_none_when_empty(runtime):
    assert ytdlp_runtime.installed_dir() is None
    assert ytdlp_runtime.installed_version() is None


def test_installed_dir_picks_newest(runtime):
    _extract(runtime, "2026.6.9")
    _extract(runtime, "2026.8.19")
    assert ytdlp_runtime.installed_dir() == runtime / "2026.8.19"


def test_installed_version_reads_version_py(runtime):
    """表示はディレクトリ名ではなく version.py の実値を使う。"""
    _extract(runtime, "2026.8.19", reported="2026.08.19")
    assert ytdlp_runtime.installed_version() == "2026.08.19"


def test_incomplete_dirs_are_ignored(runtime):
    """展開途中の残骸（yt_dlp/version.py が無い）は候補にしない。"""
    _extract(runtime, "2026.6.9")
    (runtime / ".staging-123-2026.8.19").mkdir()
    (runtime / "2026.8.19").mkdir()
    assert ytdlp_runtime.installed_dir() == runtime / "2026.6.9"


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_download(monkeypatch):
    """urlopen を差し替え、任意の bytes を返させる。"""

    def install(payload: bytes | Exception):
        def fake_urlopen(url, timeout=None):
            if isinstance(payload, Exception):
                raise payload
            return io.BytesIO(payload)

        monkeypatch.setattr(ytdlp_runtime.urllib.request, "urlopen", fake_urlopen)

    return install


def test_install_extracts_wheel(runtime, fake_download):
    fake_download(_wheel_bytes("2026.8.19"))
    path = ytdlp_runtime.install("2026.8.19", "https://example.invalid/x.whl")
    assert path == runtime / "2026.8.19"
    assert ytdlp_runtime.installed_version() == "2026.8.19"


def test_install_is_a_noop_when_already_present(runtime, fake_download):
    """展開済みなら通信しない（urlopen が呼ばれたら例外で落ちる細工）。"""
    _extract(runtime, "2026.8.19")
    fake_download(AssertionError("通信してはいけない"))
    assert ytdlp_runtime.install("2026.8.19", "https://example.invalid/x.whl") == runtime / "2026.8.19"


def test_failed_install_keeps_existing_version(runtime, fake_download):
    """取得に失敗しても使用中のバージョンを壊さず、残骸も残さない。"""
    _extract(runtime, "2026.6.9")
    fake_download(OSError("network down"))
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.install("2026.8.19", "https://example.invalid/x.whl")
    assert ytdlp_runtime.installed_dir() == runtime / "2026.6.9"
    assert not list(runtime.glob(".staging-*"))


def test_install_rejects_archive_without_package(runtime, fake_download):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.txt", "not a wheel")
    fake_download(buffer.getvalue())
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.install("2026.8.19", "https://example.invalid/x.whl")
    assert not list(runtime.glob(".staging-*"))


def test_prune_keeps_newest_generations(runtime):
    for version in ("2026.4.1", "2026.6.9", "2026.8.19"):
        _extract(runtime, version)
    ytdlp_runtime.prune(keep=2)
    remaining = sorted(p.name for p in runtime.iterdir())
    assert remaining == ["2026.6.9", "2026.8.19"]


# ---------------------------------------------------------------------------
# ロード
# ---------------------------------------------------------------------------


def test_load_puts_extracted_dir_on_sys_path(runtime, monkeypatch):
    _extract(runtime, "2026.8.19", reported="2026.08.19")
    monkeypatch.setattr(ytdlp_runtime.sys, "path", list(ytdlp_runtime.sys.path))
    assert ytdlp_runtime.load() == "2026.08.19"
    assert str(runtime / "2026.8.19") in ytdlp_runtime.sys.path


def test_is_available_without_extracted_tree(runtime, monkeypatch):
    """展開済みが無くても、通常の import で解決できるなら利用可能。"""
    monkeypatch.setattr(ytdlp_runtime.importlib.util, "find_spec", lambda name: object())
    assert ytdlp_runtime.is_available() is True
    monkeypatch.setattr(ytdlp_runtime.importlib.util, "find_spec", lambda name: None)
    assert ytdlp_runtime.is_available() is False


def test_load_raises_when_nothing_available(runtime, monkeypatch):
    def missing(name, *args, **kwargs):
        raise ImportError("no yt_dlp")

    monkeypatch.delitem(ytdlp_runtime.sys.modules, "yt_dlp", raising=False)
    monkeypatch.setattr("builtins.__import__", missing)
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.load()


# ---------------------------------------------------------------------------
# core との接続
# ---------------------------------------------------------------------------


def test_ensure_ytdlp_is_a_noop_when_already_set(monkeypatch):
    """差し替え済み（テストのフェイク）なら sys.path にも import にも触らない。

    これが崩れると、既存テストの monkeypatch.setattr(core, "YoutubeDL", ...) が
    実 yt-dlp のロードを誘発する。
    """
    sentinel = object()
    monkeypatch.setattr(core, "YoutubeDL", sentinel)

    def explode():
        raise AssertionError("load() を呼んではいけない")

    monkeypatch.setattr(ytdlp_runtime, "load", explode)
    core.ensure_ytdlp()
    assert core.YoutubeDL is sentinel


def test_ensure_ytdlp_wraps_unavailable_as_core_error(monkeypatch):
    """未取得は CoreError になり、ワーカーがそのまま UI へ出せる。"""
    monkeypatch.setattr(core, "YoutubeDL", None)

    def missing():
        raise ytdlp_runtime.YtdlpUnavailable("yt-dlp が見つかりません。")

    monkeypatch.setattr(ytdlp_runtime, "load", missing)
    with pytest.raises(core.CoreError):
        core.ensure_ytdlp()


# ---------------------------------------------------------------------------
# EJS（yt-dlp-ejs）
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_ejs_download(monkeypatch):
    """PyPI のメタデータ問い合わせと wheel 取得を URL で振り分けて差し替える。"""

    def install(wheel: bytes | Exception, *, url: str = "https://example.invalid/ejs.whl"):
        def fake_urlopen(request_url, timeout=None):
            if "pypi.org" in request_url:
                body = {"urls": [{"packagetype": "bdist_wheel", "url": url}]}
                return io.BytesIO(json.dumps(body).encode())
            if isinstance(wheel, Exception):
                raise wheel
            return io.BytesIO(wheel)

        monkeypatch.setattr(ytdlp_runtime.urllib.request, "urlopen", fake_urlopen)

    return install


def test_required_ejs_version_reads_metadata_pin(runtime):
    """取得する版は yt-dlp の METADATA のピンで決める（最新を勝手に入れない）。

    版がずれると yt-dlp 側のハッシュ照合で弾かれ、EJS が無いのと同じになる。
    """
    _extract(runtime, "2026.8.19", ejs="0.8.0")
    assert ytdlp_runtime.required_ejs_version(runtime / "2026.8.19") == "0.8.0"


def test_required_ejs_version_is_none_without_pin(runtime):
    _extract(runtime, "2026.8.19", ejs=None)
    assert ytdlp_runtime.required_ejs_version(runtime / "2026.8.19") is None


def test_ejs_version_reads_installed_tree(runtime):
    _extract(runtime, "2026.8.19")
    assert ytdlp_runtime.ejs_version() is None
    _extract_ejs(runtime, "0.8.0", "2026.8.19")
    assert ytdlp_runtime.ejs_version() == "0.8.0"


def test_install_ejs_extracts_next_to_ytdlp(runtime, fake_ejs_download):
    """EJS は yt-dlp と同じディレクトリへ置く（sys.path 1 本・世代ごと一緒に消える）。"""
    _extract(runtime, "2026.8.19", ejs="0.8.0")
    fake_ejs_download(_ejs_wheel_bytes("0.8.0"))
    assert ytdlp_runtime.install_ejs(runtime / "2026.8.19") == "0.8.0"
    assert ytdlp_runtime.ejs_version() == "0.8.0"
    assert not list(runtime.glob(".staging-ejs-*"))


def test_install_ejs_is_a_noop_when_version_matches(runtime, fake_ejs_download):
    _extract(runtime, "2026.8.19", ejs="0.8.0")
    _extract_ejs(runtime, "0.8.0", "2026.8.19")
    fake_ejs_download(AssertionError("通信してはいけない"))
    assert ytdlp_runtime.install_ejs(runtime / "2026.8.19") == "0.8.0"


def test_install_ejs_replaces_mismatched_version(runtime, fake_ejs_download):
    """本体の更新でピンが変わったら、古い EJS を入れ替える。"""
    _extract(runtime, "2026.8.19", ejs="0.9.0")
    _extract_ejs(runtime, "0.8.0", "2026.8.19")
    fake_ejs_download(_ejs_wheel_bytes("0.9.0"))
    assert ytdlp_runtime.install_ejs(runtime / "2026.8.19") == "0.9.0"
    assert ytdlp_runtime.ejs_version() == "0.9.0"


def test_failed_install_ejs_keeps_existing(runtime, fake_ejs_download):
    """取得に失敗しても、今使えている EJS を消してしまわない。"""
    _extract(runtime, "2026.8.19", ejs="0.9.0")
    _extract_ejs(runtime, "0.8.0", "2026.8.19")
    fake_ejs_download(OSError("network down"))
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.install_ejs(runtime / "2026.8.19")
    assert ytdlp_runtime.ejs_version() == "0.8.0"
    assert not list(runtime.glob(".staging-ejs-*"))
    assert not list(runtime.glob(".old-ejs-*"))


def test_install_ejs_raises_without_pin(runtime, fake_ejs_download):
    _extract(runtime, "2026.8.19", ejs=None)
    fake_ejs_download(AssertionError("通信してはいけない"))
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.install_ejs(runtime / "2026.8.19")


def test_install_ejs_rejects_archive_without_package(runtime, fake_ejs_download):
    _extract(runtime, "2026.8.19", ejs="0.8.0")
    fake_ejs_download(_ejs_wheel_bytes("0.8.0", package="something_else"))
    with pytest.raises(ytdlp_runtime.YtdlpUnavailable):
        ytdlp_runtime.install_ejs(runtime / "2026.8.19")
    assert not list(runtime.glob(".staging-ejs-*"))
