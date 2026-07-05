# -*- coding: utf-8 -*-
"""PipelineWorker のオフラインテスト。

core.download_tracks / infer_titles / write_tags を monkeypatch した
フェイクへ差し替え、qtbot.waitSignal でパイプライン完走を検証する。
LLM・yt-dlp・ネットワークは使わない。
"""
import threading

import pytest
from PySide6.QtCore import QThreadPool

import core
from core import CoreError, Status, Track
from gui.workers import MODE_FULL, MODE_INFER, PipelineWorker


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
        cancel=None,
        out_dir=None,
        expand_playlist=False,
        normalize=True,
        logger=None,
    ):
        captured["out_dir"] = out_dir
        captured["expand_playlist"] = expand_playlist
        captured["normalize"] = normalize
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
    )
    run_worker(qtbot, worker)
    assert captured == {
        "out_dir": tmp_path,
        "batch_size": 7,
        "expand_playlist": True,
        "normalize": False,
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


def test_reinfer_without_force_protects_manual(qtbot, monkeypatch):
    calls = fake_infer_factory(monkeypatch)
    manual = Track(stem="m", guessed_title="keep", manual=True, status=Status.PENDING)
    # MODE_INFER でも force=False なら manual 行は対象外
    worker = PipelineWorker([manual], mode=MODE_INFER, force=False)
    run_worker(qtbot, worker)

    # ワーカーの対象選定で manual 行が除外され、infer は呼ばれない
    assert calls == []
    assert manual.guessed_title == "keep"
