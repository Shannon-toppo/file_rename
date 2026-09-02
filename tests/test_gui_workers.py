# -*- coding: utf-8 -*-
"""PipelineWorker のオフラインテスト。

core.download_tracks / infer_titles / write_tags を monkeypatch した
フェイクへ差し替え、qtbot.waitSignal でパイプライン完走を検証する。
LLM・yt-dlp・ネットワークは使わない。
"""
import threading
import time

import pytest
from PySide6.QtCore import Qt, QThreadPool

import core
from core import CoreError, Status, Track
from gui.workers import MODE_FETCH, MODE_FULL, MODE_INFER, PipelineWorker


# ---------------------------------------------------------------------------
# フェイク
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def connection_ok(monkeypatch):
    """MODE_FULL 冒頭の接続チェックをオフラインで常に成功させる。

    実物は urllib で BASE_URL を叩くため、テストでは必ず差し替える
    （縮退モードのテストは個別に (False, ...) へ上書きする）。
    """
    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (True, "ok"))


def fake_download_factory(monkeypatch, mapping):
    """url -> list[Track] を返す download_tracks のフェイク。"""

    def fake(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        # core.download_tracks の追加キーワード引数(out_dir 等)は **kwargs で吸収
        # （署名がずれると TypeError が行単位エラーに化けるため。CLAUDE.md 参照）
        if on_progress is not None:
            on_progress("f.webm", 50.0)
        if cancel is not None and cancel.is_set():
            raise core.CancelledError("cancelled")
        result = mapping.get(url)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(core, "download_tracks", fake)


def fake_infer_factory(monkeypatch, *, raises=None):
    """guessed_title/valid を埋める infer_titles のフェイク。"""
    calls = []

    def fake(tracks, client=None, batch_size=5, force=False):
        calls.append({"tracks": list(tracks), "force": force})
        targets = [t for t in tracks if force or not t.manual]
        if raises is not None:
            for t in targets:
                t.status = Status.ERROR
                t.error = "推定失敗"
            raise raises
        for i, t in enumerate(targets):
            t.guessed_title = f"song{i}"
            t.valid = True
            t.manual = False
            t.status = Status.PENDING

    monkeypatch.setattr(core, "infer_titles", fake)
    return calls


def fake_write_factory(monkeypatch):
    """全行を DONE にする write_tags のフェイク。"""
    written = []

    def fake(tracks, on_result=None):
        for t in tracks:
            t.status = Status.DONE
            written.append(t)
            if on_result is not None:
                on_result(t)

    monkeypatch.setattr(core, "write_tags", fake)
    return written


def run_worker(qtbot, worker, timeout=3000):
    """ワーカーを threadpool で実行し、finished まで待つ。"""
    with qtbot.waitSignal(worker.signals.finished, timeout=timeout):
        QThreadPool.globalInstance().start(worker)
    # プール上のスレッドが完全に片付くまで待つ
    QThreadPool.globalInstance().waitForDone(timeout)


# ---------------------------------------------------------------------------
# フルパイプライン
# ---------------------------------------------------------------------------


def test_full_pipeline_auto_write_reaches_done(qtbot, monkeypatch):
    dl = Track(stem="a.mp3", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://u": [dl]})
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u", status=Status.QUEUED)
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)

    ready_events = []
    worker.signals.tracks_ready.connect(lambda ph, ts: ready_events.append((ph, ts)))
    run_worker(qtbot, worker)

    # プレースホルダは実 Track へ差し替えられ、その実 Track が DONE
    assert len(ready_events) == 1
    _, real_tracks = ready_events[0]
    assert real_tracks[0] is dl
    assert dl.status is Status.DONE
    assert dl.guessed_title == "song0"


def test_full_pipeline_no_auto_write_stops_at_pending(qtbot, monkeypatch):
    dl = Track(stem="a.mp3", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://u": [dl]})
    fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=False)
    run_worker(qtbot, worker)

    assert dl.status is Status.PENDING
    assert written == []  # 書き込みは呼ばれない


def test_local_file_row_skips_download(qtbot, monkeypatch):
    called = []
    monkeypatch.setattr(
        core, "download_tracks", lambda *a, **k: called.append(a) or []
    )
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    local = Track(stem="local", filepath="local.mp3", status=Status.QUEUED)
    worker = PipelineWorker([local], mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert called == []  # DL は呼ばれない
    assert local.status is Status.DONE


# ---------------------------------------------------------------------------
# エラー・キャンセル
# ---------------------------------------------------------------------------


def test_download_failure_marks_error_and_continues(qtbot, monkeypatch):
    good = Track(stem="g.mp3", filepath="g.mp3")
    fake_download_factory(
        monkeypatch,
        {"http://bad": RuntimeError("dl error"), "http://good": [good]},
    )
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    bad_ph = Track(stem="http://bad", url="http://bad")
    good_ph = Track(stem="http://good", url="http://good")
    worker = PipelineWorker([bad_ph, good_ph], mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert bad_ph.status is Status.ERROR
    assert "dl error" in bad_ph.error
    # 失敗行があっても後続行は処理される
    assert good.status is Status.DONE


def test_infer_core_error_emitted(qtbot, monkeypatch):
    dl = Track(stem="a.mp3", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://u": [dl]})
    fake_infer_factory(monkeypatch, raises=CoreError("件数不一致"))
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)

    errors = []
    worker.signals.error.connect(errors.append)
    run_worker(qtbot, worker)

    assert errors and "件数不一致" in errors[0]
    assert dl.status is Status.ERROR


def test_cancel_stops_remaining_rows(qtbot, monkeypatch):
    cancel = threading.Event()

    def fake_dl(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        # 1 件目の DL 中にキャンセルが立っている想定
        raise core.CancelledError("cancelled")

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    infer_calls = fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    cancel.set()
    p1 = Track(stem="http://1", url="http://1")
    p2 = Track(stem="http://2", url="http://2")
    worker = PipelineWorker([p1, p2], mode=MODE_FULL, cancel=cancel)
    run_worker(qtbot, worker)

    # キャンセルで推定・書き込みまで到達しない
    assert infer_calls == []
    assert written == []


# ---------------------------------------------------------------------------
# 再推定モード
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 縮退モード（LLM 未接続 → DL のみ）と設定値の伝搬
# ---------------------------------------------------------------------------


def test_connection_failure_degrades_to_download_only(qtbot, monkeypatch):
    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (False, "down"))
    dl = Track(stem="a.mp3", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://u": [dl]})
    infer_calls = fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)
    failures = []
    worker.signals.connection_failed.connect(failures.append)
    run_worker(qtbot, worker)

    assert failures == ["down"]
    # DL はされるが推定・書き込みへ進まない（QUEUED で残り、再実行で続きから）
    assert infer_calls == [] and written == []
    assert dl.status is Status.QUEUED


def direct_track(stem: str, title: str) -> Track:
    """YouTube Music 由来の「推定不要」行（core.use_metadata_title 相当）。"""
    t = Track(stem=stem, filepath=f"{stem}.mp3")
    core.use_metadata_title(t, title)
    return t


def test_full_pipeline_skips_infer_for_direct_rows(qtbot, monkeypatch):
    """skip_infer 行は推定へ渡さず、書き込みだけ行う（YouTube Music）。"""
    direct = direct_track("ytm", "Song")
    normal = Track(stem="n.mp3", filepath="n.mp3")
    fake_download_factory(
        monkeypatch, {"http://ytm": [direct], "http://n": [normal]}
    )
    infer_calls = fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    phs = [Track(stem="http://ytm", url="http://ytm"), Track(stem="http://n", url="http://n")]
    worker = PipelineWorker(phs, mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    inferred = [t for c in infer_calls for t in c["tracks"]]
    assert direct not in inferred and normal in inferred
    assert direct.guessed_title == "Song"  # 推定で上書きされない
    assert sorted(id(t) for t in written) == sorted(id(t) for t in (direct, normal))
    assert direct.status is Status.DONE


def test_degraded_mode_still_writes_direct_rows(qtbot, monkeypatch):
    """LLM 未接続でも、推定の要らない行（YouTube Music）は書き込む。"""
    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (False, "down"))
    direct = direct_track("ytm", "Song")
    normal = Track(stem="n.mp3", filepath="n.mp3")
    fake_download_factory(
        monkeypatch, {"http://ytm": [direct], "http://n": [normal]}
    )
    infer_calls = fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    phs = [Track(stem="http://ytm", url="http://ytm"), Track(stem="http://n", url="http://n")]
    worker = PipelineWorker(phs, mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert infer_calls == []
    assert written == [direct]  # 推定が要る行は QUEUED のまま残す
    assert direct.status is Status.DONE
    assert normal.status is Status.QUEUED


def test_degraded_mode_without_auto_write_leaves_direct_rows_pending(qtbot, monkeypatch):
    monkeypatch.setattr(core, "check_connection", lambda timeout=3.0: (False, "down"))
    direct = direct_track("ytm", "Song")
    fake_download_factory(monkeypatch, {"http://ytm": [direct]})
    fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    worker = PipelineWorker(
        [Track(stem="http://ytm", url="http://ytm")], mode=MODE_FULL, auto_write=False
    )
    run_worker(qtbot, worker)
    assert written == [] and direct.status is Status.PENDING


def test_download_carries_over_direct_title_from_fetched_row(qtbot, monkeypatch):
    """情報取得段で確定した曲名は、DL 後の実 Track へ引き継がれる。

    YouTube Music の再生リストを展開すると各行の URL は www.youtube.com に
    なるため、DL 段だけでは YouTube Music と判定できない。
    """
    real = Track(stem="Song (Official) [id]", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://v/a": [real]})
    infer_calls = fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    fetched = Track(stem="Song", url="http://v/a")
    core.use_metadata_title(fetched)
    worker = PipelineWorker([fetched], mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert real.skip_infer is True
    assert real.guessed_title == "Song"
    assert [t for c in infer_calls for t in c["tracks"]] == []
    assert real.status is Status.DONE


def test_reinfer_does_not_check_connection(qtbot, monkeypatch):
    """MODE_INFER は接続チェックしない（失敗すれば既存のエラー経路に乗る）。"""
    called = []
    monkeypatch.setattr(
        core, "check_connection", lambda timeout=3.0: called.append(1) or (False, "x")
    )
    fake_infer_factory(monkeypatch)
    t = Track(stem="s", status=Status.PENDING)
    worker = PipelineWorker([t], mode=MODE_INFER, force=True)
    run_worker(qtbot, worker)
    assert called == []


def test_worker_passes_download_and_infer_options(qtbot, monkeypatch, tmp_path):
    captured = {}

    def fake_dl(
        url,
        fmt="mp3",
        on_progress=None,
        on_stage=None,
        cancel=None,
        out_dir=None,
        expand_playlist=False,
        normalize=True,
        loudness=core.NORMALIZE_TARGET_I,
        trim_silence=False,
        ytmusic_direct=True,
        logger=None,
    ):
        captured["out_dir"] = out_dir
        captured["expand_playlist"] = expand_playlist
        captured["normalize"] = normalize
        captured["loudness"] = loudness
        captured["trim_silence"] = trim_silence
        captured["ytmusic_direct"] = ytmusic_direct
        return [Track(stem="a", filepath="a.mp3")]

    def fake_infer(tracks, client=None, batch_size=5, force=False):
        captured["batch_size"] = batch_size
        for t in tracks:
            t.guessed_title = "x"
            t.valid = True
            t.status = Status.PENDING

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    monkeypatch.setattr(core, "infer_titles", fake_infer)
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker(
        [placeholder],
        mode=MODE_FULL,
        auto_write=True,
        batch_size=7,
        out_dir=tmp_path,
        expand_playlist=True,
        normalize=False,
        loudness=-10.0,
        trim_silence=True,
        ytmusic_direct=False,
    )
    run_worker(qtbot, worker)
    assert captured == {
        "out_dir": tmp_path,
        "batch_size": 7,
        "expand_playlist": True,
        "normalize": False,
        "loudness": -10.0,
        "trim_silence": True,
        "ytmusic_direct": False,
    }


def test_worker_passes_yt_dlp_logger_to_download(qtbot, monkeypatch):
    """GUI 経由の DL は core.download_tracks に yt_dlp ロガーを渡す。"""
    import logging

    captured = {}

    def fake_dl(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        captured["logger"] = kwargs.get("logger")
        return [Track(stem="a", filepath="a.mp3")]

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert captured["logger"] is logging.getLogger("yt_dlp")


def test_write_summary_emitted(qtbot, monkeypatch, tmp_path):
    """書き込み後に (完了, スキップ, 失敗) の集計が通知される。"""
    from gui.workers import MODE_WRITE

    ok = Track(stem="ok", filepath=tmp_path / "ok.mp3", guessed_title="song", valid=True)
    (tmp_path / "ok.mp3").write_bytes(b"\x00" * 128)
    empty = Track(stem="e", filepath=tmp_path / "e.mp3", guessed_title="", valid=True)
    bad = Track(stem="b", guessed_title="x", valid=True)  # filepath 無し → ERROR

    worker = PipelineWorker([ok, empty, bad], mode=MODE_WRITE)
    summaries = []
    worker.signals.write_summary.connect(lambda d, s, e: summaries.append((d, s, e)))
    run_worker(qtbot, worker)

    assert summaries == [(1, 1, 1)]  # 完了 1 / スキップ(空タイトル) 1 / 失敗 1


def test_progress_signal_carries_playlist_label(qtbot, monkeypatch):
    """core からの (index, total) が「2/5」ラベルとして progress に載る。"""
    dl = Track(stem="a", filepath="a.mp3")

    def fake_dl(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        if on_progress is not None:
            on_progress("f.webm", 30.0, 2, 5)
            on_progress("f.webm", 60.0)  # 単一動画相当（ラベル無し）
        return [dl]

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)
    seen = []
    worker.signals.progress.connect(lambda t, p, label: seen.append((p, label)))
    run_worker(qtbot, worker)

    assert seen == [(30.0, "2/5"), (60.0, "")]


def test_reinfer_force_overrides_manual(qtbot, monkeypatch):
    calls = fake_infer_factory(monkeypatch)
    manual = Track(stem="m", guessed_title="ユーザー入力", manual=True, status=Status.PENDING)
    worker = PipelineWorker([manual], mode=MODE_INFER, force=True)
    run_worker(qtbot, worker)

    assert calls and calls[0]["force"] is True
    # force のフェイクが manual 行も対象にして上書き
    assert manual.guessed_title == "song0"
    assert manual.manual is False


def test_downloaded_row_not_redownloaded_on_second_run(qtbot, monkeypatch):
    """DL 済みの実 Track 行（url と filepath の両方を持つ）は再実行で再 DL しない。"""
    called = []
    monkeypatch.setattr(
        core, "download_tracks", lambda *a, **k: called.append(a) or []
    )
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    done = Track(stem="a", url="http://u", filepath="a.mp3", status=Status.DONE)
    pending = Track(stem="b", url="http://u2", filepath="b.mp3", status=Status.PENDING)
    worker = PipelineWorker([done, pending], mode=MODE_FULL, auto_write=True)
    run_worker(qtbot, worker)

    assert called == []  # 再ダウンロードされない
    assert done.status is Status.DONE  # DONE 行は再推定もされない
    assert pending.status is Status.DONE  # PENDING 行は推定→書き込みまで進む


def test_fetch_mode_expands_placeholder(qtbot, monkeypatch):
    """MODE_FETCH は未取得のプレースホルダ行だけをメタデータ行へ差し替える。"""
    a = Track(stem="A", url="http://v/a", channel="Ch")
    b = Track(stem="B", url="http://v/b")
    fetched_urls = []

    def fake_fetch(url, cancel=None, expand_playlist=False, **kwargs):
        fetched_urls.append(url)
        return [a, b]

    monkeypatch.setattr(core, "fetch_metadata", fake_fetch)

    placeholder = Track(stem="http://list", url="http://list")
    local = Track(stem="local", filepath="local.mp3")
    done = Track(stem="C", url="http://v/c")  # stem != url → 取得済み扱い
    worker = PipelineWorker([placeholder, local, done], mode=MODE_FETCH)
    ready = []
    worker.signals.tracks_ready.connect(lambda ph, ts: ready.append((ph, ts)))
    run_worker(qtbot, worker)

    # ローカル行・取得済み行はスキップされ、プレースホルダだけが展開される
    assert fetched_urls == ["http://list"]
    assert len(ready) == 1
    assert ready[0][0] is placeholder
    assert ready[0][1] == [a, b]
    # 展開後の行は QUEUED のまま（そのまま実行すれば DL される）
    assert a.status is Status.QUEUED


def test_fetch_mode_error_isolation(qtbot, monkeypatch):
    ok = Track(stem="A", url="http://v/a")

    def fake_fetch(url, cancel=None, expand_playlist=False, **kwargs):
        if url == "http://bad":
            raise RuntimeError("boom")
        return [ok]

    monkeypatch.setattr(core, "fetch_metadata", fake_fetch)
    bad = Track(stem="http://bad", url="http://bad")
    good = Track(stem="http://good", url="http://good")
    worker = PipelineWorker([bad, good], mode=MODE_FETCH)
    ready = []
    worker.signals.tracks_ready.connect(lambda ph, ts: ready.append(ph))
    run_worker(qtbot, worker)

    assert bad.status is Status.ERROR and "boom" in bad.error
    assert ready and ready[0] is good  # 失敗行があっても後続行は処理される


def test_fetch_stage_summary_counts_failures(qtbot, monkeypatch):
    """情報取得の完了/失敗件数が stage_summary で通知される。"""
    ok = Track(stem="A", url="http://v/a")

    def fake_fetch(url, cancel=None, expand_playlist=False, **kwargs):
        if url == "http://bad":
            raise RuntimeError("boom")
        return [ok]

    monkeypatch.setattr(core, "fetch_metadata", fake_fetch)
    bad = Track(stem="http://bad", url="http://bad")
    good = Track(stem="http://good", url="http://good")
    worker = PipelineWorker([bad, good], mode=MODE_FETCH)
    summaries = []
    worker.signals.stage_summary.connect(lambda s, d, e: summaries.append((s, d, e)))
    run_worker(qtbot, worker)

    assert summaries == [("情報取得", 1, 1)]


def test_download_stage_summary_counts_failures(qtbot, monkeypatch):
    """DL の完了/失敗件数が stage_summary で通知される。"""
    good = Track(stem="g.mp3", filepath="g.mp3")
    fake_download_factory(
        monkeypatch,
        {"http://bad": RuntimeError("dl error"), "http://good": [good]},
    )
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    bad_ph = Track(stem="http://bad", url="http://bad")
    good_ph = Track(stem="http://good", url="http://good")
    worker = PipelineWorker([bad_ph, good_ph], mode=MODE_FULL, auto_write=True)
    summaries = []
    worker.signals.stage_summary.connect(lambda s, d, e: summaries.append((s, d, e)))
    run_worker(qtbot, worker)

    assert summaries == [("ダウンロード", 1, 1)]


def test_stage_summary_not_emitted_for_local_only(qtbot, monkeypatch):
    """DL を試みる行が無ければ集計は出さない（ローカル行のみの実行でノイズを出さない）。"""
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)
    local = Track(stem="local", filepath="local.mp3", status=Status.QUEUED)
    worker = PipelineWorker([local], mode=MODE_FULL, auto_write=True)
    summaries = []
    worker.signals.stage_summary.connect(lambda *a: summaries.append(a))
    run_worker(qtbot, worker)

    assert summaries == []


def test_fetch_mode_passes_expand_playlist(qtbot, monkeypatch):
    captured = {}

    def fake_fetch(url, cancel=None, expand_playlist=False, **kwargs):
        captured["expand_playlist"] = expand_playlist
        return [Track(stem="A", url="http://v/a")]

    monkeypatch.setattr(core, "fetch_metadata", fake_fetch)
    placeholder = Track(stem="http://u", url="http://u")
    worker = PipelineWorker([placeholder], mode=MODE_FETCH, expand_playlist=True)
    run_worker(qtbot, worker)
    assert captured["expand_playlist"] is True


def test_download_carries_over_manual_edits(qtbot, monkeypatch):
    """情報取得済み行への手動編集は、DL 後の実 Track 行へ引き継がれる。"""
    dl = Track(stem="a", filepath="a.mp3")
    fake_download_factory(monkeypatch, {"http://v/a": [dl]})
    infer_calls = fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    fetched = Track(
        stem="A",
        url="http://v/a",
        guessed_title="手動タイトル",
        manual=True,
        valid=True,
        artist="Ch",
    )
    worker = PipelineWorker([fetched], mode=MODE_FULL, auto_write=False)
    run_worker(qtbot, worker)

    assert dl.guessed_title == "手動タイトル" and dl.manual is True
    assert dl.artist == "Ch"
    assert dl.status is Status.PENDING
    assert infer_calls == []  # manual 行は推定対象にならない


def test_reinfer_without_force_protects_manual(qtbot, monkeypatch):
    calls = fake_infer_factory(monkeypatch)
    manual = Track(stem="m", guessed_title="keep", manual=True, status=Status.PENDING)
    # MODE_INFER でも force=False なら manual 行は対象外
    worker = PipelineWorker([manual], mode=MODE_INFER, force=False)
    run_worker(qtbot, worker)

    # ワーカーの対象選定で manual 行が除外され、infer は呼ばれない
    assert calls == []
    assert manual.guessed_title == "keep"



def test_download_stage_switches_row_to_converting(qtbot, monkeypatch):
    """core の on_stage 通知が行の状態（変換中 / 情報取得中）に反映される。

    受信完了後は進捗率が出ないので、状態を差し替えないと「DL中 100%」の
    まま固まって見える。
    """
    dl = Track(stem="a", filepath="a.mp3")

    def fake_dl(url, fmt="mp3", on_progress=None, on_stage=None, cancel=None, **kwargs):
        if on_progress is not None:
            on_progress("a.webm", 100.0)
        if on_stage is not None:
            on_stage(Status.CONVERTING)
            on_stage(Status.FETCHING)
        return [dl]

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholder = Track(stem="http://u", url="http://u")
    seen = []
    worker = PipelineWorker([placeholder], mode=MODE_FULL, auto_write=True)
    # DirectConnection で emit した瞬間の状態を拾う（既定の queued だと
    # スロットが動くころには status が先へ進んでいて何も検証できない）
    worker.signals.track_updated.connect(
        lambda t: seen.append(t.status) if t is placeholder else None,
        Qt.ConnectionType.DirectConnection,
    )
    run_worker(qtbot, worker)

    assert seen == [Status.DOWNLOADING, Status.CONVERTING, Status.FETCHING]


# ---------------------------------------------------------------------------
# DL 同士の並列（max_downloads）
# ---------------------------------------------------------------------------


def test_downloads_run_in_parallel(qtbot, monkeypatch):
    """max_downloads=2 なら 1 本目の DL 中に 2 本目が始まる。

    yt-dlp は 1 呼び出しの中で受信と ffmpeg 変換を直列に回すので、行を
    またいで重ねられないと変換の間ずっと回線が空く。
    """
    second_started = threading.Event()
    waited = []

    def fake_dl(url, fmt="mp3", on_progress=None, on_stage=None, cancel=None, **kwargs):
        if url == "u0":
            # 2 本目が走り出すまで待つ（直列なら待ち切れず False が入る）
            waited.append(second_started.wait(timeout=5))
        else:
            second_started.set()
        return [Track(stem=url, filepath=f"{url}.mp3")]

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholders = [Track(stem=u, url=u) for u in ("u0", "u1")]
    worker = PipelineWorker(placeholders, mode=MODE_FULL, auto_write=True, max_downloads=2)
    run_worker(qtbot, worker, timeout=10000)

    assert waited == [True]


def test_max_downloads_one_keeps_downloads_serial(qtbot, monkeypatch):
    """max_downloads=1 なら同時に走る DL は常に 1 本（従来どおりの直列）。"""
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}

    def fake_dl(url, fmt="mp3", on_progress=None, on_stage=None, cancel=None, **kwargs):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            time.sleep(0.02)  # 重なるなら重なるだけの猶予を与える
        finally:
            with lock:
                live["now"] -= 1
        return [Track(stem=url, filepath=f"{url}.mp3")]

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    fake_infer_factory(monkeypatch)
    fake_write_factory(monkeypatch)

    placeholders = [Track(stem=f"u{i}", url=f"u{i}") for i in range(4)]
    worker = PipelineWorker(placeholders, mode=MODE_FULL, auto_write=True, max_downloads=1)
    run_worker(qtbot, worker, timeout=10000)

    assert live["peak"] == 1


def test_parallel_downloads_keep_every_row(qtbot, monkeypatch):
    """並列でも全行が 1 回ずつ処理され、行の対応（差し替え先）がずれない。"""
    urls = [f"u{i}" for i in range(6)]
    fake_download_factory(
        monkeypatch, {u: [Track(stem=u, filepath=f"{u}.mp3")] for u in urls}
    )
    fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    placeholders = [Track(stem=u, url=u) for u in urls]
    worker = PipelineWorker(placeholders, mode=MODE_FULL, auto_write=True, max_downloads=3)
    pairs = []
    worker.signals.tracks_ready.connect(lambda ph, ts: pairs.append((ph.url, ts[0].stem)))
    run_worker(qtbot, worker, timeout=10000)

    # プレースホルダと実 Track の対応が入れ替わっていないこと
    assert sorted(pairs) == [(u, u) for u in urls]
    assert sorted(t.stem for t in written) == urls


# ---------------------------------------------------------------------------
# DL と推定の並走（_InferStage）
# ---------------------------------------------------------------------------


def test_infer_starts_before_all_downloads_finish(qtbot, monkeypatch):
    """バッチ数ぶん DL が終わった時点で、残りの DL と並行して推定が始まる。

    3 件目の DL を「1 バッチ目の推定が始まるまで」待たせ、イベント列で
    推定が最後の DL より先に走ったことを確かめる（直列なら推定は全 DL の
    後になり、順序の assert で落ちる）。
    """
    events = []  # list.append は GIL 下で不可分。スレッド間の記録用
    infer_started = threading.Event()

    def fake_dl(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        if url == "u2":
            infer_started.wait(timeout=5)
        events.append(f"dl:{url}")
        return [Track(stem=url, filepath=f"{url}.mp3")]

    def fake_infer(tracks, client=None, batch_size=5, force=False):
        events.append("infer")
        infer_started.set()
        for t in tracks:
            t.guessed_title = "song"
            t.valid = True
            t.status = Status.PENDING

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    monkeypatch.setattr(core, "infer_titles", fake_infer)
    fake_write_factory(monkeypatch)

    placeholders = [Track(stem=u, url=u) for u in ("u0", "u1", "u2", "u3")]
    # DL 同士の並列は別テストの担当。ここは DL 段と推定段の並走だけを見たいので
    # max_downloads=1 で DL を直列に固定する（並列だと dl:u3 が推定より先に
    # 走れてしまい、順序の assert が意味を失う）
    worker = PipelineWorker(
        placeholders, mode=MODE_FULL, auto_write=True, batch_size=2, max_downloads=1
    )
    run_worker(qtbot, worker, timeout=10000)

    assert events.index("infer") < events.index("dl:u3")


def test_downloads_are_fed_to_infer_in_batches(qtbot, monkeypatch):
    """推定はバッチ数ごとに分割して呼ばれ、書き込み集計は最後に 1 回だけ出る。"""
    urls = [f"u{i}" for i in range(5)]
    fake_download_factory(
        monkeypatch, {u: [Track(stem=u, filepath=f"{u}.mp3")] for u in urls}
    )
    calls = fake_infer_factory(monkeypatch)
    written = fake_write_factory(monkeypatch)

    placeholders = [Track(stem=u, url=u) for u in urls]
    worker = PipelineWorker(placeholders, mode=MODE_FULL, auto_write=True, batch_size=2)
    summaries = []
    worker.signals.write_summary.connect(lambda d, s, e: summaries.append((d, s, e)))
    run_worker(qtbot, worker)

    # 2 + 2 + 端数 1 の 3 回に分かれ、全行がちょうど 1 回ずつ処理される
    assert [len(c["tracks"]) for c in calls] == [2, 2, 1]
    assert len(written) == 5
    assert summaries == [(5, 0, 0)]  # バッチごとではなく合算で 1 回


def test_infer_failure_stops_later_batches_but_finishes_downloads(qtbot, monkeypatch):
    """推定が失敗したら以降のバッチは投入しない。DL は最後まで走らせる。

    DL が一番コストの高い段なので、LLM が落ちていても取得ぶんは残す。
    失敗した推定は（DL 完走後に）error シグナルで通知される。
    """
    downloaded = []
    infer_failed = threading.Event()

    def fake_dl(url, fmt="mp3", on_progress=None, cancel=None, **kwargs):
        if url != "u0":
            # 1 件目の推定が失敗するまで待ってから次の行を投入する
            infer_failed.wait(timeout=5)
        downloaded.append(url)
        return [Track(stem=url, filepath=f"{url}.mp3")]

    calls = []

    def fake_infer(tracks, client=None, batch_size=5, force=False):
        calls.append(list(tracks))
        for t in tracks:
            t.status = Status.ERROR
            t.error = "推定失敗"
        infer_failed.set()
        raise CoreError("件数不一致")

    monkeypatch.setattr(core, "download_tracks", fake_dl)
    monkeypatch.setattr(core, "infer_titles", fake_infer)
    written = fake_write_factory(monkeypatch)

    placeholders = [Track(stem=u, url=u) for u in ("u0", "u1", "u2")]
    # DL 順を確定させるため直列に固定する（並列だと downloaded の順が不定）
    worker = PipelineWorker(
        placeholders, mode=MODE_FULL, auto_write=True, batch_size=1, max_downloads=1
    )
    errors = []
    worker.signals.error.connect(errors.append)
    run_worker(qtbot, worker, timeout=10000)

    assert len(calls) == 1  # 2 バッチ目以降は投入しない
    assert downloaded == ["u0", "u1", "u2"]  # DL は完走する
    assert written == []
    assert errors and "件数不一致" in errors[0]


# ---------------------------------------------------------------------------
# YtdlpWorker（本体 + EJS の取得・更新）
# ---------------------------------------------------------------------------


@pytest.fixture
def ytdlp_stub(monkeypatch, tmp_path):
    """通信するところを全部差し替える（本体は最新・EJS は取得できる状態）。"""
    import ytdlp_runtime

    monkeypatch.setattr(core, "YoutubeDL", None)  # まだロードしていない状態
    monkeypatch.setattr(ytdlp_runtime, "installed_version", lambda: "2026.08.19")
    monkeypatch.setattr(ytdlp_runtime, "installed_dir", lambda: tmp_path)
    monkeypatch.setattr(ytdlp_runtime, "latest_release", lambda: ("2026.8.19", "https://x/y.whl"))
    monkeypatch.setattr(ytdlp_runtime, "required_ejs_version", lambda d: "0.8.0")
    monkeypatch.setattr(ytdlp_runtime, "ejs_version", lambda d=None: "0.8.0")
    monkeypatch.setattr(ytdlp_runtime, "install_ejs", lambda target: "0.8.0")
    monkeypatch.setattr(
        ytdlp_runtime, "install", lambda v, u: pytest.fail("本体は最新なので取得しない")
    )
    return monkeypatch


def run_ytdlp_worker(check_only: bool = False):
    """YtdlpWorker を同期実行し、done シグナルの引数を返す。"""
    from gui.workers import YtdlpWorker

    worker = YtdlpWorker(check_only=check_only)
    received = []
    worker.signals.done.connect(lambda ok, message, restart: received.append((ok, message, restart)))
    worker.run()
    return received[-1]


def test_ytdlp_worker_reports_ejs_alongside_version(ytdlp_stub):
    """本体が最新でも EJS を確認し、結果を 1 行にまとめて返す。"""
    ok, message, restart = run_ytdlp_worker()
    assert ok is True
    assert message == "最新です（2026.08.19）／ EJS 0.8.0"
    assert restart is False  # まだ yt_dlp を import していないので再起動は不要


def test_ytdlp_worker_needs_restart_after_load(ytdlp_stub):
    """ロード済みなら再起動が要る（yt_dlp は import 時に EJS の有無を見るため）。"""
    ytdlp_stub.setattr(core, "YoutubeDL", object())
    _, _, restart = run_ytdlp_worker()
    assert restart is True


def test_ytdlp_worker_survives_ejs_failure(ytdlp_stub):
    """EJS の取得に失敗しても更新自体は成功扱い（EJS 無しでも DL はできる）。"""
    import ytdlp_runtime

    def boom(target):
        raise ytdlp_runtime.YtdlpUnavailable("PyPI へ接続できません")

    ytdlp_stub.setattr(ytdlp_runtime, "install_ejs", boom)
    ok, message, _ = run_ytdlp_worker()
    assert ok is True
    assert "EJS の取得に失敗" in message and "PyPI へ接続できません" in message


def test_ytdlp_worker_check_only_flags_missing_ejs(ytdlp_stub):
    """[更新を確認] は本体が最新でも EJS の欠落を見逃さない。"""
    import ytdlp_runtime

    ytdlp_stub.setattr(ytdlp_runtime, "ejs_version", lambda d=None: None)
    ytdlp_stub.setattr(
        ytdlp_runtime, "install_ejs", lambda target: pytest.fail("確認だけで取得しない")
    )
    ok, message, _ = run_ytdlp_worker(check_only=True)
    assert ok is True
    assert "EJS 0.8.0 が未取得" in message
