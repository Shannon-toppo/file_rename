# -*- coding: utf-8 -*-
"""診断モード（selftest.py）のオフラインテスト。

凍結ビルドを端末から確認するための口。ネットワークにも yt-dlp の実体にも
触れないよう、外部に出る 3 箇所（外部ツール検出・yt-dlp のロード・PyPI）を
差し替えて、終了コードと表示の分岐だけを見る。
"""
import pytest

import core
import selftest
import ytdlp_runtime


@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """すべて正常な状態を作る（各テストで壊したいところだけ上書きする）。"""
    monkeypatch.setattr(selftest.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ytdlp_runtime, "installed_version", lambda: "2026.08.19")
    monkeypatch.setattr(ytdlp_runtime, "installed_dir", lambda: tmp_path / "2026.8.19")
    monkeypatch.setattr(ytdlp_runtime, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(ytdlp_runtime, "latest_release", lambda: ("2026.8.19", "https://x/y.whl"))
    monkeypatch.setattr(ytdlp_runtime, "install_ejs", lambda target: "0.8.0")
    monkeypatch.setattr(core, "ensure_ytdlp", lambda: None)
    return monkeypatch


def test_selftest_ok(stub_env, capsys):
    assert selftest.run_selftest(["--selftest"]) == 0
    out = capsys.readouterr().out
    assert "結果: OK" in out
    assert "2026.08.19" in out


def test_selftest_reports_missing_ffmpeg(stub_env, capsys):
    """ffmpeg が無いと変換が失敗するので NG にする（deno は速度低下のみ）。"""
    stub_env.setattr(selftest.shutil, "which", lambda name: None if name == "ffmpeg" else "/usr/bin/deno")
    assert selftest.run_selftest(["--selftest"]) == 1
    assert "結果: NG" in capsys.readouterr().out


def test_selftest_reports_unfetched_ytdlp(stub_env, capsys):
    stub_env.setattr(ytdlp_runtime, "installed_version", lambda: None)
    assert selftest.run_selftest(["--selftest"]) == 1
    out = capsys.readouterr().out
    assert "未取得" in out


def test_selftest_reports_pypi_failure(stub_env, capsys):
    """更新機能の通信経路（凍結ビルドの TLS）が死んでいるケース。"""

    def boom():
        raise ytdlp_runtime.YtdlpUnavailable("証明書を検証できません")

    stub_env.setattr(ytdlp_runtime, "latest_release", boom)
    assert selftest.run_selftest(["--selftest"]) == 1
    assert "証明書を検証できません" in capsys.readouterr().out


def test_selftest_update_installs_when_missing(stub_env, capsys, tmp_path):
    """--update は未取得なら取得する（GUI の [設定] → yt-dlp と同じ処理）。"""
    calls = []
    stub_env.setattr(ytdlp_runtime, "installed_version", lambda: None)
    stub_env.setattr(
        ytdlp_runtime, "install", lambda v, u: calls.append((v, u)) or tmp_path / "2026.8.19"
    )
    selftest.run_selftest(["--selftest", "--update"])
    assert calls == [("2026.8.19", "https://x/y.whl")]
    assert "EJS 取得" in capsys.readouterr().out


def test_selftest_update_skips_when_current(stub_env, capsys):
    """導入済みが最新なら取得しない（"2026.08.19" と "2026.8.19" は同じ版）。"""
    stub_env.setattr(ytdlp_runtime, "install", lambda v, u: pytest.fail("取得してはいけない"))
    selftest.run_selftest(["--selftest", "--update"])
    assert "最新です" in capsys.readouterr().out


def test_selftest_update_fetches_ejs_even_when_ytdlp_is_current(stub_env, capsys):
    """本体が最新でも EJS が欠けていれば取りに行く（あとから足せる導線）。"""
    calls = []
    stub_env.setattr(ytdlp_runtime, "install", lambda v, u: pytest.fail("取得してはいけない"))
    stub_env.setattr(ytdlp_runtime, "install_ejs", lambda target: calls.append(target) or "0.8.0")
    assert selftest.run_selftest(["--selftest", "--update"]) == 0
    assert calls  # EJS だけの取得は走る
    assert "OK（0.8.0）" in capsys.readouterr().out


def test_selftest_reports_failed_ejs_fetch(stub_env, capsys):
    def boom(target):
        raise ytdlp_runtime.YtdlpUnavailable("PyPI へ接続できません")

    stub_env.setattr(ytdlp_runtime, "install_ejs", boom)
    assert selftest.run_selftest(["--selftest", "--update"]) == 1
    assert "PyPI へ接続できません" in capsys.readouterr().out


def test_selftest_skips_url_checks_when_basics_fail(stub_env, capsys):
    """yt-dlp が無い状態で URL 検査へ進んでも意味が無いので飛ばす。"""
    stub_env.setattr(ytdlp_runtime, "installed_version", lambda: None)
    stub_env.setattr(core, "fetch_metadata", lambda url: pytest.fail("呼んではいけない"))
    assert selftest.run_selftest(["--selftest", "https://example.com/watch?v=x"]) == 1
