#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""凍結ビルドの動作確認（`FileRenameGUI --selftest [URL]`）。

なぜ必要か
==========
yt-dlp は exe / .app に同梱せず実行時に取得する（ytdlp_runtime 参照）ため、
「配布物の中で外部の yt-dlp をロードできるか」はビルドしてみないと分からない。
GUI は console=False なので、そのままではエラーも版数も画面に出ない。
ここを通せば端末から凍結ビルドを起動して、取得先・外部ツール・TLS 通信・
yt-dlp のロードまでを一息に確認できる。

macOS では `FileRenameGUI.app/Contents/MacOS/FileRenameGUI --selftest` を
端末から実行する（.app でも標準出力は端末に出る）。Windows は GUI
サブシステムのため端末には出ないので `FileRenameGUI.exe --selftest > log.txt`
のようにリダイレクトする。

URL を渡すと、ダウンロードはせずにメタデータ取得（core.fetch_metadata）まで
試す。`--download` も付けると一時フォルダへ実際に 1 曲落として消す
（yt-dlp → deno → ffmpeg の連携まで通っているかの確認）。

`--update` は GUI の [設定] → yt-dlp と同じ取得・更新（本体 + EJS）を
端末から行う。
GUI が出せない状況（起動できない・ダイアログが見えない）での復旧手段。
"""
import platform
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

import core
import ytdlp_runtime


# ラベル欄の桁数（全角を 2 桁として数える）
_LABEL_WIDTH = 20


def _display_width(text: str) -> int:
    """端末での表示幅（全角は 2 桁）。f-string の桁揃えは文字数で数えるため自前で測る。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _line(label: str, value: str) -> None:
    padding = " " * max(0, _LABEL_WIDTH - _display_width(label))
    print(f"  {label}{padding}: {value}")


def _check_tools() -> bool:
    """ffmpeg / deno の在り処を出す。ffmpeg が無ければ False（変換が失敗するため）。"""
    ffmpeg = shutil.which("ffmpeg")
    deno = shutil.which("deno")
    _line("ffmpeg", ffmpeg or "見つかりません（mp3/wav 変換に必要）")
    _line("deno", deno or "見つかりません（DL 速度が大幅に低下）")
    return ffmpeg is not None


def _check_ytdlp() -> bool:
    """展開済み yt-dlp をロードして版を出す。ロードできなければ False。"""
    _line("yt-dlp 保存先", str(ytdlp_runtime.runtime_root()))
    installed = ytdlp_runtime.installed_version()
    if installed is None:
        _line("yt-dlp", "未取得（GUI の [設定] → yt-dlp から取得できます）")
        return False
    try:
        core.ensure_ytdlp()
    except core.CoreError as e:
        _line("yt-dlp", f"NG: ロードできません: {e}")
        return False
    _line("yt-dlp", f"{installed}（ロード成功）")
    return True


def _check_ejs() -> bool:
    """yt-dlp から EJS が見えているかを、実際の import で確かめる。

    YouTube の署名・n チャレンジを解くスクリプト。無いと yt-dlp は実行の
    たびに GitHub / npm を取りに行こうとし（既定では禁止）、解けないまま
    速度が落ちる。ロード済みの yt-dlp が見る経路そのもので判定したいので、
    ディレクトリではなく import できるかを見る（開発環境の venv でも同じ）。
    """
    try:
        import yt_dlp_ejs
    except ImportError:
        _line("EJS", "未取得（--update で取得できます。YouTube の制限解除に必要）")
        return False
    _line("EJS", f"{yt_dlp_ejs.version}（ロード成功）")
    return True


def _update_ytdlp() -> bool:
    """未取得なら取得、古ければ更新する（GUI の [設定] → yt-dlp と同じ）。"""
    current = ytdlp_runtime.installed_version()
    try:
        latest, url = ytdlp_runtime.latest_release()
        if current is not None and ytdlp_runtime.parse_version(
            current
        ) >= ytdlp_runtime.parse_version(latest):
            _line("yt-dlp 取得", f"最新です（{current}）")
            return _update_ejs(ytdlp_runtime.installed_dir())
        path = ytdlp_runtime.install(latest, url)
    except ytdlp_runtime.YtdlpUnavailable as e:
        _line("yt-dlp 取得", f"NG: {e}")
        return False
    _line("yt-dlp 取得", f"OK（{latest} を {path} へ展開）")
    return _update_ejs(path)


def _update_ejs(target) -> bool:
    """yt-dlp に対応する EJS（yt-dlp-ejs）を用意する。"""
    if target is None:
        target = ytdlp_runtime.installed_dir()
    try:
        version = ytdlp_runtime.install_ejs(target)
    except ytdlp_runtime.YtdlpUnavailable as e:
        _line("EJS 取得", f"NG: {e}")
        return False
    _line("EJS 取得", f"OK（{version}）")
    return True


def _check_pypi() -> bool:
    """PyPI へ HTTPS で問い合わせて最新版を出す（更新機能の通信経路の確認）。

    凍結ビルドでは TLS の CA が解決できずにここだけ失敗することがあるため、
    取得は行わずに読み取りだけで疎通を確かめる。
    """
    try:
        latest, _ = ytdlp_runtime.latest_release()
    except ytdlp_runtime.YtdlpUnavailable as e:
        _line("PyPI 疎通", f"NG: {e}")
        return False
    _line("PyPI 疎通", f"OK（最新版 {latest}）")
    return True


def _check_url(url: str) -> bool:
    """URL のメタデータ取得だけを試す（ダウンロードはしない）。"""
    try:
        tracks = core.fetch_metadata(url)
    except Exception as e:  # noqa: BLE001 - 失敗の内容を出して続ける
        _line("URL 情報取得", f"NG: {e}")
        return False
    _line("URL 情報取得", f"OK（{len(tracks)} 件）: {tracks[0].stem}")
    return True


def _check_download(url: str) -> bool:
    """一時フォルダへ実際にダウンロードして消す（変換まで通るかの確認）。

    凍結ビルドから起動した ffmpeg / deno が正しく動くかは、実際に走らせない
    と分からない（特に mac は .app からの子プロセス起動が絡む）。音量
    ノーマライズは時間がかかるだけなので切る。
    """
    with tempfile.TemporaryDirectory(prefix="frg-selftest-") as tmp:
        out = Path(tmp)
        try:
            tracks = core.download_tracks(url, out_dir=out, normalize=False)
        except Exception as e:  # noqa: BLE001 - 失敗の内容を出して続ける
            _line("ダウンロード", f"NG: {e}")
            return False
        files = [t.filepath for t in tracks if t.filepath is not None]
        if not files:
            _line("ダウンロード", "NG: ファイルが作られませんでした")
            return False
        size = files[0].stat().st_size
        _line("ダウンロード", f"OK（{files[0].name} / {size / 1_000_000:.1f} MB）")
    return True


def run_selftest(argv: list[str]) -> int:
    """診断を実行して終了コードを返す（0 = すべて OK）。

    Args:
        argv: `--selftest` を含む引数列。オプション以外の最初の値を URL、
            `--update` で yt-dlp の取得・更新、`--download` で実ダウンロード
            まで行う。
    """
    urls = [a for a in argv if not a.startswith("-")]
    print("FileRenameGUI 診断")
    _line("実行形態", ".app / exe（凍結）" if getattr(sys, "frozen", False) else "開発（venv）")
    _line("プラットフォーム", f"{sys.platform} / {platform.machine()}")
    _line("Python", platform.python_version())
    _line("アプリの隣", str(core.app_dir()))
    env_file = core.find_env_file()
    _line(".env", str(env_file) if env_file else "見つかりません（[設定] の入力でも可）")
    _line("保存先 files/", str(core.FILES_DIR))

    results = []
    if "--update" in argv:
        results.append(_update_ytdlp())
    results += [_check_tools(), _check_ytdlp()]
    if results[-1]:  # yt-dlp をロードできて初めて EJS の有無を見られる
        results.append(_check_ejs())
    results.append(_check_pypi())
    if urls and all(results):
        results.append(_check_url(urls[0]))
        if "--download" in argv:
            results.append(_check_download(urls[0]))

    ok = all(results)
    print("結果: OK" if ok else "結果: NG（上の行を確認してください）")
    return 0 if ok else 1
