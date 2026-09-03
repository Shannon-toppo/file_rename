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
    pp_feed: list[dict] = []  # postprocessor_hooks へ流すイベント（変換段の通知用）
    last_opts: dict | None = None  # 直近に渡された yt-dlp オプション（検査用）
    last_download: bool | None = None  # extract_info の download 引数（検査用）

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.last_opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        FakeYDL.last_download = download
        for d in self.hook_feed:
            for hook in self.opts.get("progress_hooks", []):
                hook(d)
        for d in self.pp_feed:
            for hook in self.opts.get("postprocessor_hooks", []):
                hook(d)
        return self.info

    def prepare_filename(self, entry):
        return entry["_filename"]


@pytest.fixture
def fake_ydl(monkeypatch, tmp_path):
    monkeypatch.setattr(core, "YoutubeDL", FakeYDL)
    # 出力先も一時ディレクトリへ
    monkeypatch.setattr(core, "FILES_DIR", tmp_path)
    # ローカライズ済みタイトル / YouTube Music の曲名の取得は実 HTTP を叩く
    # ため必ず無効化する (使うテストは個別に上書きする)
    monkeypatch.setattr(core, "_fetch_localized_title", lambda *a, **k: None)
    monkeypatch.setattr(core, "_fetch_ytmusic_song", lambda *a, **k: (None, None))
    FakeYDL.info = None
    FakeYDL.hook_feed = []
    FakeYDL.pp_feed = []
    FakeYDL.last_opts = None
    FakeYDL.last_download = None
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


def test_download_tracks_reports_stages_after_download(fake_ydl, tmp_path):
    """受信後の段（ffmpeg 変換 → タイトル取得）が on_stage で通知される。

    進捗フックは受信中しか呼ばれないので、これが無いと変換（ノーマライズ・
    無音切り詰め込み）とタイトル取得の間ずっと「DL中 100%」に見えてしまう。
    """
    fake_ydl.info = entry_for(tmp_path, "a")
    fake_ydl.pp_feed = [
        {"status": "started", "postprocessor": "ExtractAudio"},
        {"status": "finished", "postprocessor": "ExtractAudio"},
    ]
    seen = []
    core.download_tracks("u", "mp3", on_stage=seen.append)
    # started で「変換中」、entries ループ手前で「情報取得中」。finished は無視
    assert seen == [core.Status.CONVERTING, core.Status.FETCHING]


def test_download_tracks_stage_hook_does_not_cancel(fake_ydl, tmp_path):
    """変換中のキャンセルは即座に打ち切らない（中途半端なファイルを残さない）。

    キャンセルは extract_info を抜けた直後の再確認で効く。
    """
    fake_ydl.info = entry_for(tmp_path, "a")
    fake_ydl.pp_feed = [{"status": "started", "postprocessor": "ExtractAudio"}]
    cancel = threading.Event()
    cancel.set()
    seen = []
    with pytest.raises(CancelledError):
        core.download_tracks("u", "mp3", on_stage=seen.append, cancel=cancel)
    assert seen == [core.Status.CONVERTING]  # フックは呼ばれてから中断する


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
# fetch_metadata（DL せずメタデータのみ取得）
# ---------------------------------------------------------------------------


def test_fetch_metadata_playlist_flat(fake_ydl):
    """再生リストはフラット抽出で 1 エントリ 1 Track（DL しない）。"""
    fake_ydl.info = {
        "entries": [
            {"title": "A", "url": "https://e/a", "channel": "Ch"},
            None,  # ignoreerrors で失敗した項目
            {"title": "B", "url": "https://e/b", "uploader": "Up"},
        ]
    }
    tracks = core.fetch_metadata("https://e/list")
    assert [t.stem for t in tracks] == ["A", "B"]
    assert [t.url for t in tracks] == ["https://e/a", "https://e/b"]
    assert tracks[0].channel == "Ch"
    assert tracks[1].channel == "Up"  # uploader フォールバック
    # DL していない QUEUED 行（そのまま実行すれば通常どおり DL される）
    assert all(t.filepath is None and t.status is Status.QUEUED for t in tracks)
    assert fake_ydl.last_download is False
    assert fake_ydl.last_opts["extract_flat"] == "in_playlist"
    assert fake_ydl.last_opts["extractor_args"] == {"youtube": {"lang": ["ja"]}}


def test_fetch_metadata_single_video(fake_ydl):
    """単一動画は完全な info（webpage_url あり）から 1 Track を返す。"""
    fake_ydl.info = {"title": "Song", "webpage_url": "https://e/x", "channel": "Ch"}
    tracks = core.fetch_metadata("https://e/x")
    assert len(tracks) == 1
    assert tracks[0].stem == "Song"
    assert tracks[0].url == "https://e/x"


def test_fetch_metadata_expand_playlist_option(fake_ydl):
    """expand_playlist は download_tracks と同じく noplaylist を反転する。"""
    fake_ydl.info = {"title": "S", "webpage_url": "u"}
    core.fetch_metadata("u")
    assert fake_ydl.last_opts["noplaylist"] is True
    core.fetch_metadata("u", expand_playlist=True)
    assert fake_ydl.last_opts["noplaylist"] is False


def test_fetch_metadata_cancel_and_empty(fake_ydl):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        core.fetch_metadata("u", cancel=cancel)

    fake_ydl.info = {"entries": [None]}
    with pytest.raises(CoreError):
        core.fetch_metadata("u")
    fake_ydl.info = None
    with pytest.raises(CoreError):
        core.fetch_metadata("u")


def test_fetch_metadata_logger_injection(fake_ydl):
    import logging

    fake_ydl.info = {"title": "S", "webpage_url": "u"}
    logger = logging.getLogger("test_yt_dlp")
    core.fetch_metadata("u", logger=logger)
    assert fake_ydl.last_opts["logger"] is logger
    assert fake_ydl.last_opts["quiet"] is True


# ---------------------------------------------------------------------------
# YouTube Music は推定せずメタデータの曲名を使う（ytmusic_direct）
# ---------------------------------------------------------------------------

YTM_URL = "https://music.youtube.com/watch?v=abc"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://music.youtube.com/watch?v=a", True),
        ("https://music.youtube.com/playlist?list=X", True),
        ("http://MUSIC.YouTube.com/watch?v=a", True),
        # スキームを省いて貼り付けられた URL（yt-dlp は受け付ける）
        ("music.youtube.com/playlist?list=X", True),
        ("www.youtube.com/watch?v=a", False),
        ("https://www.youtube.com/watch?v=a", False),
        ("https://youtube.com/watch?v=a", False),
        # ホスト名で判定するので、クエリに紛れ込んでいても誤検知しない
        ("https://example.com/?u=music.youtube.com", False),
        ("", False),
        (None, False),
    ],
)
def test_is_youtube_music(url, expected):
    assert core.is_youtube_music(url) is expected


def test_is_youtube_music_any_of_several_urls():
    """候補のどれか 1 つが YouTube Music なら True（再生リストの判定用）。"""
    assert core.is_youtube_music(None, "https://www.youtube.com/watch?v=a", YTM_URL)
    assert not core.is_youtube_music(None, "https://www.youtube.com/watch?v=a")
    assert not core.is_youtube_music()


def test_download_tracks_ytmusic_uses_track_metadata(fake_ydl, tmp_path):
    entry = entry_for(tmp_path, "Song (Official Video) [abc]", channel="Artist - Topic")
    entry["track"] = "Song"
    fake_ydl.info = entry
    (track,) = core.download_tracks(YTM_URL, "mp3")
    # 元タイトルは残したまま、曲名だけメタデータの track を採用する
    assert track.stem == "Song (Official Video) [abc]"
    assert track.guessed_title == "Song"
    assert track.skip_infer is True
    assert track.valid is True
    assert track.manual is False
    assert track.status is Status.PENDING


def test_download_tracks_ytmusic_falls_back_to_title(fake_ydl, tmp_path):
    fake_ydl.info = entry_for(tmp_path, "Song [abc]")  # track フィールド無し
    (track,) = core.download_tracks(YTM_URL, "mp3")
    assert track.guessed_title == "Song [abc]"
    assert track.skip_infer is True


def ytm_entry(tmp_path: Path, name: str, title: str, track: str | None = None) -> dict:
    """YouTube Music 再生リストのエントリ相当（webpage_url は www へ正規化済み）。"""
    entry = entry_for(tmp_path, name)
    entry["webpage_url"] = f"https://www.youtube.com/watch?v={name}"
    entry["title"] = title
    if track is not None:
        entry["track"] = track
    return entry


def test_download_tracks_ytmusic_playlist_marks_every_entry(fake_ydl, tmp_path):
    """再生リストの全エントリが直採用になる（曲名は track → title の順）。"""
    fake_ydl.info = {
        "entries": [
            ytm_entry(tmp_path, "a", "Song A (Official Video)", track="Song A"),
            ytm_entry(tmp_path, "b", "Song B"),  # track メタデータ無し
        ]
    }
    tracks = core.download_tracks("https://music.youtube.com/playlist?list=OLAK5uy_x", "mp3")
    assert [t.guessed_title for t in tracks] == ["Song A", "Song B"]
    assert all(t.skip_infer and t.status is Status.PENDING for t in tracks)


def test_download_tracks_ytmusic_detected_from_playlist_original_url(fake_ydl, tmp_path):
    """entry 側に music.youtube.com が残らなくても、抽出結果の元 URL で判定する。"""
    fake_ydl.info = {
        "original_url": "https://music.youtube.com/playlist?list=OLAK5uy_x",
        "webpage_url": "https://www.youtube.com/playlist?list=OLAK5uy_x",
        "entries": [ytm_entry(tmp_path, "a", "Song A", track="Song A")],
    }
    # 呼び出し元から渡る URL が正規化済みでも取りこぼさない
    (track,) = core.download_tracks("https://www.youtube.com/playlist?list=OLAK5uy_x", "mp3")
    assert track.skip_infer is True
    assert track.guessed_title == "Song A"


def test_download_tracks_ytmusic_title_has_no_video_id(fake_ydl, tmp_path):
    """曲名は entry のタイトル由来（ファイル名の " [id]" は混ぜない）。"""
    entry = ytm_entry(tmp_path, "Song [vid123]", "Song")
    fake_ydl.info = entry
    (track,) = core.download_tracks(YTM_URL, "mp3")
    assert track.stem == "Song [vid123]"  # 元タイトル列はファイル名どおり
    assert track.guessed_title == "Song"


def test_fetch_metadata_ytmusic_detected_from_original_url(fake_ydl):
    fake_ydl.info = {
        "original_url": "https://music.youtube.com/playlist?list=OLAK5uy_x",
        "entries": [{"title": "Song A", "url": "https://www.youtube.com/watch?v=a"}],
    }
    (track,) = core.fetch_metadata("https://www.youtube.com/playlist?list=OLAK5uy_x")
    assert track.skip_infer is True
    assert track.guessed_title == "Song A"


def test_download_tracks_ytmusic_uses_song_title_not_video_title(fake_ydl, tmp_path, monkeypatch):
    """曲名は YouTube Music に問い合わせる（動画タイトルとは別物）。

    実測: 06YWg6Y1kxo の YouTube 上のタイトルは "MIMI『 Pale 』feat. 初音ミク"
    だが、YouTube Music 上の曲名は "Pale"。yt-dlp のメタデータには出てこない。
    """
    entry = ytm_entry(tmp_path, "MIMI『 Pale 』feat. 初音ミク", "MIMI『 Pale 』feat. 初音ミク")
    entry["id"] = "06YWg6Y1kxo"
    fake_ydl.info = entry
    asked = []
    monkeypatch.setattr(
        core,
        "_fetch_ytmusic_song",
        lambda vid, **k: asked.append(vid) or ("Pale", "MIMI"),
    )
    (track,) = core.download_tracks(YTM_URL, "mp3")
    assert asked == ["06YWg6Y1kxo"]
    assert track.guessed_title == "Pale"
    assert track.artist == "MIMI"  # アーティスト欄も YouTube Music から埋める
    # 元タイトル（推定入力・表示用）は動画タイトルのまま
    assert track.stem == "MIMI『 Pale 』feat. 初音ミク"


def test_download_tracks_ytmusic_falls_back_when_lookup_fails(fake_ydl, tmp_path):
    """曲名を取れなければ track フィールド → 動画タイトルの順に落とす。"""
    entry = ytm_entry(tmp_path, "a", "Song A (Official Video)", track="Song A")
    entry["id"] = "a"
    fake_ydl.info = entry  # フィクスチャの _fetch_ytmusic_song は (None, None)
    (track,) = core.download_tracks(YTM_URL, "mp3")
    assert track.guessed_title == "Song A"


def test_fetch_metadata_ytmusic_uses_song_title(fake_ydl, monkeypatch):
    """情報取得の段でも曲名を採用する（DL 前に確認できるように）。"""
    fake_ydl.info = {
        "entries": [
            {"id": "06YWg6Y1kxo", "title": "MIMI『 Pale 』feat. 初音ミク",
             "url": "https://music.youtube.com/watch?v=06YWg6Y1kxo"},
        ]
    }
    monkeypatch.setattr(core, "_fetch_ytmusic_song", lambda vid, **k: ("Pale", "MIMI"))
    (track,) = core.fetch_metadata("https://music.youtube.com/playlist?list=X")
    assert track.stem == "MIMI『 Pale 』feat. 初音ミク"
    assert track.guessed_title == "Pale"
    assert track.artist == "MIMI"
    assert track.skip_infer is True


def _byline_run(text: str, page_type: str | None = None) -> dict:
    """longBylineText の run 1 つぶん（page_type 付きならリンク付きの run）。"""
    run: dict = {"text": text}
    if page_type is not None:
        run["navigationEndpoint"] = {
            "browseEndpoint": {
                "browseEndpointContextSupportedConfigs": {
                    "browseEndpointContextMusicConfig": {"pageType": page_type}
                }
            }
        }
    return run


ARTIST_PAGE = "MUSIC_PAGE_TYPE_ARTIST"


def _panel(video_id: str, title: str, byline_runs: list[dict] | None = None) -> dict:
    renderer = {"videoId": video_id, "title": {"runs": [{"text": title}]}}
    if byline_runs is not None:
        renderer["longBylineText"] = {"runs": byline_runs}
    return {"playlistPanelVideoRenderer": renderer}


def test_find_ytmusic_song_parsing():
    """再生キューの中から、当該 videoId の曲名とアーティスト名を取り出す。"""
    data = {
        "contents": {
            "results": [
                _panel("other", "別の曲", [_byline_run("別の人", ARTIST_PAGE)]),
                {"playlistPanelVideoRenderer": {
                    "videoId": "abc",
                    "title": {"runs": [{"text": "Pa"}, {"text": "le"}]},
                    "longBylineText": {"runs": [
                        _byline_run("MIMI", ARTIST_PAGE),
                        _byline_run(" • "),
                        # アルバム・再生回数の run はアーティストとして拾わない
                        _byline_run("Pale", "MUSIC_PAGE_TYPE_ALBUM"),
                        _byline_run(" • "),
                        _byline_run("393万回視聴"),
                    ]},
                }},
            ]
        }
    }
    assert core._find_ytmusic_song(data, "abc") == ("Pale", "MIMI")
    assert core._find_ytmusic_song(data, "missing") == (None, None)
    assert core._find_ytmusic_song({}, "abc") == (None, None)


def test_find_ytmusic_song_multiple_artists():
    data = {"contents": [_panel("abc", "曲", [
        _byline_run("A", ARTIST_PAGE), _byline_run(" & "), _byline_run("B", ARTIST_PAGE),
    ])]}
    assert core._find_ytmusic_song(data, "abc") == ("曲", "A, B")


def test_find_ytmusic_song_without_artist_link():
    """アーティストの run が無ければアーティストは None（曲名だけ使う）。"""
    data = {"contents": [_panel("abc", "曲", [_byline_run("393万回視聴")])]}
    assert core._find_ytmusic_song(data, "abc") == ("曲", None)
    assert core._find_ytmusic_song({"contents": [_panel("abc", "曲")]}, "abc") == ("曲", None)


def test_use_metadata_title_keeps_existing_artist():
    """手動入力・チャンネル名コピー済みのアーティストは上書きしない。"""
    track = Track(stem="s", artist="手動アーティスト")
    core.use_metadata_title(track, "曲", artist="YTM アーティスト")
    assert track.artist == "手動アーティスト"


def test_fetch_ytmusic_song_network_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(core.urllib.request, "urlopen", boom)
    assert core._fetch_ytmusic_song("06YWg6Y1kxo") == (None, None)


def test_download_tracks_ytmusic_direct_disabled(fake_ydl, tmp_path):
    entry = entry_for(tmp_path, "Song [abc]")
    entry["track"] = "Song"
    fake_ydl.info = entry
    (track,) = core.download_tracks(YTM_URL, "mp3", ytmusic_direct=False)
    assert track.skip_infer is False
    assert track.guessed_title == ""
    assert track.status is Status.QUEUED


def test_download_tracks_non_ytmusic_untouched(fake_ydl, tmp_path):
    entry = entry_for(tmp_path, "Song [abc]")
    entry["track"] = "Song"
    fake_ydl.info = entry
    (track,) = core.download_tracks("https://www.youtube.com/watch?v=abc", "mp3")
    assert track.skip_infer is False
    assert track.guessed_title == ""


def test_fetch_metadata_ytmusic_marks_rows(fake_ydl):
    fake_ydl.info = {
        "entries": [
            {"title": "Song A", "url": "https://www.youtube.com/watch?v=a"},
            {"title": "Song B", "url": "https://www.youtube.com/watch?v=b"},
        ]
    }
    tracks = core.fetch_metadata("https://music.youtube.com/playlist?list=X")
    assert [t.guessed_title for t in tracks] == ["Song A", "Song B"]
    assert all(t.skip_infer and t.status is Status.PENDING for t in tracks)


def test_infer_titles_protects_skip_infer_rows(monkeypatch):
    fake, captured = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)
    direct = Track(stem="b", guessed_title="B", skip_infer=True, status=Status.PENDING)
    auto = Track(stem="a")
    core.infer_titles([direct, auto], client=object())

    assert [i.title for i in captured["inputs"]] == ["a"]
    assert direct.guessed_title == "B"  # 上書きされない
    assert auto.guessed_title == "song0"


def test_infer_titles_force_overrides_skip_infer(monkeypatch):
    fake, _ = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)
    direct = Track(stem="b", guessed_title="B", skip_infer=True, status=Status.PENDING)
    core.infer_titles([direct], client=object(), force=True)

    assert direct.guessed_title == "song0"
    # 明示的に推定し直した行は、以降も推定対象へ戻す
    assert direct.skip_infer is False


def test_write_tags_writes_skip_infer_row(tmp_path):
    mp3 = tmp_path / "s.mp3"
    mp3.write_bytes(b"\x00")
    track = Track(stem="s", filepath=mp3)
    core.use_metadata_title(track)
    core.write_tags([track])
    assert track.status is Status.DONE
    assert track.guessed_title == "s"


# ---------------------------------------------------------------------------
# read_url_list
# ---------------------------------------------------------------------------


def test_read_url_list(tmp_path):
    f = tmp_path / "urls.txt"
    f.write_text("http://a\n\n# コメント\n  http://b  \n", encoding="utf-8")
    assert core.read_url_list(f) == ["http://a", "http://b"]


# ---------------------------------------------------------------------------
# check_connection（urllib をフェイクに差し替え）
# ---------------------------------------------------------------------------

import types  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


def _fake_config(monkeypatch, base_url="http://127.0.0.1:1234/v1/", model="m1"):
    cfg = types.SimpleNamespace(base_url=base_url, api_key=None, model=model)
    monkeypatch.setattr(core, "Config", types.SimpleNamespace(from_env=lambda: cfg))
    return cfg


def _fake_urlopen(monkeypatch, body: bytes, status: int = 200) -> dict:
    """urlopen を status/body 固定のフェイクへ差し替え、リクエスト内容を記録する。"""
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return body

    FakeResp.status = status

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_check_connection_success(monkeypatch):
    _fake_config(monkeypatch)
    seen = _fake_urlopen(monkeypatch, b'{"object": "list", "data": []}')
    ok, msg = core.check_connection(timeout=1.5)
    assert ok
    # 末尾スラッシュに頑健（//models にならない）で、短い timeout が使われる
    assert seen["url"] == "http://127.0.0.1:1234/v1/models"
    assert seen["timeout"] == 1.5


def test_check_connection_model_in_list_no_warning(monkeypatch):
    """使用モデルが /models の一覧にあれば注意なしの OK。"""
    _fake_config(monkeypatch, model="m1")
    _fake_urlopen(monkeypatch, b'{"data": [{"id": "m1"}, {"id": "m2"}]}')
    ok, msg = core.check_connection()
    assert ok
    assert "注意" not in msg


@pytest.mark.parametrize(
    "model",
    [
        "gemma-4-e2b",  # publisher 省略（LM Studio の設定画面はこの表記）
        "Gemma-4-E2B",  # 大文字小文字の揺れ
        "google/gemma-4-e2b@q4_k_m",  # 量子化サフィックス付き
        "google/gemma-4-e2b",  # 完全一致
    ],
)
def test_check_connection_model_alias_no_warning(monkeypatch, model):
    """publisher 省略などの表記ゆれを一覧と同一視する（誤警告の防止）。"""
    _fake_config(monkeypatch, model=model)
    _fake_urlopen(monkeypatch, b'{"data": [{"id": "google/gemma-4-e2b"}]}')
    ok, msg = core.check_connection()
    assert ok
    assert "注意" not in msg


def test_check_connection_warns_on_unknown_model(monkeypatch):
    """使用モデルが一覧に無ければ OK のままモデル名入りの注意を添える。

    MODEL 未設定のままライブラリ既定値で推論だけ失敗する事故に気付ける
    ように（LM Studio はエイリアス解決で通ることがあるため NG にはしない）。
    """
    _fake_config(monkeypatch, model="gemma-4-e2b-it")
    _fake_urlopen(monkeypatch, b'{"data": [{"id": "google/gemma-4-e2b"}]}')
    ok, msg = core.check_connection()
    assert ok
    assert "gemma-4-e2b-it" in msg and "ありません" in msg


def test_check_connection_error_json_with_200(monkeypatch):
    # LM Studio は存在しないパス（/v1 抜けなど）にも HTTP 200 でエラー JSON を
    # 返すため、ステータスだけ見ると偽陽性になる。ボディ検証で NG にする。
    _fake_config(monkeypatch, base_url="http://127.0.0.1:1234")
    _fake_urlopen(monkeypatch, b'{"error":"Unexpected endpoint or method. (GET /models)"}')
    ok, msg = core.check_connection()
    assert not ok
    assert "/models" in msg


def test_check_connection_non_json_with_200(monkeypatch):
    # LLM 以外のサーバ（管理画面など）が HTML を 200 で返すケースも NG にする
    _fake_config(monkeypatch)
    _fake_urlopen(monkeypatch, b"<html>hello</html>")
    ok, msg = core.check_connection()
    assert not ok


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
