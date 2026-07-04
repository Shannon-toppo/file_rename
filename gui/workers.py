# -*- coding: utf-8 -*-
"""PipelineWorker: core を呼ぶバックグラウンドワーカー（QRunnable）。

スレッド境界の規約（厳守）
==========================
1. ワーカーは QThreadPool 上で実行される。同時に走るパイプラインは 1 本だけ
   （MainWindow が実行中は開始ボタンを無効化して担保する）。
2. ワーカーは QWidget / モデルに絶対に触らない。core の関数を呼び、Track
   （dataclass）のフィールドを書き換え、シグナルを emit するだけ。
3. UI の更新（モデル更新・行追加・再描画）は MainWindow 側のスロット
   （= メインスレッド、queued connection）でのみ行う。ワーカーは以下の
   WorkerSignals を emit するだけ:
     - track_updated(Track)          … スロットで model.refresh_track(track)
     - tracks_ready(Track, list)     … プレースホルダ行を実 Track 行へ差し替え
     - progress(Track, float)        … DL 進捗（0-100）
     - finished() / error(str)
4. UI 側は Track の内容を「シグナル受信後にのみ」読む。queued connection の
   happens-before 関係により、ワーカーの書き込みがスロット実行前に可視化
   されることが保証される（ワーカーとスロットは同じ Track を同時に触らない）。
5. キャンセルは threading.Event。停止ボタンで set() し、core.download_tracks
   に渡す。推定・書き込みは段の境目で is_set() を確認して CancelledError。
"""
import logging
import threading
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

import core
from core import CancelledError, LLMClient, Status, Track

# パイプラインの実行モード
MODE_FULL = "full"  # DL → 推定 → 書き込み（自動フロー）
MODE_INFER = "infer"  # 選択行の再推定のみ（DL 段をスキップ）
MODE_WRITE = "write"  # 選択行の書き込みのみ


class WorkerSignals(QObject):
    """ワーカー → メインスレッドのシグナル定義（QObject 派生が必須）。"""

    track_updated = Signal(object)  # 更新された Track
    tracks_ready = Signal(object, object)  # (プレースホルダ Track, list[Track])
    # (Track, 0-100, "2/5" 等のリスト内番号ラベル。単一動画なら空文字)
    progress = Signal(object, float, str)
    finished = Signal()
    error = Signal(str)
    # LLM エンドポイントに接続できず DL のみの縮退モードへ切り替えたとき(理由)
    connection_failed = Signal(str)
    # 書き込み実行後の集計 (完了, スキップ, 失敗)。ステータスバー表示用
    write_summary = Signal(int, int, int)


class PipelineWorker(QRunnable):
    """core を順に呼ぶパイプライン。1 実行 = 1 スナップショット処理。

    Args:
        tracks: 実行時点の対象 Track のスナップショット。
        mode: MODE_FULL / MODE_INFER / MODE_WRITE。
        fmt: DL 形式（MODE_FULL のみ使用）。
        auto_write: True なら推定後に自動で書き込む（MODE_FULL のみ）。
        force: 再推定で manual 行も上書きするか（MODE_INFER で使用）。
        client: 注入する LLMClient（None なら core が生成）。
        cancel: キャンセル用 Event。
        skip_infer: True なら DL のみ実行（縮退モード。MODE_FULL のみ）。
            False でも MODE_FULL 冒頭の接続チェックに失敗すると自動で
            縮退し、connection_failed を emit する。
        batch_size: core.infer_titles へ渡すバッチサイズ（None なら core 既定）。
        out_dir: DL 保存先（None なら core.FILES_DIR）。
        expand_playlist: True なら動画＋リスト混在 URL もリスト全体を展開する。
    """

    def __init__(
        self,
        tracks: Sequence[Track],
        mode: str = MODE_FULL,
        fmt: str = "mp3",
        auto_write: bool = True,
        force: bool = False,
        client: LLMClient | None = None,
        cancel: threading.Event | None = None,
        skip_infer: bool = False,
        batch_size: int | None = None,
        out_dir: Path | None = None,
        expand_playlist: bool = False,
    ):
        super().__init__()
        self.signals = WorkerSignals()
        self._tracks = list(tracks)
        self._mode = mode
        self._fmt = fmt
        self._auto_write = auto_write
        self._force = force
        self._client = client
        self._cancel = cancel or threading.Event()
        self._skip_infer = skip_infer
        self._batch_size = batch_size if batch_size is not None else core.BATCH_SIZE
        self._out_dir = out_dir
        self._expand_playlist = expand_playlist

    # -- QRunnable のエントリポイント ---------------------------------------

    def run(self) -> None:
        try:
            if self._mode == MODE_FULL:
                self._run_full()
            elif self._mode == MODE_INFER:
                self._run_infer(self._tracks)
            elif self._mode == MODE_WRITE:
                self._run_write(self._tracks)
        except CancelledError:
            # キャンセルは正常終了として扱う（残行は未処理のまま）
            pass
        except core.CoreError as e:
            self.signals.error.emit(str(e))
        except Exception as e:  # noqa: BLE001 - 全例外を UI へ通知する
            self.signals.error.emit(f"予期しないエラー: {e}")
        finally:
            self.signals.finished.emit()

    # -- 各段 ----------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise CancelledError("処理がキャンセルされました。")

    def _run_full(self) -> None:
        """接続チェック → DL（URL 行のみ）→ まとめて推定 → 自動書き込み。

        LLM エンドポイントに届かない場合は DL のみの縮退モードへ自動で
        切り替える（行は QUEUED で残るので、サーバ起動後に再実行すれば
        推定から続きが処理できる）。
        """
        skip_infer = self._skip_infer
        if not skip_infer:
            ok, msg = core.check_connection()
            if not ok:
                skip_infer = True
                self.signals.connection_failed.emit(msg)
        ready: list[Track] = self._download_all()
        self._check_cancel()
        if skip_infer:
            return  # 縮退モード: DL のみで終了（QUEUED のまま）
        # DL 済み + ローカル追加分のうち、推定対象を集める
        self._run_infer(ready)
        if not self._auto_write:
            return  # PENDING（確認待ち）で停止
        self._check_cancel()
        # 推定に成功した（PENDING の）行を書き込む
        writable = [t for t in ready if t.status is Status.PENDING]
        self._run_write(writable)

    def _download_all(self) -> list[Track]:
        """URL 行を DL して実 Track 行へ差し替える。ローカル行はそのまま返す。

        Returns:
            後続の推定対象となる Track のリスト（DL 済み実 Track + ローカル行）。
        """
        ready: list[Track] = []
        for placeholder in self._tracks:
            self._check_cancel()
            if placeholder.url is None or placeholder.filepath is not None:
                # ローカルファイル行と DL 済みの実 Track 行（url と filepath の
                # 両方を持つ）は DL をスキップ（再実行時の再ダウンロード防止）
                ready.append(placeholder)
                continue

            placeholder.status = Status.DOWNLOADING
            placeholder.error = ""
            self.signals.track_updated.emit(placeholder)

            def on_progress(
                _name: str,
                pct: float,
                index: int | None = None,
                total: int | None = None,
                _ph: Track = placeholder,
            ) -> None:
                # 再生リストなら「何番目/全体数」ラベルを添える
                label = f"{index}/{total}" if index and total else ""
                self.signals.progress.emit(_ph, pct, label)

            try:
                new_tracks = core.download_tracks(
                    placeholder.url,
                    self._fmt,
                    on_progress=on_progress,
                    cancel=self._cancel,
                    out_dir=self._out_dir,
                    expand_playlist=self._expand_playlist,
                    # yt-dlp の出力をログパネルへ流す（GUI 経由の DL は常に
                    # logging 経由）。ハンドラはワーカースレッドから呼ばれるが
                    # QtLogHandler はシグナル emit のみでスレッド安全（logpanel.py）
                    logger=logging.getLogger("yt_dlp"),
                )
            except CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 行単位のエラーは他行を止めない
                placeholder.status = Status.ERROR
                placeholder.error = f"ダウンロードに失敗しました: {e}"
                self.signals.track_updated.emit(placeholder)
                continue

            # プレースホルダ行を実 Track 行（再生リストは複数）へ差し替える
            self.signals.tracks_ready.emit(placeholder, new_tracks)
            ready.extend(new_tracks)
        return ready

    def _run_infer(self, tracks: Sequence[Track]) -> None:
        """対象行をまとめて 1 回で推定する（バッチ）。"""
        self._check_cancel()
        # QUEUED / PENDING かつ（force でなければ）manual=False の行を対象に
        targets = [
            t
            for t in tracks
            if t.status in (Status.QUEUED, Status.PENDING) and (self._force or not t.manual)
        ]
        if not targets:
            return
        try:
            core.infer_titles(
                targets, client=self._client, batch_size=self._batch_size, force=self._force
            )
        finally:
            # 成否にかかわらず各行の状態を UI へ反映する
            for t in targets:
                self.signals.track_updated.emit(t)

    def _run_write(self, tracks: Sequence[Track]) -> None:
        """対象行のタグを書き込み、集計を write_summary で通知する。"""
        self._check_cancel()
        if not tracks:
            return
        core.write_tags(list(tracks), on_result=lambda t: self.signals.track_updated.emit(t))
        done = sum(1 for t in tracks if t.status is Status.DONE)
        errors = sum(1 for t in tracks if t.status is Status.ERROR)
        skipped = len(tracks) - done - errors
        self.signals.write_summary.emit(done, skipped, errors)
