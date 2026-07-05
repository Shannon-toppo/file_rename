#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI / CLI 共通のコア処理: ダウンロード → タイトル推定 → タグ書き込み。

このモジュールは print しない。進捗・結果はコールバックと Track の状態で
呼び出し元(CLI / GUI ワーカー)へ返す。import した時点で ../mv2title/.env を
環境変数へ読み込む（Config.from_env() が読む前に載せておく必要があるため）。
新しいスクリプトでも env 設定を重複させず、このモジュールを import すること。
"""
import logging
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv
from mutagen.id3 import ID3
from mutagen.id3._frames import TIT2, TPE1
from mutagen.id3._util import ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.wave import WAVE
from mv2title import Config, LLMClient, TitleInput, extract_titles
from yt_dlp import YoutubeDL

_ROOT = Path(__file__).parent.parent

# 接続設定は mv2title 側の .env を共用する
load_dotenv(_ROOT / "mv2title" / ".env")

FILES_DIR = Path(__file__).parent / "files"
SUPPORTED_EXTS = (".mp3", ".wav", ".m4a")
SUPPORTED_FORMATS = ("mp3", "wav", "m4a")
BATCH_SIZE = 5
# YouTube の翻訳メタデータ(タイトル/チャンネル名)の優先言語
METADATA_LANG = "ja"
# 音量ノーマライズ(loudnorm)の既定パラメータ。EBU R128 相当のターゲットを
# 単一パスで適用する(download_tracks(normalize=True) で使用)。
# 基準値(統合ラウドネス I)は設定 / CLI から変更できる。TP / LRA は固定。
NORMALIZE_TARGET_I = -14.0  # 統合ラウドネス (LUFS)。音楽配信の標準的な値
_NORMALIZE_TP = -1.5  # トゥルーピーク (dBTP)
_NORMALIZE_LRA = 11.0  # ラウドネスレンジ (LU)
# 末尾の無音削除(試験的)。areverse で末尾を先頭側へ持ってきて silenceremove を
# 掛け、また元に戻す。-50dB 以下(ほぼ無音)だけを無音とみなし、1 秒は残す
# 保守的な設定(フェードアウトや余韻を音楽本体ごと削らないため)。閾値は固定。
TRIM_SILENCE_FILTER = (
    "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=1,areverse"
)


def loudnorm_filter(target_i: float = NORMALIZE_TARGET_I) -> str:
    """基準値 target_i (LUFS) を使った loudnorm の ffmpeg フィルタ文字列を作る。"""
    return f"loudnorm=I={target_i:g}:TP={_NORMALIZE_TP:g}:LRA={_NORMALIZE_LRA:g}"


class CancelledError(Exception):
    """ユーザー操作によるキャンセル。"""


class CoreError(Exception):
    """パイプラインの継続不能なエラー（件数不一致など）。"""


class Status(Enum):
    """Track の状態。value は GUI の状態列にそのまま表示する。"""

    QUEUED = "キュー"
    DOWNLOADING = "DL中"
    INFERRING = "推定中"
    PENDING = "確認待ち"
    WRITING = "書き込み中"
    DONE = "完了"
    ERROR = "エラー"


@dataclass
class Track:
    """テーブルの 1 行 = 処理対象の 1 曲。

    Attributes:
        stem: 推定の入力に使うファイル名（拡張子なし）。
        url: ダウンロード元 URL（ローカルファイル追加なら None）。
        filepath: 音声ファイルのパス（DL 完了後 or ローカル追加時に設定）。
        channel: チャンネル名。アーティスト名のヒントとして推定に渡す。
        guessed_title: 推定（または手動入力）された曲名。
        artist: アーティスト欄に書き込む値。推定はしない（手動入力または
            チャンネル名のコピー）。空文字なら書き込まない。
        valid: mv2title の検証結果。未推定なら None。
        manual: True なら guessed_title は手動編集済み（再推定で上書きしない）。
        status: 現在の処理段階。
        error: エラー・スキップ理由（正常時は空文字）。
    """

    stem: str
    url: str | None = None
    filepath: Path | None = None
    channel: str | None = None
    guessed_title: str = ""
    artist: str = ""
    valid: bool | None = None
    manual: bool = False
    status: Status = Status.QUEUED
    error: str = ""


def track_from_file(path: Path) -> Track:
    """ローカルの音声ファイルから Track を作る（DL 段はスキップ）。"""
    return Track(stem=path.stem, filepath=path)


def list_music_files(directory: Path = FILES_DIR) -> list[Path]:
    """ディレクトリ直下の対応音声ファイルを列挙する。"""
    files: list[Path] = []
    for ext in SUPPORTED_EXTS:
        files.extend(directory.glob(f"*{ext}"))
    return sorted(files)


def make_client() -> LLMClient:
    """mv2title/.env の設定で LLMClient を作る。"""
    return LLMClient(Config.from_env())


def check_connection(timeout: float = 3.0) -> tuple[bool, str]:
    """LLM エンドポイントの疎通を確認する（補完呼び出しはしない軽量チェック）。

    OpenAI 互換の GET {base_url}/models を短い timeout で叩く。
    LLM の推論を伴わないため、サーバの生死確認としては十分軽い。

    Returns:
        (成功可否, 人間向けメッセージ)。例外は投げず、失敗理由を文字列で返す。
    """
    try:
        config = Config.from_env()
    except ValueError as e:
        # BASE_URL 未設定
        return False, str(e)
    url = config.base_url.rstrip("/") + "/models"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.api_key or 'not-needed'}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if 200 <= status < 300:
                return True, f"接続 OK: {config.base_url}"
            return False, f"エンドポイントがエラーを返しました (HTTP {status})"
    except Exception as e:
        return False, f"接続できません ({config.base_url}): {e}"


# ---------------------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------------------

# (ファイル名, 進捗% [0-100], 再生リスト内の番号, リスト全体数) を受け取る
# 進捗コールバック。単一動画では番号・全体数は None。
DownloadProgress = Callable[[str, float, "int | None", "int | None"], None]


def _fetch_localized_title(
    video_id: str, lang: str = METADATA_LANG, timeout: float = 5.0
) -> str | None:
    """YouTube の watch 画面(innertube next API)から表示言語 lang のタイトルを取る。

    yt-dlp が参照する player API の videoDetails.title はロケール非依存で、
    投稿者が翻訳タイトルを用意していても常に既定言語を返す。一方ブラウザの
    動画見出しは next API 由来で、hl に応じて翻訳される
    (実測: VDdLF1YubI0 は player=英語 / next=日本語)。

    構造変更や通信失敗など、どんな理由でも失敗したら None を返す
    (呼び出し元は yt-dlp のタイトルへフォールバックする)。
    """
    payload = json.dumps(
        {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20250101.00.00",
                    "hl": lang,
                }
            },
            "videoId": video_id,
        }
    ).encode()
    req = urllib.request.Request(
        "https://www.youtube.com/youtubei/v1/next",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return None
    return _find_primary_title(data)


def _find_primary_title(node) -> str | None:
    """next API 応答から videoPrimaryInfoRenderer.title のテキストを探す。

    応答構造は YouTube 側の変更で変わり得るため、キー位置を決め打ちせず
    再帰的に探索する(見つからなければ None)。
    """
    if isinstance(node, dict):
        renderer = node.get("videoPrimaryInfoRenderer")
        if isinstance(renderer, dict):
            title = renderer.get("title") or {}
            runs = title.get("runs") or []
            text = "".join(r.get("text", "") for r in runs) or title.get("simpleText")
            if text:
                return text
        for value in node.values():
            found = _find_primary_title(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_primary_title(value)
            if found:
                return found
    return None


def download_tracks(
    url: str,
    fmt: str = "mp3",
    on_progress: DownloadProgress | None = None,
    cancel: threading.Event | None = None,
    out_dir: Path | None = None,
    expand_playlist: bool = False,
    normalize: bool = True,
    loudness: float = NORMALIZE_TARGET_I,
    trim_silence: bool = False,
    logger: logging.Logger | None = None,
) -> list[Track]:
    """URL の音声を指定形式でダウンロードし、Track のリストを返す。

    再生リスト URL は含まれる各動画を 1 Track ずつ返す。
    動画＋リスト混在 URL（watch?v=...&list=...）は既定では動画 1 本のみ
    （noplaylist=True）。expand_playlist=True にするとリスト全体を展開する。
    チャンネル名が取得できれば Track.channel に載せる。
    out_dir を指定すると FILES_DIR の代わりにそこへ保存する（GUI の設定用）。
    normalize=True（既定）だと ffmpeg の loudnorm フィルタで音量を揃える
    （基準値は loudness で変更可。loudnorm_filter 参照）。trim_silence=True だと
    末尾の無音区間を削除する（試験的。TRIM_SILENCE_FILTER 参照）。どちらも
    ffmpeg の再エンコード時に適用される。
    logger を渡すと yt-dlp の出力を stdout ではなくその Python ロガーへ流す
    （quiet=True 併用で logging 経由へ完全に切り替える。GUI のログパネル用）。
    None なら現状どおり yt-dlp が直接コンソールへ出力する（CLI 用）。

    Raises:
        CancelledError: cancel がセットされた場合（DL 途中で中断）。
        CoreError: 情報取得に失敗、または 1 件もダウンロードできなかった場合。
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported format: {fmt}")
    dest = out_dir if out_dir is not None else FILES_DIR
    dest.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest / "%(title)s [%(id)s].%(ext)s")

    def hook(d: dict) -> None:
        # yt-dlp のフックから例外を投げると当該エントリの DL が中断される
        if cancel is not None and cancel.is_set():
            raise CancelledError("ダウンロードがキャンセルされました。")
        if on_progress is not None and d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                name = Path(d.get("filename", "")).name
                # 再生リスト中なら「何番目 / 全体数」を info_dict から拾う
                info = d.get("info_dict") or {}
                on_progress(
                    name,
                    d.get("downloaded_bytes", 0) / total * 100,
                    info.get("playlist_index"),
                    info.get("n_entries"),
                )

    opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": not expand_playlist,
        "ignoreerrors": True,  # 一部の動画が失敗してもリスト全体を止めない
        "progress_hooks": [hook],
        # YouTube は既定で英語のメタデータを返すため、投稿者が英訳を用意して
        # いる動画では英語のタイトル/チャンネル名になってしまう。翻訳メタデータ
        # の優先言語を日本語に指定する（日本語版が無ければ原語のまま）。
        # タイトルはファイル名(= 推定の入力)にも使われるためここで効く。
        "extractor_args": {"youtube": {"lang": [METADATA_LANG]}},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": fmt,
            }
        ],
    }
    filters = []
    if trim_silence:
        # 無音を除いた本体でラウドネスを測れるよう、loudnorm より前段に置く
        filters.append(TRIM_SILENCE_FILTER)
    if normalize:
        filters.append(loudnorm_filter(loudness))
    if filters:
        # ffmpeg 音声フィルタとして FFmpegExtractAudio へ渡す。フラットな list は
        # 全 ffmpeg 系ポストプロセッサに適用される（ここでは抽出のみ）。
        # 二重掛けを避けるため、両方 OFF のときは付けない。
        opts["postprocessor_args"] = ["-af", ",".join(filters)]
    if logger is not None:
        # yt-dlp の出力を logging 経由へ切り替える（quiet=True で stdout を止め、
        # logger へ渡した Python ロガーに info/warning/error/debug を流す）
        opts["logger"] = logger
        opts["quiet"] = True

    tracks: list[Track] = []
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # ignoreerrors=True では CancelledError も entry 単位で握り潰されるため、
        # 抜けた直後に必ず再確認する
        if cancel is not None and cancel.is_set():
            raise CancelledError("ダウンロードがキャンセルされました。")
        if not info:
            raise CoreError("情報を取得できませんでした（URL を確認してください）。")

        # 再生リストなら entries を、単一動画ならそれ自身を対象にする
        entries = info["entries"] if "entries" in info else [info]
        for entry in entries:
            if not entry:
                # ignoreerrors により失敗した項目は None になる
                continue
            # ダウンロード前の拡張子のままのパスが返るため、変換後の拡張子に差し替える
            path = Path(ydl.prepare_filename(entry)).with_suffix(f".{fmt}")
            if not path.exists():
                continue
            # 推定の入力(stem)には、可能なら watch 画面の日本語タイトルを使う。
            # yt-dlp のタイトル(= ファイル名)は player API 由来で翻訳されない
            # ため、翻訳付き動画では英語のままになる(_fetch_localized_title 参照)。
            video_id = entry.get("id")
            localized = _fetch_localized_title(video_id) if video_id else None
            tracks.append(
                Track(
                    stem=localized or path.stem,
                    url=entry.get("webpage_url") or url,
                    filepath=path,
                    channel=entry.get("channel") or entry.get("uploader"),
                )
            )

    if not tracks:
        raise CoreError("ダウンロードした音声ファイルが見つかりません。")
    return tracks


# ---------------------------------------------------------------------------
# タイトル推定
# ---------------------------------------------------------------------------


def infer_titles(
    tracks: Sequence[Track],
    client: LLMClient | None = None,
    batch_size: int = BATCH_SIZE,
    force: bool = False,
) -> None:
    """各 Track の曲名を mv2title で推定し、guessed_title / valid を更新する。

    mv2title はバッチ設計のため、対象をまとめて 1 回で呼ぶ（1 リクエスト N 件）。
    manual=True の行は保護してスキップする（force=True で明示的に上書き）。
    成功した行は Status.PENDING になる（書き込みは write_tags で行う）。

    Raises:
        CoreError: 応答件数が対象件数と一致しない場合（全対象行を ERROR にした上で）。
        その他: LLM 接続エラー等はそのまま伝播する（呼び出し元で処理）。
    """
    targets = [t for t in tracks if force or not t.manual]
    if not targets:
        return
    for t in targets:
        t.status = Status.INFERRING
        t.error = ""

    inputs = [TitleInput(t.stem, channel=t.channel) for t in targets]
    if client is None:
        client = make_client()
    try:
        results = extract_titles(inputs, client, batch_size=batch_size, bypass_check=True)
    except Exception as e:
        for t in targets:
            t.status = Status.ERROR
            t.error = f"タイトル推定に失敗しました: {e}"
        raise

    # extract_titles は入力と同数・同順で返す契約だが、誤マッチはファイルを
    # 壊すため、念のため件数を確認してから位置で対応付ける。
    if len(results) != len(targets):
        msg = (
            f"応答件数({len(results)})が対象件数({len(targets)})と一致しません。"
            "誤対応を避けるため中断しました。"
        )
        for t in targets:
            t.status = Status.ERROR
            t.error = msg
        raise CoreError(msg)

    for t, res in zip(targets, results):
        t.guessed_title = res.title
        t.valid = res.valid
        t.manual = False
        t.status = Status.PENDING


# ---------------------------------------------------------------------------
# タグ書き込み
# ---------------------------------------------------------------------------


def write_title(filepath: Path, title: str, artist: str | None = None) -> None:
    """ファイル形式に応じたタイトル（と任意でアーティスト）タグを書き込む。

    タイトルは .mp3 / .wav が ID3 の TIT2 フレーム、.m4a が MP4 の \xa9nam
    アトム。アーティストは TPE1 / \xa9ART（None・空文字なら書き込まない）。
    """
    ext = filepath.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        tags.save(str(filepath))
    elif ext == ".wav":
        audio = WAVE(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        audio.save(str(filepath))
    elif ext == ".m4a":
        audio = MP4(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        audio.tags["\xa9nam"] = [title]
        if artist:
            audio.tags["\xa9ART"] = [artist]
        audio.save()
    else:
        raise ValueError(f"unsupported extension: {ext}")


def write_tags(
    tracks: Sequence[Track],
    on_result: Callable[[Track], None] | None = None,
) -> None:
    """各 Track の guessed_title をメタデータへ書き込む。

    スキップ方針（CLI / GUI 共通のポリシーをここに集約）:
    - guessed_title が空 → PENDING のまま（error に理由）
    - valid=False かつ手動編集されていない → PENDING のまま（error に理由）。
      手動編集済み(manual=True)ならユーザーの意思なので書き込む。
    - 書き込み失敗 → ERROR / 成功 → DONE

    1 行の失敗は他の行を止めない。on_result は各行の処理直後に呼ばれる。
    """
    for t in tracks:
        if t.filepath is None:
            t.status = Status.ERROR
            t.error = "ファイルパスが未設定です。"
        elif not t.guessed_title:
            t.status = Status.PENDING
            t.error = "曲名が空のためスキップしました。"
        elif t.valid is False and not t.manual:
            t.status = Status.PENDING
            t.error = "検証失敗（元タイトルに含まれない曲名）のためスキップしました。"
        else:
            t.status = Status.WRITING
            try:
                write_title(t.filepath, t.guessed_title, artist=t.artist or None)
            except Exception as e:
                t.status = Status.ERROR
                t.error = f"書き込みに失敗しました: {e}"
            else:
                t.status = Status.DONE
                t.error = ""
        if on_result is not None:
            on_result(t)


def describe_result(track: Track) -> str:
    """write_tags 後の Track を CLI 表示用の 1 行に整形する（print はしない）。"""
    name = track.filepath.name if track.filepath else track.stem
    if track.status is Status.DONE:
        return f"  [OK] {name}  ->  {track.guessed_title}"
    if track.status is Status.ERROR:
        return f"  [ERR] {name}  ->  {track.error}"
    return f"  [SKIP] {name}  ->  {track.error}"
