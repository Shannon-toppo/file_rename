# -*- coding: utf-8 -*-
"""core.py のオフラインテスト（LLM・yt-dlp は使わない）。"""
import threading
from pathlib import Path

import pytest
from mutagen.id3 import ID3
from mutagen.id3._util import ID3NoHeaderError
from mv2title import TitleResult

import core
from core import CancelledError, CoreError, Status, Track


# ---------------------------------------------------------------------------
# ヘルパ
# ---------------------------------------------------------------------------


def make_mp3(tmp_path: Path, name: str) -> Path:
    """ID3 ヘッダ無しのダミー mp3 を作る。"""
    p = tmp_path / name
    p.write_bytes(b"\x00" * 128)
    return p


def read_tit2(path: Path) -> str | None:
    try:
        frame = ID3(str(path)).get("TIT2")
    except ID3NoHeaderError:
        return None
    return str(frame) if frame else None


def fake_extract_factory(results_fn):
    """extract_titles を差し替えるフェイク。入力を捕捉する。"""
    captured = {}

    def fake(inputs, client, **kw):
        captured["inputs"] = list(inputs)
        captured["kw"] = kw
        return results_fn(inputs)

    return fake, captured


def ok_results(inputs):
    return [
        TitleResult(index=i + 1, original=t.title, title=f"song{i}", valid=True)
        for i, t in enumerate(inputs)
    ]


# ---------------------------------------------------------------------------
# infer_titles
# ---------------------------------------------------------------------------


def test_infer_titles_updates_tracks(monkeypatch):
    fake, captured = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)
    tracks = [
        Track(stem="Artist - A [MV]", channel="ArtistCh"),
        Track(stem="B (Official Video)"),
    ]
    core.infer_titles(tracks, client=object())

    assert captured["inputs"][0].title == "Artist - A [MV]"
    assert captured["inputs"][0].channel == "ArtistCh"
    assert captured["inputs"][1].channel is None
    assert captured["kw"]["bypass_check"] is True
    assert [t.guessed_title for t in tracks] == ["song0", "song1"]
    assert all(t.valid for t in tracks)
    assert all(t.status is Status.PENDING for t in tracks)


def test_infer_titles_protects_manual_rows(monkeypatch):
    fake, captured = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)
    manual = Track(stem="manual", guessed_title="ユーザー入力", manual=True)
    auto = Track(stem="auto")
    core.infer_titles([manual, auto], client=object())

    assert manual.guessed_title == "ユーザー入力"  # 上書きされない
    assert len(captured["inputs"]) == 1
    assert auto.guessed_title == "song0"


def test_infer_titles_force_overrides_manual(monkeypatch):
    fake, _ = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)
    manual = Track(stem="manual", guessed_title="ユーザー入力", manual=True)
    core.infer_titles([manual], client=object(), force=True)

    assert manual.guessed_title == "song0"
    assert manual.manual is False  # 再推定後は自動扱いに戻る


def test_infer_titles_length_mismatch_raises(monkeypatch):
    fake, _ = fake_extract_factory(lambda inputs: ok_results(inputs)[:1])
    monkeypatch.setattr(core, "extract_titles", fake)
    tracks = [Track(stem="a"), Track(stem="b")]
    with pytest.raises(CoreError):
        core.infer_titles(tracks, client=object())
    assert all(t.status is Status.ERROR for t in tracks)
    assert all(t.error for t in tracks)


def test_infer_titles_llm_error_marks_all(monkeypatch):
    def boom(inputs, client, **kw):
        raise ConnectionError("endpoint down")

    monkeypatch.setattr(core, "extract_titles", boom)
    tracks = [Track(stem="a")]
    with pytest.raises(ConnectionError):
        core.infer_titles(tracks, client=object())
    assert tracks[0].status is Status.ERROR
    assert "endpoint down" in tracks[0].error


def test_infer_titles_no_targets_is_noop(monkeypatch):
    monkeypatch.setattr(core, "extract_titles", None)  # 呼ばれたら TypeError
    core.infer_titles([Track(stem="m", manual=True)], client=object())


# ---------------------------------------------------------------------------
# write_tags / write_title / describe_result
# ---------------------------------------------------------------------------


def test_write_tags_policies(tmp_path):
    ok = Track(stem="ok", filepath=make_mp3(tmp_path, "ok.mp3"), guessed_title="song", valid=True)
    empty = Track(stem="e", filepath=make_mp3(tmp_path, "e.mp3"), guessed_title="", valid=True)
    invalid = Track(
        stem="i", filepath=make_mp3(tmp_path, "i.mp3"), guessed_title="bad", valid=False
    )
    manual_invalid = Track(
        stem="m",
        filepath=make_mp3(tmp_path, "m.mp3"),
        guessed_title="手動確定",
        valid=False,
        manual=True,
    )
    nopath = Track(stem="n", guessed_title="x", valid=True)

    seen = []
    core.write_tags([ok, empty, invalid, manual_invalid, nopath], on_result=seen.append)

    assert ok.status is Status.DONE and read_tit2(ok.filepath) == "song"
    assert empty.status is Status.PENDING and read_tit2(empty.filepath) is None
    assert invalid.status is Status.PENDING and read_tit2(invalid.filepath) is None
    # 手動編集済みなら valid=False でも書き込む（ユーザーの意思を優先）
    assert manual_invalid.status is Status.DONE and read_tit2(manual_invalid.filepath) == "手動確定"
    assert nopath.status is Status.ERROR
    assert len(seen) == 5


def test_write_title_with_artist_mp3(tmp_path):
    """アーティスト指定時は TPE1 も書き込む（未指定なら書かない）。"""
    p = make_mp3(tmp_path, "a.mp3")
    core.write_title(p, "song", artist="ArtistName")
    tags = ID3(str(p))
    assert str(tags.get("TIT2")) == "song"
    assert str(tags.get("TPE1")) == "ArtistName"

    p2 = make_mp3(tmp_path, "b.mp3")
    core.write_title(p2, "song")
    assert ID3(str(p2)).get("TPE1") is None


def test_write_tags_writes_artist(tmp_path):
    t = Track(
        stem="s",
        filepath=make_mp3(tmp_path, "s.mp3"),
        guessed_title="song",
        artist="Ch",
        valid=True,
    )
    core.write_tags([t])
    assert t.status is Status.DONE
    assert str(ID3(str(t.filepath)).get("TPE1")) == "Ch"


def test_write_tags_failure_does_not_stop_others(tmp_path):
    bad = Track(
        stem="bad", filepath=tmp_path / "bad.flac", guessed_title="x", valid=True
    )  # 未対応拡張子 → write_title が ValueError
    ok = Track(stem="ok", filepath=make_mp3(tmp_path, "ok.mp3"), guessed_title="y", valid=True)
    core.write_tags([bad, ok])
    assert bad.status is Status.ERROR
    assert ok.status is Status.DONE


def test_describe_result_formats(tmp_path):
    done = Track(stem="d", filepath=tmp_path / "d.mp3", guessed_title="song", status=Status.DONE)
    err = Track(stem="e", status=Status.ERROR, error="boom")
    skip = Track(stem="s", status=Status.PENDING, error="曲名が空のためスキップしました。")
    assert core.describe_result(done) == "  [OK] d.mp3  ->  song"
    assert core.describe_result(err) == "  [ERR] e  ->  boom"
    assert "[SKIP]" in core.describe_result(skip)


# ---------------------------------------------------------------------------
# download_tracks（yt-dlp をフェイクに差し替え）
# ---------------------------------------------------------------------------


class FakeYDL:
    """core.YoutubeDL の代役。info / files はクラス変数で注入する。"""

    info: dict | None = None
    hook_feed: list[dict] = []
    last_opts: dict | None = None  # 直近に渡された yt-dlp オプション（検査用）

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        for d in self.hook_feed:
            for hook in self.opts.get("progress_hooks", []):
                hook(d)
        return self.info

    def prepare_filename(self, entry):
        return entry["_filename"]


@pytest.fixture
def fake_ydl(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "YoutubeDL", FakeYDL)
    # 出力先も一時ディレクトリへ
    monkeypatch.setattr(core, "FILES_DIR", tmp_path)
    # ローカライズ済みタイトルの取得は実 HTTP を叩くため必ず無効化する
    # (使うテストは個別に上書きする)
    monkeypatch.setattr(core, "_fetch_localized_title", lambda *a, **k: None)
    FakeYDL.info = None
    FakeYDL.hook_feed = []
    FakeYDL.last_opts = None
    return FakeYDL


def entry_for(tmp_path: Path, name: str, channel=None, uploader=None) -> dict:
    mp3 = tmp_path / f"{name}.mp3"
    mp3.write_bytes(b"\x00")
    return {
        "_filename": str(tmp_path / f"{name}.webm"),
        "webpage_url": f"https://example.com/{name}",
        "channel": channel,
        "uploader": uploader,
    }


def test_download_tracks_single(fake_ydl, tmp_path):
    fake_ydl.info = entry_for(tmp_path, "Artist - Song [abc]", channel="ArtistCh")
    tracks = core.download_tracks("https://example.com/x", "mp3")
    assert len(tracks) == 1
    t = tracks[0]
    assert t.stem == "Artist - Song [abc]"
    assert t.channel == "ArtistCh"
    assert t.filepath is not None and t.filepath.exists()


def test_download_tracks_playlist_and_uploader_fallback(fake_ydl, tmp_path):
    fake_ydl.info = {
        "entries": [
            entry_for(tmp_path, "a", uploader="UploaderName"),
            None,  # ignoreerrors で失敗した項目
            entry_for(tmp_path, "b", channel="Ch"),
        ]
    }
    tracks = core.download_tracks("https://example.com/list", "mp3")
    assert [t.stem for t in tracks] == ["a", "b"]
    assert tracks[0].channel == "UploaderName"
    assert tracks[1].channel == "Ch"


def test_download_tracks_progress_and_cancel(fake_ydl, tmp_path):
    fake_ydl.info = entry_for(tmp_path, "a")
    fake_ydl.hook_feed = [
        {"status": "downloading", "filename": "a.webm", "downloaded_bytes": 50, "total_bytes": 100}
    ]
    seen = []
    core.download_tracks(
        "u", "mp3", on_progress=lambda n, p, i=None, t=None: seen.append((n, p, i, t))
    )
    assert seen == [("a.webm", 50.0, None, None)]

    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        core.download_tracks("u", "mp3", cancel=cancel)


def test_download_tracks_progress_playlist_index(fake_ydl, tmp_path):
    """再生リスト中は info_dict の playlist_index / n_entries を進捗に添える。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    fake_ydl.hook_feed = [
        {
            "status": "downloading",
            "filename": "a.webm",
            "downloaded_bytes": 30,
            "total_bytes": 100,
            "info_dict": {"playlist_index": 2, "n_entries": 5},
        }
    ]
    seen = []
    core.download_tracks(
        "u", "mp3", on_progress=lambda n, p, i=None, t=None: seen.append((p, i, t))
    )
    assert seen == [(30.0, 2, 5)]


def test_download_tracks_empty_raises(fake_ydl, tmp_path):
    fake_ydl.info = {"entries": [None]}
    with pytest.raises(CoreError):
        core.download_tracks("u", "mp3")


def test_download_tracks_bad_format():
    with pytest.raises(ValueError):
        core.download_tracks("u", "flac")


def test_download_tracks_uses_localized_title_as_stem(fake_ydl, tmp_path, monkeypatch):
    """watch 画面の日本語タイトルが取れたら推定入力(stem)に使う。

    player API 由来の yt-dlp タイトル(= ファイル名)は翻訳されないため、
    翻訳付き動画では next API の日本語タイトルを優先する回帰テスト。
    """
    entry = entry_for(tmp_path, "natori - Propose [VDdLF1YubI0]")
    entry["id"] = "VDdLF1YubI0"
    fake_ydl.info = entry
    monkeypatch.setattr(
        core, "_fetch_localized_title", lambda vid, **k: "なとり - プロポーズ"
    )
    tracks = core.download_tracks("u", "mp3")
    assert tracks[0].stem == "なとり - プロポーズ"
    # ファイル自体は yt-dlp のタイトルのまま(タグだけ日本語になる)
    assert tracks[0].filepath.stem == "natori - Propose [VDdLF1YubI0]"

    # 取得失敗(None)ならファイル名 stem へフォールバック
    monkeypatch.setattr(core, "_fetch_localized_title", lambda vid, **k: None)
    tracks = core.download_tracks("u", "mp3")
    assert tracks[0].stem == "natori - Propose [VDdLF1YubI0]"


def test_find_primary_title_parsing():
    """next API 応答の構造探索(runs / simpleText / 見つからない)。"""
    runs = {
        "contents": [
            {"videoPrimaryInfoRenderer": {"title": {"runs": [{"text": "なとり - "}, {"text": "プロポーズ"}]}}}
        ]
    }
    assert core._find_primary_title(runs) == "なとり - プロポーズ"
    simple = {"a": [{"videoPrimaryInfoRenderer": {"title": {"simpleText": "曲名"}}}]}
    assert core._find_primary_title(simple) == "曲名"
    assert core._find_primary_title({"contents": []}) is None


def test_fetch_localized_title_network_failure_returns_none(monkeypatch):
    def boom(req, timeout=0):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert core._fetch_localized_title("VDdLF1YubI0") is None


def test_download_tracks_prefers_japanese_metadata(fake_ydl, tmp_path):
    """翻訳メタデータの優先言語として ja を yt-dlp へ渡す。

    YouTube は既定で英語版タイトル/チャンネル名を返すため、日本語版が
    あればそれを取得する（無ければ原語のまま）ようにする回帰テスト。
    """
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")
    assert fake_ydl.last_opts["extractor_args"] == {"youtube": {"lang": ["ja"]}}


def test_download_tracks_expand_playlist_option(fake_ydl, tmp_path):
    """expand_playlist=True で混在 URL もリスト展開（noplaylist=False）になる。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")
    assert fake_ydl.last_opts["noplaylist"] is True  # 既定は動画 1 本のみ
    core.download_tracks("u", "mp3", expand_playlist=True)
    assert fake_ydl.last_opts["noplaylist"] is False


def test_download_tracks_normalize_option(fake_ydl, tmp_path):
    """normalize=True（既定）で loudnorm フィルタが postprocessor_args に入る。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")  # 既定 ON
    assert fake_ydl.last_opts["postprocessor_args"] == ["-af", core.loudnorm_filter()]
    core.download_tracks("u", "mp3", normalize=False)
    assert "postprocessor_args" not in fake_ydl.last_opts  # OFF なら付けない


def test_download_tracks_loudness_option(fake_ydl, tmp_path):
    """loudness で loudnorm の基準値 (I) を変えられる（TP / LRA は固定）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", loudness=-9.5)
    assert fake_ydl.last_opts["postprocessor_args"] == [
        "-af",
        "loudnorm=I=-9.5:TP=-1.5:LRA=11",
    ]


def test_download_tracks_trim_silence_option(fake_ydl, tmp_path):
    """trim_silence=True で末尾無音削除フィルタが loudnorm の前段に入る（既定 OFF）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", trim_silence=True)
    assert fake_ydl.last_opts["postprocessor_args"] == [
        "-af",
        core.TRIM_SILENCE_FILTER + "," + core.loudnorm_filter(),
    ]
    # ノーマライズ OFF でも無音削除は単独で使える
    core.download_tracks("u", "mp3", normalize=False, trim_silence=True)
    assert fake_ydl.last_opts["postprocessor_args"] == ["-af", core.TRIM_SILENCE_FILTER]


def test_download_tracks_out_dir(fake_ydl, tmp_path):
    """out_dir 指定時は FILES_DIR ではなくそこへ保存する（フォルダも作成）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    dest = tmp_path / "sub" / "dir"
    core.download_tracks("u", "mp3", out_dir=dest)
    assert dest.is_dir()  # outtmpl の組み立てと mkdir が out_dir 基準で行われる


def test_download_tracks_logger_injection(fake_ydl, tmp_path):
    """logger 指定時は opts に logger と quiet=True が入る（logging 経由へ切替）。"""
    import logging

    fake_ydl.info = entry_for(tmp_path, "a")
    logger = logging.getLogger("test_yt_dlp")
    core.download_tracks("u", "mp3", logger=logger)
    assert fake_ydl.last_opts["logger"] is logger
    assert fake_ydl.last_opts["quiet"] is True


def test_download_tracks_no_logger_by_default(fake_ydl, tmp_path):
    """logger 未指定なら opts に logger/quiet は入らない（CLI はコンソール出力）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")
    assert "logger" not in fake_ydl.last_opts
    assert "quiet" not in fake_ydl.last_opts


# ---------------------------------------------------------------------------
# check_connection（urllib をフェイクに差し替え）
# ---------------------------------------------------------------------------

import types  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


def _fake_config(monkeypatch, base_url="http://127.0.0.1:1234/v1/"):
    cfg = types.SimpleNamespace(base_url=base_url, api_key=None)
    monkeypatch.setattr(core, "Config", types.SimpleNamespace(from_env=lambda: cfg))
    return cfg


def test_check_connection_success(monkeypatch):
    _fake_config(monkeypatch)
    seen = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, msg = core.check_connection(timeout=1.5)
    assert ok
    # 末尾スラッシュに頑健（//models にならない）で、短い timeout が使われる
    assert seen["url"] == "http://127.0.0.1:1234/v1/models"
    assert seen["timeout"] == 1.5


def test_check_connection_refused(monkeypatch):
    _fake_config(monkeypatch)

    def boom(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    ok, msg = core.check_connection()
    assert not ok
    assert "接続できません" in msg


def test_check_connection_no_baseurl(monkeypatch):
    def raise_ve():
        raise ValueError("BASE_URL が未設定です。")

    monkeypatch.setattr(core, "Config", types.SimpleNamespace(from_env=raise_ve))
    ok, msg = core.check_connection()
    assert not ok
    assert "BASE_URL" in msg
