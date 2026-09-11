# -*- coding: utf-8 -*-
"""core.py のオフラインテスト（LLM・yt-dlp は使わない）。"""
import threading
from pathlib import Path

import mv2title
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


# 実物の Ogg Opus（無音 0.2 秒 / 242 バイト）。mutagen は正しい Ogg ストリームで
# ないとタグを書けないため、ダミーではなく本物を置いている
OPUS_FIXTURE = Path(__file__).parent / "data" / "silence.opus"


def make_opus(tmp_path: Path, name: str = "a.opus") -> Path:
    """テスト用に Ogg Opus のコピーを作る（元のフィクスチャは汚さない）。"""
    p = tmp_path / name
    p.write_bytes(OPUS_FIXTURE.read_bytes())
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


def test_infer_titles_marks_still_empty_rows(monkeypatch):
    """再問い合わせでも空なら、空欄のままにせず理由を error に残す。"""

    def fake(inputs, client, **kw):
        return [
            TitleResult(index=i + 1, original=t.title, title="", valid=False)
            for i, t in enumerate(inputs)
        ]

    monkeypatch.setattr(core, "extract_titles", fake)
    t = Track(stem="a")
    core.infer_titles([t], client=object())

    # 手入力・再推定・作者/アルバムのみの書き込みを従来どおり効かせるため
    # 状態は PENDING のまま（ERROR にはしない）
    assert t.status is Status.PENDING
    assert t.error == core.EMPTY_TITLE_ERROR


def test_infer_titles_passes_use_schema(monkeypatch):
    """構造化出力の有無は extract_titles(use_schema=...) へそのまま渡る（既定 ON）。"""
    fake, captured = fake_extract_factory(ok_results)
    monkeypatch.setattr(core, "extract_titles", fake)

    core.infer_titles([Track(stem="a")], client=object())
    assert captured["kw"]["use_schema"] is core.USE_SCHEMA is True

    core.infer_titles([Track(stem="a")], client=object(), use_schema=False)
    assert captured["kw"]["use_schema"] is False


def test_infer_titles_calls_extract_titles_once(monkeypatch):
    """core は extract_titles を 1 回しか呼ばない（往復を二重化しない）。

    空で返った項目の拾い直しは mv2title 0.4.0 側の責務になった。core にも
    同じ処理があったが、失敗時に mv2title と同条件のリクエストを送り直す
    無駄な 1 往復になるため無効化してある（core._retry_missing_titles 参照）。
    """
    calls = []

    def fake(inputs, client, **kw):
        calls.append(kw)
        # 2 件目が空で返っても、core からの再問い合わせは起こさない
        return [
            TitleResult(
                index=i + 1, original=t.title, title="song0" if i == 0 else "", valid=i == 0
            )
            for i, t in enumerate(inputs)
        ]

    monkeypatch.setattr(core, "extract_titles", fake)
    core.infer_titles([Track(stem="a"), Track(stem="b")], client=object())
    assert len(calls) == 1


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


def test_write_title_with_album_mp3(tmp_path):
    """アルバム名指定時は TALB も書き込む（未指定なら書かない）。"""
    p = make_mp3(tmp_path, "a.mp3")
    core.write_title(p, "song", artist="A", album="Album1")
    tags = ID3(str(p))
    assert str(tags.get("TALB")) == "Album1"

    p2 = make_mp3(tmp_path, "b.mp3")
    core.write_title(p2, "song")
    assert ID3(str(p2)).get("TALB") is None


def test_write_tags_writes_album(tmp_path):
    t = Track(
        stem="s",
        filepath=make_mp3(tmp_path, "s.mp3"),
        guessed_title="song",
        artist="Ch",
        album="Alb",
        valid=True,
    )
    core.write_tags([t])
    assert t.status is Status.DONE
    assert str(ID3(str(t.filepath)).get("TALB")) == "Alb"


def test_write_tags_partial_when_title_missing(tmp_path):
    """曲名が空でも、作者・アルバム名の編集は書き込まれる（行は確認待ちのまま）。"""
    t = Track(stem="s", filepath=make_mp3(tmp_path, "s.mp3"), album="Alb", artist="A")
    core.write_tags([t])
    assert t.status is Status.PENDING  # 曲名は未確定なので完了にしない
    assert "作者・アルバム名のみ" in t.error
    tags = ID3(str(t.filepath))
    assert str(tags.get("TALB")) == "Alb" and str(tags.get("TPE1")) == "A"
    assert tags.get("TIT2") is None  # 空の曲名は書かない


def test_write_tags_skips_when_nothing_to_write(tmp_path):
    """曲名も作者もアルバムも空なら、従来どおり何も書かずスキップする。"""
    t = Track(stem="s", filepath=make_mp3(tmp_path, "s.mp3"))
    core.write_tags([t])
    assert t.status is Status.PENDING and "スキップ" in t.error
    assert read_tit2(t.filepath) is None


# ---------------------------------------------------------------------------
# タグ読み込み（保存先から取り込み）
# ---------------------------------------------------------------------------


def test_read_tags_roundtrip_mp3(tmp_path):
    """書き込んだ曲名 / 作者 / アルバム名がそのまま読み戻せる。"""
    p = make_mp3(tmp_path, "a.mp3")
    core.write_title(p, "曲名", artist="作者", album="アルバム")
    assert core.read_tags(p) == {"title": "曲名", "artist": "作者", "album": "アルバム"}


def test_read_tags_missing_or_broken(tmp_path):
    """タグ無し・未対応拡張子・存在しないファイルでも例外を投げず空を返す。"""
    empty = {"title": "", "artist": "", "album": ""}
    assert core.read_tags(make_mp3(tmp_path, "notag.mp3")) == empty  # ID3 ヘッダ無し
    assert core.read_tags(tmp_path / "none.mp3") == empty  # 存在しない
    assert core.read_tags(tmp_path / "x.flac") == empty  # 未対応拡張子
    broken = tmp_path / "broken.m4a"
    broken.write_bytes(b"not an mp4")
    assert core.read_tags(broken) == empty


def test_track_from_file_loads_metadata(tmp_path):
    """既存タグが行の初期値になり、曲名があれば推定から保護される。"""
    p = make_mp3(tmp_path, "a.mp3")
    core.write_title(p, "曲名", artist="作者", album="アルバム")

    t = core.track_from_file(p)
    assert (t.guessed_title, t.artist, t.album) == ("曲名", "作者", "アルバム")
    assert t.skip_infer and t.valid is True and t.status is Status.PENDING
    # skip_infer 行は再推定の対象外（force=True でのみ上書きされる）
    core.infer_titles([t], client=object(), batch_size=5)
    assert t.guessed_title == "曲名"


def test_track_from_file_without_tags(tmp_path):
    """タグが無いファイルは従来どおり空・QUEUED（推定対象）のまま。"""
    t = core.track_from_file(make_mp3(tmp_path, "a.mp3"))
    assert t.guessed_title == "" and t.artist == "" and t.album == ""
    assert not t.skip_infer and t.status is Status.QUEUED


def test_track_from_file_can_skip_metadata(tmp_path):
    p = make_mp3(tmp_path, "a.mp3")
    core.write_title(p, "曲名", album="アルバム")
    t = core.track_from_file(p, read_metadata=False)
    assert t.guessed_title == "" and t.album == "" and t.status is Status.QUEUED


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
    error_feed: list[str] = []  # report_error へ流す文言（ignoreerrors 時の失敗）
    last_opts: dict | None = None  # 直近に渡された yt-dlp オプション（検査用）
    last_download: bool | None = None  # extract_info の download 引数（検査用）
    last_pps: list | None = None  # add_post_processor で足された PP（検査用）

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.last_opts = opts
        FakeYDL.last_pps = []

    def add_post_processor(self, pp, when="post_process"):
        # 本物は set_downloader も呼ぶが、ここでは登録されたことだけ見る
        assert FakeYDL.last_pps is not None
        FakeYDL.last_pps.append((pp, when))

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
        # ignoreerrors=True の本物と同じく、失敗は例外ではなく report_error で
        # 報告して info を返すだけにする
        for msg in self.error_feed:
            self.report_error(msg)
        return self.info

    def report_error(self, message, *args, **kwargs):
        # 本物は stderr / logger へ出す。ここでは core 側のフックだけが要る
        pass

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
    FakeYDL.error_feed = []
    FakeYDL.last_opts = None
    FakeYDL.last_download = None
    FakeYDL.last_pps = None
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


def test_download_tracks_installs_cancel_match_filter(fake_ydl, tmp_path):
    """受信開始前にも止まるよう match_filter でキャンセルを見る。

    進捗フックはバイトを受け取り始めてからしか鳴らないため、これが無いと
    ライブ配信や HLS のエントリで停止ボタンが何十秒も効かない。
    """
    fake_ydl.info = entry_for(tmp_path, "a")
    cancel = threading.Event()
    core.download_tracks("u", "mp3", cancel=cancel)
    match_filter = fake_ydl.last_opts["match_filter"]
    assert match_filter({}, incomplete=True) is None  # 未キャンセルなら通す
    cancel.set()
    with pytest.raises(CancelledError):
        match_filter({}, incomplete=True)


def test_download_tracks_no_match_filter_without_cancel(fake_ydl, tmp_path):
    """cancel を渡さない CLI 経路でも match_filter 自体は無害に通る。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")
    assert fake_ydl.last_opts["match_filter"]({}, incomplete=True) is None


def test_cancel_exception_is_recognized_by_ytdlp(monkeypatch):
    """キャンセル例外は yt-dlp の DownloadCancelled でもあること。

    素の CancelledError だと ignoreerrors=True に握り潰され、再生リストの
    残りが処理され続ける（= 停止ボタンが効かない）。DownloadCancelled だけは
    _handle_extraction_exceptions が再送出するので、その場で全体が止まる。
    """
    from yt_dlp.utils import DownloadCancelled

    monkeypatch.setattr(core, "_YDL_CANCELLED", None)
    core._load_cancel_exception()
    exc = core._cancelled("停止")
    assert isinstance(exc, CancelledError)
    assert isinstance(exc, DownloadCancelled)
    assert str(exc) == "停止"


def test_cancel_exception_falls_back_without_ytdlp(monkeypatch):
    """yt-dlp 未ロード（テストの代役など）では素の CancelledError に落とす。"""
    monkeypatch.setattr(core, "_YDL_CANCELLED", None)
    exc = core._cancelled("停止")
    assert type(exc) is CancelledError


def test_download_tracks_cancel_stops_title_lookups(fake_ydl, tmp_path, monkeypatch):
    """受信後のタイトル取得ループ（1 本あたり最大 5 秒）でも停止が効く。"""
    entries = []
    for name in ("a", "b", "c"):
        e = entry_for(tmp_path, name)
        e["id"] = name
        entries.append(e)
    fake_ydl.info = {"entries": entries}
    cancel = threading.Event()
    calls = []

    def fetch(video_id, *a, **k):
        calls.append(video_id)
        cancel.set()  # 1 本目の取得中に停止ボタンが押された想定
        return None

    monkeypatch.setattr(core, "_fetch_localized_title", fetch)
    with pytest.raises(CancelledError):
        core.download_tracks("u", "mp3", cancel=cancel)
    assert calls == ["a"]  # 2 本目以降は問い合わせない


def test_fetch_metadata_cancel_stops_song_lookups(fake_ydl, monkeypatch):
    """情報取得の YouTube Music 曲名ループでも停止が効く。"""
    fake_ydl.info = {
        "entries": [
            {"id": "a", "title": "A", "url": "https://music.youtube.com/watch?v=a"},
            {"id": "b", "title": "B", "url": "https://music.youtube.com/watch?v=b"},
        ]
    }
    cancel = threading.Event()
    calls = []

    def fetch(video_id, *a, **k):
        calls.append(video_id)
        cancel.set()
        return (None, None)

    monkeypatch.setattr(core, "_fetch_ytmusic_song", fetch)
    with pytest.raises(CancelledError):
        core.fetch_metadata("https://music.youtube.com/playlist?list=x", cancel=cancel)
    assert calls == ["a"]


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
    # キー付きで渡す（フラットな list だと FixupM4a など他の ffmpeg PP にも
    # 適用され、-c copy とぶつかって実行ごと落ちる）
    assert fake_ydl.last_opts["postprocessor_args"] == {
        "extractaudio": ["-af", core.loudnorm_filter()]
    }
    core.download_tracks("u", "mp3", normalize=False)
    assert "postprocessor_args" not in fake_ydl.last_opts  # OFF なら付けない


def test_download_tracks_loudness_option(fake_ydl, tmp_path):
    """loudness で loudnorm の基準値 (I) を変えられる（TP / LRA は固定）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", loudness=-9.5)
    assert fake_ydl.last_opts["postprocessor_args"] == {
        "extractaudio": ["-af", "loudnorm=I=-9.5:TP=-1.5:LRA=11"]
    }


def test_download_tracks_trim_silence_option(fake_ydl, tmp_path):
    """trim_silence=True で末尾無音削除フィルタが loudnorm の前段に入る（既定 OFF）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", trim_silence=True)
    assert fake_ydl.last_opts["postprocessor_args"] == {
        "extractaudio": ["-af", core.TRIM_SILENCE_FILTER + "," + core.loudnorm_filter()]
    }
    # ノーマライズ OFF でも無音削除は単独で使える
    core.download_tracks("u", "mp3", normalize=False, trim_silence=True)
    assert fake_ydl.last_opts["postprocessor_args"] == {
        "extractaudio": ["-af", core.TRIM_SILENCE_FILTER]
    }


def test_parse_bitrate():
    """CLI / 設定の値を正規化する（既定 None・取得元と同じ・固定 kbps）。"""
    assert core.parse_bitrate(None) is None
    assert core.parse_bitrate("") is None
    assert core.parse_bitrate("default") is None
    assert core.parse_bitrate("source") == core.BITRATE_SOURCE
    assert core.parse_bitrate(" SOURCE ") == core.BITRATE_SOURCE
    assert core.parse_bitrate("192") == 192
    assert core.parse_bitrate("192k") == 192
    assert core.parse_bitrate("192kbps") == 192
    assert core.parse_bitrate(320) == 320
    with pytest.raises(ValueError):
        core.parse_bitrate("high")
    with pytest.raises(ValueError):
        core.parse_bitrate(9999)  # 範囲外


def test_source_bitrate_kbps():
    """abr を四捨五入して返す。無い / 小さすぎる場合は None（= ffmpeg 既定）。"""
    assert core.source_bitrate_kbps({"abr": 128.93}) == 129
    assert core.source_bitrate_kbps({"abr": None, "tbr": 130.4}) == 130  # 代用
    assert core.source_bitrate_kbps({}) is None
    # 10 以下は yt-dlp が VBR 品質スケールとして解釈してしまうので使わない
    assert core.source_bitrate_kbps({"abr": 8}) is None


def only_pp(fake_ydl):
    """add_post_processor で足された音声抽出 PP を 1 つだけ取り出す。"""
    assert fake_ydl.last_pps is not None and len(fake_ydl.last_pps) == 1
    pp, when = fake_ydl.last_pps[0]
    assert when == "post_process"
    return pp


def run_pp(pp, monkeypatch, info):
    """PP の run() を、親（ffmpeg / ffprobe を起動する）を止めて呼ぶ。"""
    monkeypatch.setattr(type(pp).__mro__[1], "run", lambda self, i: None, raising=True)
    pp.run(info)


def test_download_tracks_bitrate_fixed(fake_ydl, tmp_path, monkeypatch):
    """audio_bitrate=数値 が PP の目標ビットレート(preferredquality)になる。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")  # 既定は指定なし（ffmpeg 任せ）
    assert only_pp(fake_ydl)._preferredquality is None
    core.download_tracks("u", "mp3", audio_bitrate=320)
    pp = only_pp(fake_ydl)
    assert pp._preferredquality == 320
    assert pp._quality_args("libmp3lame") == ["-b:a", "320.0k"]
    # 文字列でも同じ（CLI からはそのまま渡ってくる）
    core.download_tracks("u", "mp3", audio_bitrate="192k")
    assert only_pp(fake_ydl)._preferredquality == 192


def test_download_tracks_bitrate_ignored_for_wav(fake_ydl, tmp_path):
    """wav は非圧縮なのでビットレート指定は捨てる（PCM に -b:a は効かない）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.wav").write_bytes(b"\x00")  # 変換後のファイル
    core.download_tracks("u", "wav", audio_bitrate=320)
    pp = only_pp(fake_ydl)
    assert pp._preferredquality is None
    assert pp._match_source_bitrate is False
    core.download_tracks("u", "wav", audio_bitrate=core.BITRATE_SOURCE)
    assert only_pp(fake_ydl)._match_source_bitrate is False  # 「取得元と同じ」も同様


def test_download_tracks_bitrate_source_reads_abr(fake_ydl, tmp_path, monkeypatch):
    """audio_bitrate="source" は変換直前に情報 dict の abr を目標値へ移す。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", audio_bitrate=core.BITRATE_SOURCE)
    pp = only_pp(fake_ydl)
    assert pp._match_source_bitrate is True
    run_pp(pp, monkeypatch, {"abr": 128.93, "asr": 48000})
    assert pp._preferredquality == 129
    assert pp._quality_args("libmp3lame") == ["-b:a", "129k"]


def test_extract_audio_pp_pins_source_sample_rate(fake_ydl, tmp_path, monkeypatch):
    """変換後のサンプリングレートは取得元に固定する（loudnorm の 192kHz 対策）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.m4a").write_bytes(b"\x00")  # 変換後のファイル
    core.download_tracks("u", "m4a")
    pp = only_pp(fake_ydl)
    run_pp(pp, monkeypatch, {"asr": 48000})
    calls = []
    monkeypatch.setattr(
        type(pp).__mro__[1],
        "run_ffmpeg",
        lambda self, path, out, codec, opts: calls.append((codec, opts)),
        raising=True,
    )
    pp.run_ffmpeg("in.webm", "out.m4a", "aac", ["-x"])
    assert calls[-1] == ("aac", ["-x", "-ar", "48000"])
    # 再エンコードしない(copy)ときは触らない
    pp.run_ffmpeg("in.m4a", "out.m4a", "copy", [])
    assert calls[-1] == ("copy", [])
    # asr が取れない音源では従来どおり ffmpeg 任せ
    run_pp(pp, monkeypatch, {})
    pp.run_ffmpeg("in.webm", "out.m4a", "aac", [])
    assert calls[-1] == ("aac", [])


def test_download_tracks_falls_back_to_opts_postprocessor(fake_ydl, tmp_path, monkeypatch):
    """派生 PP を作れない環境では opts の postprocessors 指定に落ちる。"""
    monkeypatch.setattr(core, "_extract_audio_pp_class", lambda: None)
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3", audio_bitrate=320)
    assert fake_ydl.last_pps == []
    assert fake_ydl.last_opts["postprocessors"] == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}
    ]


def test_download_tracks_best_quality_option(fake_ydl, tmp_path):
    """best_quality=True でフォーマットの並べ替え順を音質優先へ差し替える。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    core.download_tracks("u", "mp3")
    assert "format_sort" not in fake_ydl.last_opts  # 既定は yt-dlp 任せ
    core.download_tracks("u", "mp3", best_quality=True)
    assert fake_ydl.last_opts["format_sort"] == list(core.BEST_AUDIO_SORT)


def test_write_and_read_tags_opus(tmp_path):
    """opus は Vorbis コメント（TITLE / ARTIST / ALBUM）に読み書きする。"""
    p = make_opus(tmp_path)
    core.write_title(p, "曲名", artist="作者", album="アルバム")
    assert core.read_tags(p) == {"title": "曲名", "artist": "作者", "album": "アルバム"}
    # 空文字の項目は既存値を残す（他形式と同じ方針）
    core.write_title(p, "", artist="作者2")
    assert core.read_tags(p) == {"title": "曲名", "artist": "作者2", "album": "アルバム"}


def test_track_from_file_opus(tmp_path):
    """取り込み時も opus の既存タグを読む（推定をスキップした PENDING 行）。"""
    p = make_opus(tmp_path)
    core.write_title(p, "既存曲名", artist="既存作者")
    track = core.track_from_file(p)
    assert track.guessed_title == "既存曲名"
    assert track.artist == "既存作者"
    assert track.skip_infer is True and track.status is core.Status.PENDING


def test_is_native_codec():
    """出力形式のまま保存できるコーデックか（YouTube の acodec 表記に合わせる）。"""
    assert core.is_native_codec("opus", "opus") is True
    assert core.is_native_codec("m4a", "mp4a.40.2") is True
    assert core.is_native_codec("opus", "mp4a.40.2") is False
    assert core.is_native_codec("mp3", "opus") is False  # mp3 は常に変換
    assert core.is_native_codec("opus", None) is False
    assert core.is_native_codec("opus", "none") is False


def test_download_tracks_opus_defaults_to_source_bitrate(fake_ydl, tmp_path):
    """opus は指定なしでも取得元のビットレートに合わせる（libopus 既定は 96kbps）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.opus").write_bytes(b"\x00")
    core.download_tracks("u", "opus")
    assert only_pp(fake_ydl)._match_source_bitrate is True
    # 明示した値はそのまま優先される
    core.download_tracks("u", "opus", audio_bitrate=192)
    pp = only_pp(fake_ydl)
    assert pp._match_source_bitrate is False and pp._preferredquality == 192
    # 他形式の既定は従来どおり ffmpeg 任せ
    core.download_tracks("u", "mp3")
    assert only_pp(fake_ydl)._match_source_bitrate is False


def test_download_tracks_prefers_copyable_format_without_filters(fake_ydl, tmp_path):
    """フィルタ無しなら出力形式と同じコーデックの音源を優先して取る。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.opus").write_bytes(b"\x00")
    core.download_tracks("u", "opus", normalize=False)
    assert fake_ydl.last_opts["format"] == core._COPY_FORMAT_SELECTOR["opus"]
    # ノーマライズ ON では再エンコードが必須なので、素直に最良の音源を取る
    core.download_tracks("u", "opus")
    assert fake_ydl.last_opts["format"] == core.DEFAULT_FORMAT_SELECTOR
    # mp3 / wav はどのみち変換なので既定のまま
    core.download_tracks("u", "mp3", normalize=False)
    assert fake_ydl.last_opts["format"] == core.DEFAULT_FORMAT_SELECTOR


def test_download_tracks_force_encode_with_filters(fake_ydl, tmp_path, monkeypatch):
    """フィルタを掛ける行の PP は copy を選ばせない（-af と同居できないため）。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.opus").write_bytes(b"\x00")
    # 親の get_audio_codec は ffprobe を起動するので差し替える
    monkeypatch.setattr(
        core._extract_audio_pp_class().__mro__[1],
        "get_audio_codec",
        lambda self, path: "opus",
        raising=True,
    )
    core.download_tracks("u", "opus")  # ノーマライズ ON
    forced = only_pp(fake_ydl)
    assert forced._force_encode is True
    # 出力形式と一致しない値を返す = 親の run が再エンコード側の分岐へ行く
    assert forced.get_audio_codec("x.webm") != "opus"
    core.download_tracks("u", "opus", normalize=False)
    plain = only_pp(fake_ydl)
    assert plain._force_encode is False
    assert plain.get_audio_codec("x.webm") == "opus"  # そのまま = copy が選ばれる


def test_extract_audio_pp_skips_ar_for_opus(fake_ydl, tmp_path, monkeypatch):
    """libopus は 48kHz 固定。-ar を渡すと変換が落ちるので付けない。"""
    fake_ydl.info = entry_for(tmp_path, "a")
    (tmp_path / "a.opus").write_bytes(b"\x00")
    core.download_tracks("u", "opus", normalize=False)
    pp = only_pp(fake_ydl)
    run_pp(pp, monkeypatch, {"asr": 44100})
    calls = []
    monkeypatch.setattr(
        type(pp).__mro__[1],
        "run_ffmpeg",
        lambda self, path, out, codec, opts: calls.append((codec, opts)),
        raising=True,
    )
    pp.run_ffmpeg("in.m4a", "out.opus", "libopus", [])
    assert calls[-1] == ("libopus", [])  # -ar は付かない
    # mp3 が対応するレートなら付ける
    pp.run_ffmpeg("in.webm", "out.mp3", "libmp3lame", [])
    assert calls[-1] == ("libmp3lame", ["-ar", "44100"])
    # 対応外のレート（96kHz を mp3 へ）は付けない
    run_pp(pp, monkeypatch, {"asr": 96000})
    pp.run_ffmpeg("in.webm", "out.mp3", "libmp3lame", [])
    assert calls[-1] == ("libmp3lame", [])


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


# ---------------------------------------------------------------------------
# yt-dlp のエラー文言の引き継ぎ（ignoreerrors で握り潰される理由を CoreError へ）
# ---------------------------------------------------------------------------


PREMIUM_ERROR = (
    "[youtube] BCQEq6EM_mM: この動画を視聴できるのは、Music Premium のメンバーのみです"
)


def test_download_tracks_raises_ydl_reason(fake_ydl):
    """DL 失敗時は「URL を確認してください」ではなく yt-dlp の理由を出す。"""
    fake_ydl.info = None
    fake_ydl.error_feed = [PREMIUM_ERROR]
    with pytest.raises(CoreError) as ei:
        core.download_tracks("https://music.youtube.com/watch?v=BCQEq6EM_mM", "mp3")
    assert str(ei.value) == PREMIUM_ERROR


def test_download_tracks_no_files_raises_ydl_reason(fake_ydl):
    """entries が全滅した場合も理由を引き継ぐ。"""
    fake_ydl.info = {"entries": [None]}
    fake_ydl.error_feed = [PREMIUM_ERROR]
    with pytest.raises(CoreError) as ei:
        core.download_tracks("u", "mp3")
    assert str(ei.value) == PREMIUM_ERROR


def test_download_tracks_falls_back_to_generic_message(fake_ydl):
    """理由が 1 件も報告されなければ従来の汎用文言のまま。"""
    fake_ydl.info = None
    with pytest.raises(CoreError) as ei:
        core.download_tracks("u", "mp3")
    assert str(ei.value) == core.GENERIC_EXTRACT_ERROR


def test_fetch_metadata_raises_ydl_reason(fake_ydl):
    fake_ydl.info = None
    fake_ydl.error_feed = [PREMIUM_ERROR]
    with pytest.raises(CoreError) as ei:
        core.fetch_metadata("u")
    assert str(ei.value) == PREMIUM_ERROR


def test_ydl_error_message_normalizes_and_dedupes():
    msgs = [
        "ERROR: boom" + chr(10) + "You might want to use a VPN.",
        "boom You might want to use a VPN.",  # 同一 → 1 回だけ
        "second",
        "   ",  # 空白のみ → 落とす
    ]
    assert core._ydl_error_message(msgs, "fallback") == (
        "boom You might want to use a VPN. / second"
    )
    assert core._ydl_error_message([], "fallback") == "fallback"


def test_record_ydl_errors_tolerates_missing_report_error():
    """report_error を持たない実装でも落ちない（差し替えは諦めて空のまま）。"""

    class Bare:
        pass

    assert core._record_ydl_errors(Bare()) == []


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


def test_check_connection_shows_resolved_model(monkeypatch):
    """publisher 省略の MODEL は、一覧の完全な id に解決して使うことを表示する。"""
    _fake_config(monkeypatch, model="gemma-4-e2b")
    _fake_urlopen(
        monkeypatch, b'{"data": [{"id": "google/gemma-4-e4b"}, {"id": "google/gemma-4-e2b"}]}'
    )
    ok, msg = core.check_connection()
    assert ok
    assert "gemma-4-e2b → google/gemma-4-e2b" in msg
    assert "注意" not in msg


# ---------------------------------------------------------------------------
# make_client / 応答モデルの確認（実装は mv2title 0.5.0、ここは利用側の配線）
# （LM Studio は一覧と完全一致しない名前だとロード中の別モデルで黙って答える）
# ---------------------------------------------------------------------------

_SERVER_BODY = (
    b'{"data": [{"id": "google/gemma-4-e4b"}, {"id": "google/gemma-4-e2b"},'
    b' {"id": "hy-mt2-1.8b@bf16"}, {"id": "hy-mt2-1.8b@8bit"}]}'
)


def test_make_client_resolves_model(monkeypatch):
    """MODEL の解決は mv2title.make_client が行う（core は設定を渡すだけ）。"""
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:1234/v1/")
    monkeypatch.setenv("MODEL", "gemma-4-e2b")
    _fake_urlopen(monkeypatch, _SERVER_BODY)
    client = core.make_client()
    assert client.config.model == "google/gemma-4-e2b"
    assert isinstance(client, mv2title.ModelCheckedClient)


def test_make_client_keeps_model_when_list_unavailable(monkeypatch):
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:1234/v1/")
    monkeypatch.setenv("MODEL", "gemma-4-e2b")

    def boom(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert core.make_client().config.model == "gemma-4-e2b"


def _checked_client(monkeypatch, answered, model="google/gemma-4-e2b"):
    """応答の model 欄が answered になる ModelCheckedClient と、送信の記録。"""
    sent = []

    def fake_send(self, prompt, system_prompt=None, model_name=None, **kwargs):
        sent.append(kwargs)
        message = types.SimpleNamespace(content='{"results": [{"id": 1, "title": "Song"}]}')
        return types.SimpleNamespace(
            model=answered, choices=[types.SimpleNamespace(message=message)]
        )

    monkeypatch.setattr(core.LLMClient, "send_message", fake_send)
    config = core.Config(base_url="http://127.0.0.1:1234/v1/", model=model)
    return mv2title.ModelCheckedClient(config), sent


def test_infer_titles_errors_on_substituted_model(monkeypatch):
    """違うモデルが答えたら行を ERROR にし、mv2title の再送でも推論させない。

    検出はライブラリ側だが、行を ERROR にするには CoreError で返る必要がある。
    """
    client, sent = _checked_client(monkeypatch, answered="google/gemma-4-e4b")
    tracks = [Track(stem="MIMI『 Pale 』feat. 初音ミク")]
    with pytest.raises(core.ModelMismatchError) as exc:
        core.infer_titles(tracks, client=client)
    assert isinstance(exc.value, core.CoreError)
    assert "google/gemma-4-e4b" in str(exc.value)
    assert core.MODEL_MISMATCH_HINT in str(exc.value)
    assert tracks[0].status == Status.ERROR
    assert "google/gemma-4-e4b" in tracks[0].error
    # 一度不一致を見たら送信せずに止める（違うモデルでもう一度推論させない）
    assert len(sent) == 1
