#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI / CLI 共通のコア処理: ダウンロード → タイトル推定 → タグ書き込み。

このモジュールは print しない。進捗・結果はコールバックと Track の状態で
呼び出し元(CLI / GUI ワーカー)へ返す。import した時点で接続設定の .env
（find_env_file 参照。開発時は ../mv2title/.env）を環境変数へ読み込む
（Config.from_env() が読む前に載せておく必要があるため）。
新しいスクリプトでも env 設定を重複させず、このモジュールを import すること。
"""
import logging
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from mutagen.id3 import ID3
from mutagen.id3._frames import TALB, TIT2, TPE1
from mutagen.id3._util import ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus
from mutagen.wave import WAVE
from mv2title import (
    Config,
    ConnectionCheckError,
    LLMClient,
    TitleInput,
    check_endpoint,
    extract_titles,
    model_aliases,
)
from mv2title import ModelMismatchError as LibModelMismatchError
from mv2title import make_client as _lib_make_client
from mv2title.connect import DEFAULT_MODEL  # noqa: F401 - MODEL 未設定時の実効値（GUI の表示用に re-export）

import ytdlp_runtime
from ytdlp_runtime import YtdlpUnavailable  # noqa: F401 - 呼び出し元の except 用に re-export

_ROOT = Path(__file__).parent.parent

# GUI のログパネルは "core" / "mv2title" / "yt_dlp" ロガーを購読する
# （gui/logpanel.attach_handler 参照）。CLI では未設定なので何も出ない。
_LOG = logging.getLogger(__name__)

# PyInstaller で凍結された exe / .app として動いているか
_IS_FROZEN = bool(getattr(sys, "frozen", False))

# 接続設定の環境変数キー（.env / 設定ダイアログの上書き対象）
ENV_KEYS = ("BASE_URL", "API_KEY", "MODEL", "SYSTEM_PROMPT")

# .env より優先されるプロセス環境変数（load_dotenv が上書きしない従来挙動を
# apply_env_overrides でも保つため、.env を読む前にスナップショットする）
_PROCESS_ENV = {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def app_dir() -> Path:
    """アプリの基準ディレクトリ（.env や既定の files/ を置く場所）を返す。

    凍結時は実行ファイルのあるフォルダ。macOS の .app バンドル内
    （Foo.app/Contents/MacOS/exe）で動いている場合は .app を置いたフォルダ
    （= ユーザーから見た「アプリの隣」）。開発時はこのファイルのフォルダ。
    """
    if not _IS_FROZEN:
        return Path(__file__).parent
    exe = Path(sys.executable).resolve()
    if (
        sys.platform == "darwin"
        and exe.parent.name == "MacOS"
        and exe.parent.parent.name == "Contents"
    ):
        return exe.parents[3]  # MacOS → Contents → Foo.app → その親
    return exe.parent


def find_env_file() -> Path | None:
    """接続設定 .env を探す。① app_dir()/.env → ② ../mv2title/.env（開発時）。

    凍結配布では exe（mac は .app）の隣に .env を置く運用。開発時は従来どおり
    mv2title 側の .env を共用する。見つからなければ None。
    """
    candidates = (app_dir() / ".env", _ROOT / "mv2title" / ".env")
    return next((p for p in candidates if p.is_file()), None)


def env_defaults() -> dict[str, str]:
    """上書き前の既定値（.env / プロセス環境変数由来）を返す（設定画面の表示用）。"""
    env_file = find_env_file()
    values = dict(dotenv_values(env_file)) if env_file is not None else {}
    defaults = {k: v for k, v in values.items() if k in ENV_KEYS and v}
    defaults.update(_PROCESS_ENV)  # プロセス環境変数は .env より優先（従来挙動）
    return defaults


def apply_env_overrides(overrides: dict[str, str]) -> None:
    """設定ダイアログの接続設定上書きを os.environ へ反映する（GUI 用）。

    優先度: 上書き値 > プロセス環境変数 > .env。空の上書きはキー自体を
    既定値へ戻す（既定も無ければ環境変数から外す）ため、設定画面で欄を
    空にすれば .env の値に復帰する。Config.from_env() 内の load_dotenv は
    既存の環境変数を上書きしないので、ここで載せた値がそのまま使われる。
    """
    defaults = env_defaults()
    for key in ENV_KEYS:
        value = overrides.get(key) or defaults.get(key)
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


# macOS で Finder 起動のアプリに補う PATH。brew（Apple Silicon / Intel）と、
# deno 公式インストーラの既定の置き場所（~/.deno/bin）。deno は yt-dlp が
# 使う JS ランタイムで、無いと DL 速度が 1/10 以下になる。
_DARWIN_EXTRA_PATHS = ("/opt/homebrew/bin", "/usr/local/bin", "~/.deno/bin")


def _augment_path_darwin() -> None:
    """macOS: Finder 起動のアプリへ Homebrew / deno の PATH を補う。

    Finder から起動した GUI アプリはログインシェルの PATH を継承しないため、
    brew で入れた ffmpeg や deno が見つからない。未含有のときだけ末尾へ
    追加する（冪等）。実在しないディレクトリが混ざっても PATH 解決では
    単に無視されるため、存在確認はしない。
    """
    if sys.platform != "darwin":
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    expanded = [os.path.expanduser(p) for p in _DARWIN_EXTRA_PATHS]
    extra = [p for p in expanded if p not in current]
    if extra:
        os.environ["PATH"] = os.pathsep.join([p for p in current if p] + extra)


_augment_path_darwin()

# 接続設定は mv2title 側の .env を共用する（凍結時は exe / .app 隣の .env）
_ENV_FILE = find_env_file()
if _ENV_FILE is not None:
    load_dotenv(_ENV_FILE)

FILES_DIR = app_dir() / "files"
SUPPORTED_EXTS = (".mp3", ".wav", ".m4a", ".opus")
SUPPORTED_FORMATS = ("mp3", "wav", "m4a", "opus")
BATCH_SIZE = 5
# タイトル推定で構造化出力（OpenAI 互換の response_format=json_schema）を使うか。
# 既定は True（スキーマで縛るほうが解析は安定する）。ただしサーバ／モデルに
# よっては制約付きデコード下で応答が配列の 1 件目だけになり（実測: LM Studio +
# gemma-4-e2b）、毎回 1 バッチ目を捨てて部分リトライで拾い直すぶんの往復が
# 増える（曲名自体は mv2title のフォールバックが回収する）。その組み合わせでは
# False にする
# （infer_titles(use_schema=False) → extract_titles へそのまま渡る）。
USE_SCHEMA = True
# URL 行を同時に何本ダウンロードするか（GUI の設定で変更可）。yt-dlp は
# 1 回の呼び出しの中では「受信 → ffmpeg 変換 → 次」を直列に回すので、行を
# またいで並べないと変換中は回線が空く。増やしすぎると YouTube 側の制限
# （429）を踏みやすく帯域も分割されるため、控えめな既定にしている。
MAX_DOWNLOADS = 2
# YouTube の翻訳メタデータ(タイトル/チャンネル名)の優先言語
METADATA_LANG = "ja"
# YouTube Music のホスト名。ここから来た URL は配信元がメタデータとして
# 曲名を持っているため、LLM による推定を挟まずそのまま採用できる
# （is_youtube_music / download_tracks(ytmusic_direct=True) 参照）。
YTMUSIC_HOSTS = ("music.youtube.com",)
# YouTube Music の innertube クライアント（曲名の取得に使う。
# _fetch_ytmusic_song 参照）。バージョンは形式さえ合っていれば通る。
_YTMUSIC_CLIENT = "WEB_REMIX"
_YTMUSIC_CLIENT_VERSION = "1.20250101.01.00"
# アーティスト名の run に付く遷移先の種別（_byline_artist 参照）
_YTMUSIC_ARTIST_PAGE = "MUSIC_PAGE_TYPE_ARTIST"
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


# 変換（再エンコード）時のビットレート指定。download_tracks(audio_bitrate=...)、
# CLI の --bitrate、設定ダイアログの「変換ビットレート」で使う。
# None = 指定なし。yt-dlp は preferredquality を渡さないと ffmpeg の既定
# （音声は 64kbps/ch = ステレオ 128kbps）に任せるため、YouTube の opus 約
# 129kbps を 128kbps へ落として再圧縮することになる。
# BITRATE_SOURCE = 取得した音源と同じ値（動画ごとに変わる。source_bitrate_kbps
# / _source_bitrate_pp_class 参照）。整数 = その kbps 固定。
BITRATE_SOURCE = "source"
BITRATE_CHOICES = (128, 192, 256, 320)  # 設定ダイアログに並べる固定値 (kbps)
_BITRATE_MIN = 8
_BITRATE_MAX = 512
# best_quality=True のときに yt-dlp のフォーマット並べ替えへ差し込む優先順。
# 既定の並びは配信側が申告する quality を先に見るので、音声コーデックの質 →
# ビットレート → サンプリングレートの順に選び直させる。acodec を先頭に置くのは
# 意図的で、実測では YouTube に opus 128.9kbps と aac 129.5kbps が並ぶため、
# ビットレートだけで選ぶと数字がわずかに大きい aac（音質は劣る）を掴む。
BEST_AUDIO_SORT = ("acodec", "abr", "asr")

# 取得する音声の既定指定。yt-dlp は「取得したコーデック == 出力形式」のときだけ
# -acodec copy（= 再エンコードなし）を選ぶ。
DEFAULT_FORMAT_SELECTOR = "bestaudio/best"
# 出力形式ごとの「そのまま保存できる」音声コーデック。YouTube の acodec は
# opus が "opus"、AAC が "mp4a.40.2" のような文字列で返る。
NATIVE_CODECS = {"opus": ("opus",), "m4a": ("mp4a", "aac")}
# 上のコーデックがあるならそれを選ぶ format 指定（無ければ bestaudio に落ちる）。
# フィルタ（ノーマライズ / 無音削除）を掛けるときは再エンコードが避けられない
# ので使わない ＝ その場合は素直に一番音質の良い音源から変換する。
_COPY_FORMAT_SELECTOR = {
    "opus": "bestaudio[acodec=opus]/bestaudio/best",
    "m4a": "bestaudio[acodec^=mp4a]/bestaudio/best",
}
# -ar に渡してよいサンプリングレート（対応外を渡すと ffmpeg が変換に失敗する）。
# 記載の無いエンコーダ（aac / PCM）は制限なしとみなす。libopus は必ず 48kHz へ
# 変換するので指定しない — 44.1kHz の AAC 音源から opus を作るときに
# -ar 44100 を付けると "Conversion failed" になる（実測）。
_ENCODER_SAMPLE_RATES: dict[str, tuple[int, ...]] = {
    "libopus": (),
    "libmp3lame": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
}


def is_native_codec(fmt: str, acodec: str | None) -> bool:
    """取得した音声 acodec が、出力形式 fmt のまま（無変換で）保存できるか。"""
    prefixes = NATIVE_CODECS.get(fmt)
    if not prefixes or not acodec or acodec == "none":
        return False
    return acodec.lower().startswith(prefixes)


def parse_bitrate(value: object) -> int | str | None:
    """ビットレート指定（CLI 引数 / 設定値）を正規化する。

    受け付ける値は None・""・"default"（= 指定なし）、"source"（= 取得元と
    同じ）、"192" / "192k" / 192（= その kbps に固定）。

    Raises:
        ValueError: 解釈できない値、または範囲外の kbps。
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "default", "auto"):
            return None
        if text == BITRATE_SOURCE:
            return BITRATE_SOURCE
        text = text.removesuffix("bps").removesuffix("k")
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"ビットレートの指定が不正です: {value}") from None
    kbps = int(value)
    if not _BITRATE_MIN <= kbps <= _BITRATE_MAX:
        raise ValueError(
            f"ビットレートは {_BITRATE_MIN}〜{_BITRATE_MAX} kbps で指定してください: {kbps}"
        )
    return kbps


def source_bitrate_kbps(info: dict) -> int | None:
    """yt-dlp の情報 dict から、取得した音源のビットレート (kbps) を読む。

    abr（音声だけのビットレート）が無ければ tbr（全体）で代用する。
    yt-dlp の _quality_args は 10 以下を VBR の品質スケールとして解釈するため、
    それ以下や欠損は None を返して ffmpeg の既定に任せる。
    """
    for key in ("abr", "tbr"):
        try:
            kbps = round(float(info.get(key)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if kbps > 10:
            return kbps
    return None


class CancelledError(Exception):
    """ユーザー操作によるキャンセル。"""


class CoreError(Exception):
    """パイプラインの継続不能なエラー（件数不一致など）。"""


class ModelMismatchError(CoreError):
    """指定したモデルとは別のモデルが応答した（サーバー側の差し替え）。

    検出自体は mv2title（ModelCheckedClient）が行う。infer_titles が
    mv2title.ModelMismatchError を捕まえてこちらへ包み直すのは、CoreError で
    なければ行が ERROR にならないため（と、GUI 向けの案内を足すため）。
    """


class Status(Enum):
    """Track の状態。value は GUI の状態列にそのまま表示する。"""

    QUEUED = "キュー"
    FETCHING = "情報取得中"
    DOWNLOADING = "DL中"
    CONVERTING = "変換中"  # 受信後の ffmpeg 変換（ノーマライズ / 無音切り詰め含む）
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
        artist: アーティスト欄（作者）に書き込む値。推定はしない（手動入力・
            チャンネル名のコピー・既存タグの読み込み）。空文字なら書き込まない。
        album: アルバム名に書き込む値。推定はしない（手動入力または既存タグの
            読み込み）。空文字なら書き込まない。
        valid: mv2title の検証結果。未推定なら None。
        manual: True なら guessed_title は手動編集済み（再推定で上書きしない）。
        skip_infer: True ならタイトル推定を行わず、取得済みのメタデータ上の
            タイトルをそのまま曲名として使う（YouTube Music 用。
            use_metadata_title 参照）。manual と同じく再推定から保護される。
        status: 現在の処理段階。
        error: エラー・スキップ理由（正常時は空文字）。
    """

    stem: str
    url: str | None = None
    filepath: Path | None = None
    channel: str | None = None
    guessed_title: str = ""
    artist: str = ""
    album: str = ""
    valid: bool | None = None
    manual: bool = False
    skip_infer: bool = False
    status: Status = Status.QUEUED
    error: str = ""


# タグ名 → 各形式のキー。曲名 / 作者(アーティスト) / アルバム名の 3 項目だけ扱う
# （write_title が書き込む項目と対になる）。
_ID3_TAG_KEYS = {"title": "TIT2", "artist": "TPE1", "album": "TALB"}
_MP4_TAG_KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb"}
# Ogg Opus は Vorbis コメント（キー名そのまま。大文字小文字は区別されない）
_OGG_TAG_KEYS = {"title": "title", "artist": "artist", "album": "album"}
# read_tags が返すキー（呼び出し元の参照用）
TAG_FIELDS = ("title", "artist", "album")


def _first_text(value) -> str:
    """タグの値（ID3 フレーム / MP4 のリスト / 素の文字列）を 1 行の文字列にする。"""
    if value is None:
        return ""
    text = getattr(value, "text", value)  # ID3 フレームは .text がリスト
    if isinstance(text, (list, tuple)):
        text = text[0] if text else ""
    return str(text).strip()


def read_tags(filepath: Path) -> dict[str, str]:
    """音声ファイルから曲名 / 作者 / アルバム名を読み取る（best effort）。

    「できるだけ読む」ためのヘルパなので **例外を投げない**: タグ無し・
    壊れたファイル・未対応拡張子はすべて空文字の辞書として返す（取り込み時に
    1 ファイルの不備でリスト全体が止まらないようにするため）。
    戻り値のキーは TAG_FIELDS。
    """
    tags = None
    keys = _ID3_TAG_KEYS
    ext = filepath.suffix.lower()
    try:
        if ext == ".mp3":
            try:
                tags = ID3(str(filepath))
            except ID3NoHeaderError:
                tags = None
        elif ext == ".wav":
            tags = WAVE(str(filepath)).tags
        elif ext == ".m4a":
            tags = MP4(str(filepath)).tags
            keys = _MP4_TAG_KEYS
        elif ext == ".opus":
            tags = OggOpus(str(filepath)).tags
            keys = _OGG_TAG_KEYS
    except Exception as e:  # 壊れたファイル等。読めないだけなので握って空を返す
        _LOG.debug("タグを読めませんでした: %s (%s)", filepath, e)
        tags = None
    if tags is None:
        return dict.fromkeys(TAG_FIELDS, "")
    result = {}
    for name in TAG_FIELDS:
        try:
            result[name] = _first_text(tags.get(keys[name]))
        except Exception:  # 個別フレームの破損も他の項目を巻き込まない
            result[name] = ""
    return result


def track_from_file(path: Path, read_metadata: bool = True) -> Track:
    """ローカルの音声ファイルから Track を作る（DL 段はスキップ）。

    read_metadata=True なら既存のタグ（曲名 / 作者 / アルバム名）を読み込んで
    初期値にする。曲名が既に入っているファイルは skip_infer=True / PENDING に
    して推定から保護する（YouTube Music 行と同じ扱い。use_metadata_title 参照）
    ——取り込み直後に見えている曲名が [実行] で勝手に置き換わらないようにする
    ためで、推定し直したい場合は「選択行を再推定」または行のクリアで戻せる。
    """
    track = Track(stem=path.stem, filepath=path)
    if not read_metadata:
        return track
    tags = read_tags(path)
    track.artist = tags["artist"]
    track.album = tags["album"]
    if tags["title"]:
        track.guessed_title = tags["title"]
        track.skip_infer = True
        track.valid = True
        track.status = Status.PENDING
    return track


def list_music_files(directory: Path = FILES_DIR) -> list[Path]:
    """ディレクトリ直下の対応音声ファイルを列挙する。"""
    files: list[Path] = []
    for ext in SUPPORTED_EXTS:
        files.extend(directory.glob(f"*{ext}"))
    return sorted(files)


def read_url_list(path: Path) -> list[str]:
    """テキストファイルから URL を 1 行ずつ読み込む（空行と # 始まりの行は無視）。

    CLI（download.py -a）と GUI（リスト読込・.txt ドロップ）で共用する。
    """
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    urls = [line.strip() for line in lines]
    return [u for u in urls if u and not u.startswith("#")]


def make_client() -> LLMClient:
    """接続設定（.env / GUI の上書き済み環境変数）で LLMClient を作る。

    実体は mv2title の make_client。MODEL をサーバーの /models にある完全な id
    へ解決し、指定と違うモデルが応答したら止めるクライアントを返す。LM Studio は
    一覧の id と完全一致しない名前を受けると、エラーにせずロード中の別モデルで
    黙って答えるため（実測と詳細は mv2title.connect の docstring を参照）。
    """
    return _lib_make_client(Config.from_env(), timeout=3.0)


def check_connection(timeout: float = 3.0) -> tuple[bool, str]:
    """LLM エンドポイントの疎通を確認する（補完呼び出しはしない軽量チェック）。

    OpenAI 互換の GET {base_url}/models を短い timeout で叩くだけ
    （mv2title.check_endpoint）。LLM の推論を伴わないため、サーバの生死確認
    としては十分軽い。文言の組み立てはこちらの責務（ライブラリは素材だけ返す）。

    Returns:
        (成功可否, 人間向けメッセージ)。例外は投げず、失敗理由を文字列で返す。
    """
    try:
        config = Config.from_env()
    except ValueError as e:
        # BASE_URL 未設定
        return False, str(e)
    try:
        ids, resolved = check_endpoint(config, timeout)
    except ConnectionCheckError as e:
        return False, str(e)
    # 使用するモデル名がサーバーの一覧に無ければ注意を添える（MODEL 未設定の
    # まま既定値になっている事故などに気付けるように）。LM Studio はその場合
    # ロード中の別モデルで答えようとし、推論は make_client が返す
    # ModelCheckedClient が止める。
    # 一覧に無くても通るサーバーはあり得るので NG（接続失敗）にはしない
    model = config.model or ""
    if ids and not any(model_aliases(model) & model_aliases(i) for i in ids):
        return True, (
            f"接続 OK: {config.base_url}（注意: モデル '{model}' は"
            "サーバーのモデル一覧にありません。[設定] の MODEL を一覧にある名前にしてください）"
        )
    if resolved != model:
        return True, f"接続 OK: {config.base_url}（モデル: {model} → {resolved}）"
    return True, f"接続 OK: {config.base_url}"


# ---------------------------------------------------------------------------
# ダウンロード
# ---------------------------------------------------------------------------

# (ファイル名, 進捗% [0-100], 再生リスト内の番号, リスト全体数) を受け取る
# 進捗コールバック。単一動画では番号・全体数は None。
DownloadProgress = Callable[[str, float, "int | None", "int | None"], None]

# 受信完了後の段（ffmpeg 変換・タイトル取得）へ移ったことを知らせるコールバック。
# 進捗率が取れない段なので、状態そのもの（Status）を渡して表示を切り替えさせる。
DownloadStage = Callable[["Status"], None]

# yt-dlp は exe に同梱しない（YouTube の仕様変更で数か月ごとに使えなくなるため、
# 実体はユーザー領域に置いて GUI から更新する。ytdlp_runtime 参照）。
# import は sys.path を整えたあとでないと解決できないので ensure_ytdlp() で遅延させる。
# テストが monkeypatch.setattr(core, "YoutubeDL", FakeYDL) で差し替えられるよう、
# モジュール属性として持つ（非 None なら ensure_ytdlp() は素通りする）。
YoutubeDL = None

# ensure_ytdlp() の初回ロードを直列化する（並列ダウンロード時に複数スレッドが
# 同時に sys.path 操作と import を行うのを防ぐ）。
_YTDLP_LOCK = threading.Lock()

# yt-dlp のフック内から投げるキャンセル例外（CancelledError と yt-dlp の
# DownloadCancelled の両方を継承する）。ensure_ytdlp() でロード後に作る。
_YDL_CANCELLED: type[BaseException] | None = None

# 音声抽出の postprocessor クラス（yt-dlp の FFmpegExtractAudioPP を継承する。
# _extract_audio_pp_class() で初回に作る）。
_EXTRACT_AUDIO_PP: type | None = None


def _cancelled(message: str) -> BaseException:
    """yt-dlp のフックから投げるキャンセル例外を作る。

    素の CancelledError では駄目 — yt-dlp の _handle_extraction_exceptions は
    ignoreerrors=True のとき「予期しない例外」を report_error で握り潰し、
    **再生リストの次のエントリへ進んでしまう**（停止ボタンを押しても最後まで
    走り続ける）。yt-dlp が中断として特別扱いするのは DownloadCancelled だけで、
    これだけは握り潰さず再送出されるため、その場でリスト全体が止まる。
    呼び出し元は従来どおり CancelledError として捕捉できる。
    yt-dlp 未ロード（テストの代役など）では素の CancelledError に落とす。
    """
    cls = _YDL_CANCELLED or CancelledError
    return cls(message)


def ensure_ytdlp() -> None:
    """yt-dlp をロードして core.YoutubeDL に載せる（済んでいれば何もしない）。

    差し替え済み（テストのフェイク）なら sys.path にも import にも触れない。

    Raises:
        CoreError: yt-dlp が未取得で、ロードできなかった場合。
    """
    global YoutubeDL
    if YoutubeDL is not None:
        return
    with _YTDLP_LOCK:
        if YoutubeDL is not None:
            return  # 待っている間に別スレッドがロードし終えていた
        try:
            ytdlp_runtime.load()
            from yt_dlp import YoutubeDL as _YoutubeDL
        except YtdlpUnavailable as e:
            raise CoreError(str(e)) from e
        except ImportError as e:  # load() が通ったのに import できないのは壊れた展開
            raise CoreError(f"yt-dlp を読み込めませんでした: {e}") from e
        _load_cancel_exception()
        YoutubeDL = _YoutubeDL


def _load_cancel_exception() -> None:
    """yt-dlp の DownloadCancelled を継承したキャンセル例外を用意する。

    ロードできない古い yt-dlp では None のままにし、_cancelled() が素の
    CancelledError に落ちる（従来どおり単一動画では止まる）。
    """
    global _YDL_CANCELLED
    if _YDL_CANCELLED is not None:
        return
    try:
        from yt_dlp.utils import DownloadCancelled
    except ImportError:
        return
    _YDL_CANCELLED = type("_YdlCancelled", (CancelledError, DownloadCancelled), {})


def _extract_audio_pp_class() -> type | None:
    """音声抽出 postprocessor（FFmpegExtractAudioPP の派生）を返す。

    標準の PP に対して 2 点を足す。どちらも「変換する動画ごとの実測値」が
    要るため、生成時に 1 つしか値を持てない yt-dlp のオプションでは書けない
    （再生リストではエントリごとに値が変わる）。run() へ渡ってくる情報 dict を
    見てから ffmpeg 引数を組む。

    - サンプリングレートを取得元に合わせる（-ar）。loudnorm は内部で 192kHz へ
      アップサンプリングし、その値が出力の交渉結果にそのまま出る（実測: 48kHz の
      opus から m4a なら 96kHz、wav なら 192kHz。mp3 だけは規格上 48kHz が上限
      なので露見しない）。容量が倍以上になるだけで音質は上がらないので戻す。
    - match_source_bitrate=True なら、ビットレートを取得元に合わせる
      （source_bitrate_kbps。10 より大きい値は yt-dlp 側で "-b:a <値>k" になる。
      _quality_args 参照）。

    yt-dlp を import できない場合（テストの代役など）は None を返し、呼び出し元は
    従来どおり opts の postprocessors 指定（= 標準の PP）に落ちる。
    """
    global _EXTRACT_AUDIO_PP
    if _EXTRACT_AUDIO_PP is not None:
        return _EXTRACT_AUDIO_PP
    try:
        from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP
    except ImportError:
        return None

    class ExtractAudioPP(FFmpegExtractAudioPP):  # type: ignore[misc,valid-type]
        """変換直前に、その音源の実測値から ffmpeg 引数を決める抽出 PP。

        クラス名から取られる pp_key は "ExtractAudio"（標準の PP と同じ）なので、
        postprocessor_args を {"extractaudio": [...]} で渡せばこの PP だけに
        届く（download_tracks 参照）。
        """

        def __init__(
            self,
            *args,
            match_source_bitrate: bool = False,
            force_encode: bool = False,
            **kwargs,
        ):
            super().__init__(*args, **kwargs)
            self._match_source_bitrate = match_source_bitrate
            self._force_encode = force_encode
            self._source_asr: int | None = None

        def get_audio_codec(self, path):
            codec = super().get_audio_codec(path)
            if self._force_encode and codec is not None:
                # 親の run は「取得したコーデック == 出力形式」なら -acodec copy を
                # 選ぶが、copy と -af（フィルタ）は同居できず ffmpeg が失敗する。
                # 一致しない値を返して必ず再エンコード側の分岐へ行かせる
                # （filecodec は分岐の比較にしか使われない）。
                return f"{codec}+filtered"
            return codec

        def run(self, information):
            # yt-dlp のメタクラスは run を postprocessor_hooks 送出でくるむため、
            # ここと親の run で started/finished が 2 回ずつ流れる。購読側
            # （GUI の on_stage → Status.CONVERTING）は同じ状態を入れ直すだけ
            # なので実害はない。
            try:
                self._source_asr = int(information["asr"])
            except (KeyError, TypeError, ValueError):
                self._source_asr = None
            if self._match_source_bitrate:
                kbps = source_bitrate_kbps(information)
                if kbps is not None:
                    self._preferredquality = kbps
            return super().run(information)

        def run_ffmpeg(self, path, out_path, codec, more_opts):
            # フィルタ側の内部レートが出力へ漏れないよう、取得元へ固定する
            # （codec="copy" は再エンコードしないので触らない。エンコーダが
            # 対応しないレートは指定しない — _ENCODER_SAMPLE_RATES 参照）
            rates = _ENCODER_SAMPLE_RATES.get(codec)
            if (
                self._source_asr
                and codec != "copy"
                and (rates is None or self._source_asr in rates)
            ):
                more_opts = [*more_opts, "-ar", str(self._source_asr)]
            return super().run_ffmpeg(path, out_path, codec, more_opts)

    _EXTRACT_AUDIO_PP = ExtractAudioPP
    return _EXTRACT_AUDIO_PP


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


def _fetch_ytmusic_song(
    video_id: str, lang: str = METADATA_LANG, timeout: float = 5.0
) -> tuple[str | None, str | None]:
    """YouTube Music が表示している曲名とアーティスト名を innertube から取る。

    YouTube Music は動画タイトルとは別に「曲名」を持っている。例えば
    06YWg6Y1kxo の YouTube 上のタイトルは "MIMI『 Pale 』feat. 初音ミク"
    だが、YouTube Music 上の曲名は "Pale"、アーティストは "MIMI" である。
    これらは yt-dlp が参照する player / tab のメタデータには現れない
    （entry["track"] は「この動画の音楽」欄がある動画にしか入らず、実測では
    多くの動画で None）ため、YouTube Music のクライアント(WEB_REMIX)として
    直接問い合わせる。

    構造変更や通信失敗など、どんな理由でも失敗したら (None, None) を返す
    （呼び出し元は entry["track"] や動画タイトルへフォールバックする）。
    """
    payload = json.dumps(
        {
            "context": {
                "client": {
                    "clientName": _YTMUSIC_CLIENT,
                    "clientVersion": _YTMUSIC_CLIENT_VERSION,
                    "hl": lang,
                }
            },
            "videoId": video_id,
        }
    ).encode()
    req = urllib.request.Request(
        "https://music.youtube.com/youtubei/v1/next",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return (None, None)
    return _find_ytmusic_song(data, video_id)


def _find_ytmusic_song(node, video_id: str) -> tuple[str | None, str | None]:
    """next API 応答から、再生キュー内の当該動画の曲名とアーティスト名を探す。

    曲名は playlistPanelVideoRenderer.title に入る。videoId 指定の呼び出し
    では通常 1 件だけ返るが、別の曲の行を拾わないよう videoId で照合する
    （応答構造は YouTube 側の変更で変わり得るのでキー位置は決め打ちしない）。
    アーティスト名は longBylineText の中から拾う（_byline_artist 参照）。
    """
    renderers: list[dict] = []
    _collect_renderers(node, "playlistPanelVideoRenderer", renderers)
    for renderer in renderers:
        if renderer.get("videoId") not in (None, video_id):
            continue
        title = renderer.get("title") or {}
        runs = title.get("runs") or []
        text = "".join(r.get("text", "") for r in runs) or title.get("simpleText")
        if text:
            return (text, _byline_artist(renderer.get("longBylineText")))
    return (None, None)


def _byline_artist(byline) -> str | None:
    """longBylineText の runs からアーティスト名だけを取り出す。

    byline は "MIMI • 393万回視聴 • 高評価 6.5万 件"（MV）や
    "MIMI • Pale • 2020年"（アルバム収録曲）のように、アーティスト・
    アルバム・再生回数が中黒で連なる。テキストを分割すると曲によって
    構成が変わって当てにならないので、run に付いている遷移先の種別
    （pageType = MUSIC_PAGE_TYPE_ARTIST）でアーティストの run だけを選ぶ。
    複数アーティストは ", " で連結する。見つからなければ None。
    """
    runs = (byline or {}).get("runs") or []
    names = []
    for run in runs:
        endpoint = (run.get("navigationEndpoint") or {}).get("browseEndpoint") or {}
        configs = endpoint.get("browseEndpointContextSupportedConfigs") or {}
        music = configs.get("browseEndpointContextMusicConfig") or {}
        if music.get("pageType") == _YTMUSIC_ARTIST_PAGE:
            name = (run.get("text") or "").strip()
            if name:
                names.append(name)
    return ", ".join(names) or None


def _collect_renderers(node, key: str, found: list[dict]) -> None:
    """応答ツリーから key という名前の renderer dict を再帰的に集める。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                found.append(v)
            _collect_renderers(v, key, found)
    elif isinstance(node, list):
        for v in node:
            _collect_renderers(v, key, found)


def _hostname(url: str) -> str:
    """URL のホスト名を小文字で返す（スキーム無しの貼り付けにも対応）。

    "music.youtube.com/playlist?list=..." のようにスキームを省いた URL は
    urlsplit がホストではなくパスとして解釈するため、"//" を補って解析する
    （yt-dlp 自体はスキーム無しでも受け付けるので、判定側だけ落ちるのを防ぐ）。
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme and not parts.netloc:
        parts = urllib.parse.urlsplit("//" + url)
    return (parts.hostname or "").lower().lstrip(".")


def is_youtube_music(*urls: str | None) -> bool:
    """渡された URL のいずれかが YouTube Music のものか判定する。

    YouTube Music の配信データは曲名・アーティスト名を独立したメタデータ
    として持っているため、動画タイトルから曲名を推測する必要がない
    （download_tracks / fetch_metadata の ytmusic_direct）。

    複数取るのは、判定できる URL が抽出のどこに残るか一定しないため。
    yt-dlp は entry の webpage_url を www.youtube.com へ正規化するので、
    呼び出し元から渡した URL・再生リスト側の original_url / webpage_url・
    エントリ側の元 URL を順に見る（どれか 1 つでも music.youtube.com なら
    その抽出は YouTube Music 由来）。
    """
    return any(_hostname(u) in YTMUSIC_HOSTS for u in urls if u)


def use_metadata_title(
    track: Track, title: str | None = None, artist: str | None = None
) -> None:
    """推定を挟まず、取得済みのタイトルをそのまま曲名として採用する。

    title を渡さなければ stem（＝動画タイトル）をそのまま使う。配信元が
    付けた曲名なので検証(valid)は行わず True 扱いにし、書き込み待ち
    (PENDING) にする。skip_infer=True により以降の推定からは保護される
    （manual は立てない — ユーザーの手動編集と区別するため）。
    artist を渡すとアーティスト欄も埋める（既に入っている行は上書きしない
    — 手動入力やチャンネル名コピーの結果を消さないため）。
    """
    track.guessed_title = (title or track.stem).strip()
    if artist and not track.artist:
        track.artist = artist.strip()
    track.skip_infer = True
    track.valid = True
    track.status = Status.PENDING


# yt-dlp が失敗を報告したときの文言。ignoreerrors=True では extract_info が
# 例外を投げず None（または欠けた entries）を返すだけなので、拾っておかないと
# 「なぜ失敗したか」が呼び出し元に一切残らず、URL は正しいのに「URL を確認して
# ください」と出てしまう（例: YouTube が Premium 限定に指定した動画）。
# yt-dlp の出力自体はロガーを渡した GUI のログパネルにしか出ないため、
# ここで拾って CoreError の文言＝GUI の行に出る文言に載せる。
GENERIC_EXTRACT_ERROR = "情報を取得できませんでした（URL を確認してください）。"


def _record_ydl_errors(ydl) -> list[str]:
    """ydl.report_error を差し替え、報告されたエラー文言を溜めるリストを返す。

    report_error は ignoreerrors で握り潰される経路も含めて必ず通るため、
    ここが理由を拾える唯一の場所になる（logger オプションは CLI では未設定で、
    設定すると yt-dlp の進捗表示が壊れるので使えない）。report_error を持たない
    実装（テストの代役など）では何もせず空リストを返す。
    """
    errors: list[str] = []
    original = getattr(ydl, "report_error", None)
    if original is None:
        return errors

    def report_error(message, *args, **kwargs):
        errors.append(str(message))
        return original(message, *args, **kwargs)

    ydl.report_error = report_error
    return errors


def _ydl_error_message(errors: Sequence[str], fallback: str) -> str:
    """溜めたエラー文言を 1 行にまとめる。1 件も無ければ fallback を返す。"""
    seen: list[str] = []
    for raw in errors:
        # 表の 1 セルに収めるため改行を潰す（yt-dlp は対処法を改行で足す）
        msg = " ".join(str(raw).split())
        # report_error には接頭辞なしで渡るが、念のため落としておく
        if msg.startswith("ERROR:"):
            msg = msg[len("ERROR:"):].strip()
        if msg and msg not in seen:
            seen.append(msg)
    return " / ".join(seen) if seen else fallback


def download_tracks(
    url: str,
    fmt: str = "mp3",
    on_progress: DownloadProgress | None = None,
    on_stage: DownloadStage | None = None,
    cancel: threading.Event | None = None,
    out_dir: Path | None = None,
    expand_playlist: bool = False,
    normalize: bool = True,
    loudness: float = NORMALIZE_TARGET_I,
    trim_silence: bool = False,
    best_quality: bool = False,
    audio_bitrate: int | str | None = None,
    ytmusic_direct: bool = True,
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
    best_quality=True だと取得するフォーマットを音質優先で選び直す
    （BEST_AUDIO_SORT 参照。既定は yt-dlp の bestaudio 任せ）。
    audio_bitrate は再エンコード時のビットレート: None（既定）なら ffmpeg の
    既定値（ステレオ 128kbps 相当）、"source" なら取得した音源と同じ値、
    整数なら その kbps 固定（parse_bitrate 参照）。wav は非圧縮なので無視し、
    opus は指定なしのとき "source" 扱いにする（libopus の既定 96kbps は音源より
    低いため）。
    サンプリングレートは指定によらず取得元と同じ値に固定する
    （_extract_audio_pp_class 参照）。
    normalize / trim_silence をどちらも切っている場合は、出力形式と同じ
    コーデックの音源（opus 出力なら opus、m4a なら AAC）を優先して取得し、
    yt-dlp に再エンコードなしで保存させる（_COPY_FORMAT_SELECTOR）。その音源が
    無い動画では通常どおり最良の音源から変換する（ログに 1 行残す）。
    フィルタを掛ける場合は再エンコードが必須なので、この優先は行わない。
    ytmusic_direct=True（既定）だと、YouTube Music の URL はタイトル推定を
    行わず、YouTube Music 上の曲名とアーティスト名をそのまま採用する
    （_fetch_ytmusic_song / use_metadata_title）。
    on_stage は受信完了後の段（Status.CONVERTING = ffmpeg 変換、
    Status.FETCHING = 日本語タイトル取得）へ移ったときに呼ばれる。on_progress は
    バイト受信中しか呼ばれないため、これが無いと変換とタイトル取得の間ずっと
    進捗が最後の % のまま止まって見える（変換はノーマライズ・無音切り詰めを
    含む全編の再エンコードで、無音切り詰め ON なら areverse の分だけさらに重い）。
    logger を渡すと yt-dlp の出力を stdout ではなくその Python ロガーへ流す
    （quiet=True 併用で logging 経由へ完全に切り替える。GUI のログパネル用）。
    None なら現状どおり yt-dlp が直接コンソールへ出力する（CLI 用）。

    Raises:
        CancelledError: cancel がセットされた場合（DL 途中で中断）。
        CoreError: 情報取得に失敗、または 1 件もダウンロードできなかった場合。
            文言には yt-dlp が報告した理由をそのまま載せる
            （_record_ydl_errors / _ydl_error_message 参照）。
    """
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported format: {fmt}")
    bitrate = parse_bitrate(audio_bitrate)
    if fmt == "wav":
        bitrate = None  # wav は非圧縮 (PCM) なのでビットレート指定は効かない
    elif fmt == "opus" and bitrate is None:
        # libopus の既定は約 96kbps で、YouTube の約 129kbps の音源を再エンコード
        # すると黙って音質が落ちる（実測 127kbps → 94kbps）。指定が無ければ
        # 取得元に合わせる（mp3 / AAC は既定 128kbps で音源とほぼ同じなので
        # そのまま ffmpeg 任せにする）。
        bitrate = BITRATE_SOURCE
    ensure_ytdlp()
    dest = out_dir if out_dir is not None else FILES_DIR
    dest.mkdir(parents=True, exist_ok=True)
    outtmpl = str(dest / "%(title)s [%(id)s].%(ext)s")

    def hook(d: dict) -> None:
        # yt-dlp のフックから例外を投げると当該エントリの DL が中断される。
        # 再生リストの残りまで止めるには DownloadCancelled 系である必要がある
        # （_cancelled 参照。素の CancelledError は ignoreerrors に食われる）
        if cancel is not None and cancel.is_set():
            raise _cancelled("ダウンロードがキャンセルされました。")
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

    def pp_hook(d: dict) -> None:
        # 受信が終わってポストプロセッサ（音声抽出 = ffmpeg 変換、ファイル移動）
        # に入ったことを通知する。ここでは進捗率が取れないので状態だけ。
        # 進捗フック(hook)と違い例外は投げない — 変換中に中断するとファイルが
        # 中途半端に残るため、キャンセルは現在のファイルを書き終えてから効かせる。
        if on_stage is not None and d.get("status") == "started":
            on_stage(Status.CONVERTING)

    def cancel_filter(info: dict, *, incomplete: bool = False) -> str | None:
        # 各エントリの処理に入る前に呼ばれる。進捗フックはバイトを受け取り
        # 始めてからしか鳴らないため（ライブや HLS では最初の 1 個目が来る
        # まで数十秒かかる）、ここで先に止める。DownloadCancelled を投げると
        # 残りのエントリごと中断されることが match_filter の仕様に明記されている。
        if cancel is not None and cancel.is_set():
            raise _cancelled("ダウンロードがキャンセルされました。")
        return None

    filters = []
    if trim_silence:
        # 無音を除いた本体でラウドネスを測れるよう、loudnorm より前段に置く
        filters.append(TRIM_SILENCE_FILTER)
    if normalize:
        filters.append(loudnorm_filter(loudness))
    # フィルタを掛けるなら再エンコードは避けられないので、素直に一番良い音源を
    # 取る。掛けないなら出力形式と同じコーデックの音源を優先し、yt-dlp に
    # -acodec copy（無変換）を選ばせる（opus 出力 + opus 音源など）。
    selector = DEFAULT_FORMAT_SELECTOR
    if not filters:
        selector = _COPY_FORMAT_SELECTOR.get(fmt, DEFAULT_FORMAT_SELECTOR)
    extract_audio: dict = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if isinstance(bitrate, int):
        # 10 より大きい値は yt-dlp 側で "-b:a <値>k" になる（_quality_args 参照）
        extract_audio["preferredquality"] = str(bitrate)
    # 音声抽出は自前の派生 PP で行う（動画ごとの実測値が要るため。
    # _extract_audio_pp_class 参照）。ロードできない環境では opts 指定に落とす。
    pp_class = _extract_audio_pp_class()
    opts = {
        "format": selector,
        "outtmpl": outtmpl,
        "noplaylist": not expand_playlist,
        "ignoreerrors": True,  # 一部の動画が失敗してもリスト全体を止めない
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        # YouTube は既定で英語のメタデータを返すため、投稿者が英訳を用意して
        # いる動画では英語のタイトル/チャンネル名になってしまう。翻訳メタデータ
        # の優先言語を日本語に指定する（日本語版が無ければ原語のまま）。
        # タイトルはファイル名(= 推定の入力)にも使われるためここで効く。
        "extractor_args": {"youtube": {"lang": [METADATA_LANG]}},
        # 受信開始前にキャンセルを効かせる（cancel_filter 参照）
        "match_filter": cancel_filter,
        "postprocessors": [] if pp_class is not None else [extract_audio],
    }
    if best_quality:
        # 音質優先でフォーマットを選び直す（先頭に足した項目が最優先になる）
        opts["format_sort"] = list(BEST_AUDIO_SORT)
    if filters:
        # ffmpeg 音声フィルタとして音声抽出の PP にだけ渡す。フラットな list に
        # すると **全 ffmpeg 系ポストプロセッサ** に適用され、コンテナを直す
        # FixupM4a（-c copy）とぶつかって "Error opening output files" で
        # 実行ごと落ちる（AAC 音源しか無い動画で実測）。キーは PP 名。
        opts["postprocessor_args"] = {"extractaudio": ["-af", ",".join(filters)]}
    if logger is not None:
        # yt-dlp の出力を logging 経由へ切り替える（quiet=True で stdout を止め、
        # logger へ渡した Python ロガーに info/warning/error/debug を流す）
        opts["logger"] = logger
        opts["quiet"] = True

    tracks: list[Track] = []
    ydl_errors: list[str] = []
    with YoutubeDL(opts) as ydl:
        if pp_class is not None:
            # downloader は add_post_processor が set_downloader で入れる
            ydl.add_post_processor(
                pp_class(
                    preferredcodec=fmt,
                    preferredquality=extract_audio.get("preferredquality"),
                    match_source_bitrate=bitrate == BITRATE_SOURCE,
                    # フィルタを掛ける行は copy ではなく必ず再エンコードさせる
                    force_encode=bool(filters),
                ),
                when="post_process",
            )
        # 失敗理由は握り潰されるので、CoreError に載せるため控えておく
        ydl_errors = _record_ydl_errors(ydl)
        try:
            info = ydl.extract_info(url, download=True)
        except CancelledError:
            # match_filter 経由の中断では yt-dlp が break_err() を引数なしで
            # 作り直すため文言が英語の既定値に化ける。ここで戻す
            raise CancelledError("ダウンロードがキャンセルされました。") from None
        # ignoreerrors=True では CancelledError も entry 単位で握り潰されるため、
        # 抜けた直後に必ず再確認する
        if cancel is not None and cancel.is_set():
            raise CancelledError("ダウンロードがキャンセルされました。")
        if not info:
            raise CoreError(_ydl_error_message(ydl_errors, GENERIC_EXTRACT_ERROR))

        # 再生リストなら entries を、単一動画ならそれ自身を対象にする
        entries = info["entries"] if "entries" in info else [info]
        # YouTube Music 判定に使う URL 候補（再生リストでは entry 側に
        # music.youtube.com が残らないため、抽出結果の元 URL も見る）
        source_urls = (url, info.get("original_url"), info.get("webpage_url"))
        direct_count = 0
        if on_stage is not None:
            # 以降は動画ごとに watch 画面へ問い合わせる（1 本あたり最大 5 秒）。
            # ここも進捗が出ないので、変換とは別の段として見せる。
            on_stage(Status.FETCHING)
        for entry in entries:
            # 1 本ごとに watch 画面へ問い合わせる段（最大 5 秒 × 件数）なので、
            # ここで見ないと大きなリストでは停止ボタンが数分効かなく見える
            if cancel is not None and cancel.is_set():
                raise CancelledError("ダウンロードがキャンセルされました。")
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
            if not filters and not is_native_codec(fmt, entry.get("acodec")):
                # 無変換で保存できる音源が無かった動画（例: opus を持たない）。
                # 再エンコードになるので、ログにだけ残しておく
                if fmt in NATIVE_CODECS:
                    _LOG.info(
                        "%s: %s 音声が無いため %s から変換します",
                        entry.get("title") or entry.get("id") or url,
                        fmt,
                        entry.get("acodec") or "不明なコーデック",
                    )
            video_id = entry.get("id")
            localized = _fetch_localized_title(video_id) if video_id else None
            track = Track(
                stem=localized or path.stem,
                url=entry.get("webpage_url") or url,
                filepath=path,
                channel=entry.get("channel") or entry.get("uploader"),
            )
            if ytmusic_direct and is_youtube_music(
                *source_urls, entry.get("original_url")
            ):
                # YouTube Music 上の曲名・アーティスト名を採用する。曲名は
                # 動画タイトルとは別物で（例: "MIMI『 Pale 』feat. 初音ミク"
                # の曲名は "Pale"）、yt-dlp のメタデータには出てこないため
                # 直接問い合わせる。曲名が取れなければ track フィールド →
                # 動画タイトルの順に落とす。
                song, singer = _fetch_ytmusic_song(video_id) if video_id else (None, None)
                use_metadata_title(
                    track,
                    song or entry.get("track") or entry.get("title"),
                    artist=singer or entry.get("artist"),
                )
                direct_count += 1
            tracks.append(track)
        if direct_count:
            _LOG.info(
                "YouTube Music: %d 件のタイトルを推定せずそのまま使います", direct_count
            )

    if not tracks:
        raise CoreError(
            _ydl_error_message(
                ydl_errors, "ダウンロードした音声ファイルが見つかりません。"
            )
        )
    return tracks


def fetch_metadata(
    url: str,
    cancel: threading.Event | None = None,
    expand_playlist: bool = False,
    ytmusic_direct: bool = True,
    logger: logging.Logger | None = None,
) -> list[Track]:
    """URL のメタデータ（タイトル・チャンネル）だけを取得し、Track のリストを返す。

    ダウンロードは行わない。再生リストはフラット抽出（extract_flat）で
    エントリごとに 1 Track を返すため、大きいリストでも各動画の完全な
    情報取得は走らず軽い（DL 前に内容を確認する用途）。返る Track は
    filepath=None の QUEUED 行なので、そのまま実行すれば通常どおり DL される。
    フラット抽出のタイトルは翻訳されないことがあるが、DL 時に stem が
    日本語タイトルへ置き直されるため（download_tracks 参照）ここでは追わない。
    expand_playlist / ytmusic_direct / logger の意味は download_tracks と同じ
    （ytmusic_direct の行は曲名・アーティスト名を確定済みの PENDING で
    返る。_fetch_ytmusic_song で 1 件ずつ問い合わせるため、YouTube Music の
    大きい再生リストではその件数ぶん時間がかかる）。

    Raises:
        CancelledError: cancel がセットされた場合。
        CoreError: 情報を取得できなかった、または有効なエントリが無かった場合。
            download_tracks と同じく yt-dlp の理由を文言に載せる。
    """
    ensure_ytdlp()
    opts = {
        "extract_flat": "in_playlist",
        "noplaylist": not expand_playlist,
        "ignoreerrors": True,
        "extractor_args": {"youtube": {"lang": [METADATA_LANG]}},
    }
    if logger is not None:
        opts["logger"] = logger
        opts["quiet"] = True

    if cancel is not None and cancel.is_set():
        raise CancelledError("情報取得がキャンセルされました。")
    with YoutubeDL(opts) as ydl:
        # download_tracks と同じく、握り潰される失敗理由を控えておく
        ydl_errors = _record_ydl_errors(ydl)
        info = ydl.extract_info(url, download=False)
    if cancel is not None and cancel.is_set():
        raise CancelledError("情報取得がキャンセルされました。")
    if not info:
        raise CoreError(_ydl_error_message(ydl_errors, GENERIC_EXTRACT_ERROR))

    entries = info["entries"] if "entries" in info else [info]
    # YouTube Music 判定に使う URL 候補（download_tracks と同じ考え方）
    source_urls = (url, info.get("original_url"), info.get("webpage_url"))
    tracks: list[Track] = []
    for entry in entries:
        # YouTube Music 行は 1 件ずつ曲名を問い合わせるため、ここでも見る
        if cancel is not None and cancel.is_set():
            raise CancelledError("情報取得がキャンセルされました。")
        if not entry:
            continue
        track = Track(
            stem=entry.get("title") or entry.get("id") or url,
            # フラット抽出のエントリは webpage_url を持たず url が動画 URL
            url=entry.get("webpage_url") or entry.get("url") or url,
            channel=entry.get("channel") or entry.get("uploader"),
        )
        if ytmusic_direct and is_youtube_music(
            *source_urls, entry.get("url"), entry.get("webpage_url")
        ):
            # 展開後の行の URL は www.youtube.com になることがあるため、ここで
            # 印を付けておく（DL 段でこの行の skip_infer が実 Track へ引き継がれる）。
            # 曲名も DL 段と同じ経路で取る（フラット抽出のタイトルは動画
            # タイトルなので、ここで曲名にしておかないと確認の役に立たない）。
            video_id = entry.get("id")
            song, singer = _fetch_ytmusic_song(video_id) if video_id else (None, None)
            use_metadata_title(
                track,
                song or entry.get("track") or entry.get("title"),
                artist=singer or entry.get("artist"),
            )
        tracks.append(track)
    direct_count = sum(1 for t in tracks if t.skip_infer)
    if direct_count:
        _LOG.info(
            "YouTube Music: %d 件のタイトルを推定せずそのまま使います", direct_count
        )
    if not tracks:
        raise CoreError(_ydl_error_message(ydl_errors, "有効な動画が見つかりません。"))
    return tracks


# ---------------------------------------------------------------------------
# タイトル推定
# ---------------------------------------------------------------------------

# 応答に該当項目が無く曲名が空のまま返った行に載せる理由。
# 空欄のままだと「推定を飛ばした」のか「推定に失敗した」のか区別が付かない。
EMPTY_TITLE_ERROR = "タイトルを推定できませんでした（LLM の応答にこの行の項目がありません）。"


# GUI 向けの案内。mv2title の文言は「'X' をロードするか、MODEL を一覧にある
# 名前にしてください」までなので、その操作場所だけを足す。
MODEL_MISMATCH_HINT = "（ロードは LM Studio 側で、MODEL の変更は [設定] で行えます）"


# --- 空で返った項目の拾い直し（mv2title 0.4.0 で不要になったため無効）---------
#
# 症状: 2 件以上を一度に推定すると、1 件目以外の曲名が空欄になる。
#
# 原因は 2 つの合わせ技だった。
#  ① 構造化出力（mv2title の strict な json_schema）を付けて送ると、モデルに
#     よっては **配列の 1 件目だけを出力して停止する**。実測（LM Studio +
#     gemma-4-e2b）で finish_reason=stop / completion_tokens 37 /
#     reasoning_tokens 0 と、入力が何件でも決定的にこうなる。同じ入力を
#     response_format 無しで送ると全件返る（同条件で reasoning_tokens 555）。
#     制約付きデコードだと思考する余地が無く、その場で打ち切られるため。
#  ② mv2title の check_results は応答が入力より短くても **件数を合わせて**
#     返す（不足分は title="" / valid=False のプレースホルダ）。このため
#     infer_titles の「件数が合わなければ CoreError」は素通りし、該当行だけが
#     黙って空欄になる。
#
# 恒久対応は mv2title 0.4.0 で入った（bypass_check と retry_invalid を分離し、
# bypass_check=True でも部分リトライが走る。欠けた項目は use_schema=False で
# 問い合わせ直し、打ち切りを検出したら以降のバッチも構造化出力なしに落とす）。
# こちらのリトライは no-op になるだけでなく、失敗時は **mv2title が直前に
# 送ったのと同じ条件（schema なし・温度 0.0）を送り直す無駄な 1 往復**に
# なるため、呼び出しごと止めてある。
#
# 残してあるのは、mv2title 0.3.0 以前で動かす場合と、別のモデル・別の
# エンドポイントで同種の「応答が入力より短い」症状に当たった場合の備え。
# 復活させるなら下の関数と infer_titles 内の呼び出し（同じ理由のコメント付き）
# の両方を戻し、tests/test_core.py にリトライのテストを足すこと。
#
# def _retry_missing_titles(
#     inputs: list[TitleInput],
#     results: list,
#     client: LLMClient,
#     batch_size: int,
# ) -> list:
#     """曲名が空で返った項目だけ、構造化出力を使わずに 1 回だけ問い合わせ直す。
#
#     再問い合わせも失敗した行はそのまま（空 / valid=False）返す。呼び出し元が
#     EMPTY_TITLE_ERROR を載せるので、行は空欄のまま放置されない。
#     """
#     missing = [i for i, r in enumerate(results) if not (r.title or "").strip()]
#     if not missing:
#         return results
#     _LOG.info(
#         "%d/%d 件が空で返ったため、構造化出力なしで問い合わせ直します",
#         len(missing),
#         len(results),
#     )
#     retry = extract_titles(
#         [inputs[i] for i in missing],
#         client,
#         batch_size=batch_size,
#         bypass_check=True,
#         use_schema=False,
#     )
#     if len(retry) != len(missing):
#         # 件数が合わない再問い合わせは誤対応の元なので丸ごと捨てる
#         return results
#     filled = 0
#     for pos, res in zip(missing, retry):
#         if not (res.title or "").strip():
#             continue
#         # サブセット内の通し番号を、リスト全体での位置へ戻す
#         res.index = pos + 1
#         results[pos] = res
#         filled += 1
#     _LOG.info("再問い合わせで %d/%d 件を回収しました", filled, len(missing))
#     return results


def infer_titles(
    tracks: Sequence[Track],
    client: LLMClient | None = None,
    batch_size: int = BATCH_SIZE,
    force: bool = False,
    use_schema: bool = USE_SCHEMA,
) -> None:
    """各 Track の曲名を mv2title で推定し、guessed_title / valid を更新する。

    mv2title はバッチ設計のため、対象をまとめて 1 回で呼ぶ（1 リクエスト N 件）。
    manual=True の行と skip_infer=True の行（YouTube Music など、曲名が
    メタデータで確定している行）は保護してスキップする
    （force=True で明示的に上書き）。
    成功した行は Status.PENDING になる（書き込みは write_tags で行う）。
    応答に載らなかった（曲名が空で返った）行の拾い直しは mv2title 0.4.0 が
    行う。それでも空のまま返った行は PENDING のまま error に
    EMPTY_TITLE_ERROR を載せる（空欄だけを残さないため）。
    use_schema=False にすると構造化出力（response_format）を付けずに送る。
    制約付きデコードで応答が 1 件目だけに打ち切られるモデルでは、これを
    切ったほうが 1 バッチ目の捨て呼び出しと部分リトライぶんの往復が減る
    （USE_SCHEMA 参照）。

    Raises:
        CoreError: 応答件数が対象件数と一致しない場合（全対象行を ERROR にした上で）。
        その他: LLM 接続エラー等はそのまま伝播する（呼び出し元で処理）。
    """
    targets = [t for t in tracks if force or not (t.manual or t.skip_infer)]
    if not targets:
        return
    for t in targets:
        t.status = Status.INFERRING
        t.error = ""

    inputs = [TitleInput(t.stem, channel=t.channel) for t in targets]
    if client is None:
        client = make_client()
    try:
        # 応答に載らなかった項目の拾い直しは mv2title 側で行われる
        results = extract_titles(
            inputs, client, batch_size=batch_size, bypass_check=True, use_schema=use_schema
        )
    except LibModelMismatchError as e:
        # 別のモデルが答えた場合。CoreError でないと行が ERROR にならないため
        # 包み直し、GUI での直し方を添える
        msg = f"タイトル推定に失敗しました: {e}{MODEL_MISMATCH_HINT}"
        for t in targets:
            t.status = Status.ERROR
            t.error = msg
        raise ModelMismatchError(msg) from e
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
        t.skip_infer = False  # force で推定し直した行は以降も推定対象に戻す
        t.status = Status.PENDING
        # 再問い合わせでも曲名が取れなかった行。PENDING のままにして手入力・
        # 再推定・作者/アルバムのみの書き込みは従来どおり効かせつつ、理由を
        # 残して「空欄なだけ」の状態にしない（GUI は推定タイトル列に出す）
        t.error = "" if (res.title or "").strip() else EMPTY_TITLE_ERROR


# ---------------------------------------------------------------------------
# タグ書き込み
# ---------------------------------------------------------------------------


def write_title(
    filepath: Path,
    title: str,
    artist: str | None = None,
    album: str | None = None,
) -> None:
    """ファイル形式に応じたタイトル（と任意で作者・アルバム名）タグを書き込む。

    タイトルは .mp3 / .wav が ID3 の TIT2 フレーム、.m4a が MP4 の \xa9nam
    アトム、.opus が Vorbis コメントの TITLE。アーティストは TPE1 / \xa9ART /
    ARTIST、アルバム名は TALB / \xa9alb / ALBUM。
    3 項目とも **空なら書き込まない**（ファイル側の既存値をそのまま残す）ので、
    title="" で呼べば作者・アルバム名だけを更新できる（write_tags 参照）。
    """
    ext = filepath.suffix.lower()
    if ext == ".mp3":
        try:
            tags = ID3(str(filepath))
        except ID3NoHeaderError:
            tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        tags.save(str(filepath))
    elif ext == ".wav":
        audio = WAVE(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        if title:
            audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            audio.tags["TALB"] = TALB(encoding=3, text=album)
        audio.save(str(filepath))
    elif ext == ".m4a":
        audio = MP4(str(filepath))
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        if title:
            audio.tags["\xa9nam"] = [title]
        if artist:
            audio.tags["\xa9ART"] = [artist]
        if album:
            audio.tags["\xa9alb"] = [album]
        audio.save()
    elif ext == ".opus":
        audio = OggOpus(str(filepath))
        # Ogg Opus のタグは Vorbis コメント。値は常にリストで持つ
        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]
        audio.save()
    else:
        raise ValueError(f"unsupported extension: {ext}")


def write_tags(
    tracks: Sequence[Track],
    on_result: Callable[[Track], None] | None = None,
) -> None:
    """各 Track の guessed_title（と artist / album）をメタデータへ書き込む。

    スキップ方針（CLI / GUI 共通のポリシーをここに集約）:
    - guessed_title が空 → 曲名は書かない（PENDING のまま、error に理由）
    - valid=False かつ手動編集されていない → 同上。手動編集済み(manual=True)
      ならユーザーの意思なので書き込む。
    - ただし曲名を書かない行でも、artist / album が入っていればその 2 項目
      だけは書き込む（取り込んだファイルの作者・アルバム名を直す編集を、
      曲名が未確定というだけで捨てないため）。行は PENDING のまま残る。
    - 書き込み失敗 → ERROR / 曲名まで書けたら DONE

    1 行の失敗は他の行を止めない。on_result は各行の処理直後に呼ばれる。
    """
    for t in tracks:
        if t.filepath is None:
            t.status = Status.ERROR
            t.error = "ファイルパスが未設定です。"
            if on_result is not None:
                on_result(t)
            continue

        # 曲名を書かない理由（空文字なら曲名も書く）
        if not t.guessed_title:
            skip = "曲名が空"
        elif t.valid is False and not t.manual:
            skip = "検証失敗（元タイトルに含まれない曲名）"
        else:
            skip = ""

        if skip and not (t.artist or t.album):
            t.status = Status.PENDING
            t.error = f"{skip}のためスキップしました。"
        else:
            t.status = Status.WRITING
            try:
                write_title(
                    t.filepath,
                    "" if skip else t.guessed_title,  # 空文字は書き込まれない
                    artist=t.artist or None,
                    album=t.album or None,
                )
            except Exception as e:
                t.status = Status.ERROR
                t.error = f"書き込みに失敗しました: {e}"
            else:
                t.status = Status.PENDING if skip else Status.DONE
                t.error = f"{skip}のため、作者・アルバム名のみ書き込みました。" if skip else ""
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
