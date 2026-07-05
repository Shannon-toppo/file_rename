# -*- coding: utf-8 -*-
"""プレビュー再生: ノーマライズ・無音削除の結果を耳で確認するための最小プレーヤ。

QMediaPlayer + QAudioOutput の薄いラッパ。メインスレッドでのみ使う
（パイプラインとは独立で、ワーカーからは触らない）。QtMultimedia の
バックエンド初期化を避けるため、実プレーヤは初回 play() で遅延生成する
（オフスクリーンのテストでは play() を呼ばない限りバックエンドに触れない）。
"""
from pathlib import Path

from PySide6.QtCore import QObject, QUrl, Signal


def tail_start_ms(duration_ms: int, tail_secs: float) -> int:
    """末尾試聴の開始位置（ms）。曲の長さが tail_secs より短ければ 0（先頭から）。"""
    return max(0, int(duration_ms - tail_secs * 1000))


def format_time(ms: int) -> str:
    """ミリ秒を「分:秒」表記にする（シークバー横の時間表示用）。"""
    secs = max(0, ms) // 1000
    return f"{secs // 60}:{secs % 60:02d}"


class PreviewPlayer(QObject):
    """1 ファイルずつ再生する試聴プレーヤ。

    playing_changed(bool) で UI（▶/⏸ ボタン表示など）が再生状態に追従し、
    position_changed / duration_changed(ms) でシークバーが追従する。
    末尾試聴は setSource 直後には曲の長さが分からないため、
    durationChanged を待ってからシークする。
    """

    playing_changed = Signal(bool)
    position_changed = Signal(int)  # 再生位置 (ms)
    duration_changed = Signal(int)  # 曲の長さ (ms)。ソース読み込み時に確定

    # 末尾試聴で再生する秒数（無音削除の確認に十分な長さ）
    TAIL_SECS = 10.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = None  # QMediaPlayer（遅延生成）
        self._audio = None  # QAudioOutput（player が参照を持たないため自前で保持）
        self._pending_tail = False  # durationChanged 待ちの末尾シーク要求
        self.current_path: Path | None = None  # 現在ロード中のファイル

    def play(self, path: Path, tail_only: bool = False) -> None:
        """path を先頭（tail_only=True なら末尾 TAIL_SECS 秒）から再生する。"""
        player = self._ensure_player()
        player.stop()
        self._pending_tail = tail_only
        self.current_path = Path(path)
        player.setSource(QUrl.fromLocalFile(str(path)))
        player.play()

    def pause(self) -> None:
        if self._player is not None:
            self._player.pause()

    def resume(self) -> None:
        """一時停止位置から再生を再開する（play() と違いソースを読み直さない）。"""
        if self._player is not None:
            self._player.play()

    def stop(self) -> None:
        if self._player is not None:
            self._player.stop()

    def seek(self, position_ms: int) -> None:
        """再生位置を移動する（シークバーのドラッグから呼ばれる）。"""
        if self._player is not None:
            self._player.setPosition(position_ms)

    @property
    def is_playing(self) -> bool:
        if self._player is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer

        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @property
    def is_paused(self) -> bool:
        if self._player is None:
            return False
        from PySide6.QtMultimedia import QMediaPlayer

        return self._player.playbackState() == QMediaPlayer.PlaybackState.PausedState

    # -- 内部 -----------------------------------------------------------------

    def _ensure_player(self):
        if self._player is None:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.playbackStateChanged.connect(self._on_state_changed)
            self._player.durationChanged.connect(self._on_duration_changed)
            self._player.positionChanged.connect(self.position_changed.emit)
        return self._player

    def _on_duration_changed(self, duration_ms: int) -> None:
        # 末尾試聴: 長さが判明したタイミングで末尾へシークする
        if self._pending_tail and duration_ms > 0:
            self._pending_tail = False
            self._player.setPosition(tail_start_ms(duration_ms, self.TAIL_SECS))
        self.duration_changed.emit(duration_ms)

    def _on_state_changed(self, state) -> None:
        from PySide6.QtMultimedia import QMediaPlayer

        self.playing_changed.emit(state == QMediaPlayer.PlaybackState.PlayingState)
