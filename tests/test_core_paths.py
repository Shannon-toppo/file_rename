# -*- coding: utf-8 -*-
"""凍結（PyInstaller）対応のパス・環境変数まわりのオフラインテスト。

app_dir / find_env_file / env_defaults / apply_env_overrides /
_augment_path_darwin を、frozen 状態や .env を monkeypatch で擬似して検証する。
"""
import os
import sys
from pathlib import Path

import pytest

import core


@pytest.fixture
def env_guard(monkeypatch):
    """テスト中の ENV_KEYS の変更を確実に元へ戻す。

    monkeypatch.setenv / delenv は最初に触った時点の値を記録して復元するため、
    先に全キーへ触れておけばテスト内（テスト対象コード含む）の変更が漏れない。
    """
    for key in core.ENV_KEYS:
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# app_dir / find_env_file
# ---------------------------------------------------------------------------


def test_app_dir_not_frozen_is_source_dir():
    assert core.app_dir() == Path(core.__file__).parent


def test_app_dir_frozen_windows(monkeypatch, tmp_path):
    exe = tmp_path / "FileRenameGUI" / "FileRenameGUI.exe"
    exe.parent.mkdir()
    monkeypatch.setattr(core, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert core.app_dir() == exe.parent


def test_app_dir_frozen_mac_app_bundle(monkeypatch, tmp_path):
    """mac の .app 内で動いている場合は .app を置いたフォルダを返す。"""
    exe = tmp_path / "FileRenameGUI.app" / "Contents" / "MacOS" / "FileRenameGUI"
    exe.parent.mkdir(parents=True)
    monkeypatch.setattr(core, "_IS_FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert core.app_dir() == tmp_path


def test_find_env_file_prefers_app_dir(monkeypatch, tmp_path):
    """探索順: app_dir()/.env → ../mv2title/.env。"""
    app = tmp_path / "app"
    root = tmp_path / "root"
    (root / "mv2title").mkdir(parents=True)
    app.mkdir()
    monkeypatch.setattr(core, "app_dir", lambda: app)
    monkeypatch.setattr(core, "_ROOT", root)

    assert core.find_env_file() is None  # どちらにも無い

    dev_env = root / "mv2title" / ".env"
    dev_env.write_text("BASE_URL=dev\n", encoding="utf-8")
    assert core.find_env_file() == dev_env  # 開発時フォールバック

    app_env = app / ".env"
    app_env.write_text("BASE_URL=app\n", encoding="utf-8")
    assert core.find_env_file() == app_env  # exe 隣が最優先


# ---------------------------------------------------------------------------
# env_defaults / apply_env_overrides
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_env_file(monkeypatch, tmp_path):
    """BASE_URL / MODEL を持つ .env を find_env_file の結果として差し込む。"""
    env = tmp_path / ".env"
    env.write_text("BASE_URL=http://from-dotenv/v1\nMODEL=dotenv-model\n", encoding="utf-8")
    monkeypatch.setattr(core, "find_env_file", lambda: env)
    monkeypatch.setattr(core, "_PROCESS_ENV", {})
    return env


def test_env_defaults_reads_dotenv(fake_env_file):
    defaults = core.env_defaults()
    assert defaults == {"BASE_URL": "http://from-dotenv/v1", "MODEL": "dotenv-model"}


def test_env_defaults_process_env_wins(fake_env_file, monkeypatch):
    """プロセス環境変数は .env より優先（load_dotenv の従来挙動と同じ）。"""
    monkeypatch.setattr(core, "_PROCESS_ENV", {"MODEL": "proc-model"})
    assert core.env_defaults()["MODEL"] == "proc-model"


def test_apply_env_overrides_override_wins(fake_env_file, env_guard):
    core.apply_env_overrides({"BASE_URL": "http://override/v1"})
    assert os.environ["BASE_URL"] == "http://override/v1"
    assert os.environ["MODEL"] == "dotenv-model"  # 上書きしないキーは .env の値


def test_apply_env_overrides_empty_restores_dotenv(fake_env_file, env_guard):
    """空の上書きは .env の値へ戻す（設定画面で欄を空にした場合）。"""
    core.apply_env_overrides({"BASE_URL": "http://override/v1"})
    core.apply_env_overrides({"BASE_URL": ""})
    assert os.environ["BASE_URL"] == "http://from-dotenv/v1"


def test_apply_env_overrides_removes_unset_keys(fake_env_file, env_guard):
    """.env にも上書きにも無いキーは環境変数から外す。"""
    os.environ["API_KEY"] = "stale"
    core.apply_env_overrides({})
    assert "API_KEY" not in os.environ


# ---------------------------------------------------------------------------
# _augment_path_darwin
# ---------------------------------------------------------------------------


def test_augment_path_darwin_adds_brew_paths_once(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/usr/bin")
    core._augment_path_darwin()
    core._augment_path_darwin()  # 冪等: 2 回呼んでも増えない
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts.count("/opt/homebrew/bin") == 1
    assert parts.count("/usr/local/bin") == 1
    assert parts[0] == "/usr/bin"  # 既存 PATH を先頭に保つ


def test_augment_path_darwin_noop_on_other_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PATH", "/usr/bin")
    core._augment_path_darwin()
    assert os.environ["PATH"] == "/usr/bin"
