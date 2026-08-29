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
6. MODE_FULL では DL 段と推定段を並走させる（_InferStage）。ワーカースレッド
   の内側にもう 1 本だけスレッドを持ち、DL で溜まった行をバッチ単位で推定
   （＋自動書き込み）へ流す。並走するのはこの 2 本だけで、推定側は
   max_workers=1 なので LLM への同時リクエストは常に 1 本。シグナルは
   どちらのスレッドから emit しても queued connection でメインスレッドへ
   直列化される。行は DL 側 → 推定側の順にしか触られない（feed 済みの行を
   DL 側が再度触ることはない）ので、Track の所有権は常に片側にある。
"""
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

import core
import ytdlp_runtime
from core import CancelledError, LLMClient, Status, Track

# パイプラインの実行モード
MODE_FULL = "full"  # DL → 推定 → 書き込み（自動フロー）
MODE_INFER = "infer"  # 選択行の再推定のみ（DL 段をスキップ）
MODE_WRITE = "write"  # 選択行の書き込みのみ
MODE_FETCH = "fetch"  # メタデータのみ取得（DL しない。再生リストの内容確認用）


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
    # 情報取得 / DL 段の集計 (段名, 成功, 失敗)。失敗行があっても最後の
    # ステータスバーが「完了」だけになり気付けない問題への対策
    stage_summary = Signal(str, int, int)


class PipelineWorker(QRunnable):
    """core を順に呼ぶパイプライン。1 実行 = 1 スナップショット処理。

    Args:
        tracks: 実行時点の対象 Track のスナップショット。
        mode: MODE_FULL / MODE_INFER / MODE_WRITE / MODE_FETCH。
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
        expand_playlist: True なら動画＋リスト混在 URL もリスト全体を展開する
            （MODE_FETCH の情報取得にも同じ設定が効く）。
        normalize: True（既定）なら DL 時に音量ノーマライズ(loudnorm)を掛ける。
        loudness: ノーマライズの基準値 LUFS（loudnorm の I）。
        trim_silence: True なら DL 時に末尾の無音区間を削除する（試験的）。
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
        normalize: bool = True,
        loudness: float = core.NORMALIZE_TARGET_I,
        trim_silence: bool = False,
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
        self._normalize = normalize
        self._loudness = loudness
        self._trim_silence = trim_silence

    # -- QRunnable のエントリポイント ---------------------------------------

    def run(self) -> None:
        try:
            if self._mode == MODE_FULL:
                self._run_full()
            elif self._mode == MODE_INFER:
                self._run_infer(self._tracks)
            elif self._mode == MODE_WRITE:
                self._run_write(self._tracks)
            elif self._mode == MODE_FETCH:
                self._run_fetch()
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
        """接続チェック → DL（URL 行のみ）→ 推定 → 自動書き込み。

        推定は DL と**並走**させる（_InferStage）。DL の完了行をバッチ数
        （batch_size）ぶん溜めた時点で推定へ流すので、リストが 1 バッチより
        長ければ「残りの DL」と「溜まったぶんの推定」が同時に進む。
        1 バッチに満たない端数は DL 完了後にまとめて流す（= 従来と同じ
        1 回の推定）ので、少数の行では挙動が変わらない。

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
        if skip_infer:
            # 縮退モード: DL のみで終了（QUEUED のまま）
            self._download_all()
            self._check_cancel()
            return

        stage = _InferStage(self)
        try:
            self._download_all(on_ready=stage.feed)
        finally:
            # DL がキャンセル・例外で抜けても、投入済みバッチは必ず回収する
            # （シグナルを finished より後に emit させないため）
            stage.close(flush=not self._cancel.is_set())
        self._check_cancel()
        stage.raise_if_failed()
        stage.emit_write_summary()

    def _download_all(
        self, on_ready: Callable[[list[Track]], None] | None = None
    ) -> list[Track]:
        """URL 行を DL して実 Track 行へ差し替える。ローカル行はそのまま返す。

        Args:
            on_ready: 1 行ぶんの DL が終わるたびに、後続段へ渡せるように
                なった Track のリストで呼ばれる（推定との並走用）。tracks_ready
                を emit した**後**に呼ぶので、UI 側は必ず行の差し替えを先に
                受け取る。None なら戻り値にまとめるだけ。

        Returns:
            後続の推定対象となる Track のリスト（DL 済み実 Track + ローカル行）。
        """
        ready: list[Track] = []
        attempted = 0  # DL を試みた行数（スキップ行は集計に含めない）
        failed = 0
        for placeholder in self._tracks:
            self._check_cancel()
            if placeholder.url is None or placeholder.filepath is not None:
                # ローカルファイル行と DL 済みの実 Track 行（url と filepath の
                # 両方を持つ）は DL をスキップ（再実行時の再ダウンロード防止）
                ready.append(placeholder)
                if on_ready is not None:
                    on_ready([placeholder])
                continue
            attempted += 1

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
                    normalize=self._normalize,
                    loudness=self._loudness,
                    trim_silence=self._trim_silence,
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
                failed += 1
                continue

            # 情報取得済み行への手動編集（タイトル・アーティスト）は、DL で
            # 行が実 Track へ差し替わっても失われないよう引き継ぐ（単一動画
            # のみ。リスト展開はどの行への編集か対応付けられないため対象外）
            if len(new_tracks) == 1:
                real = new_tracks[0]
                if placeholder.manual:
                    real.guessed_title = placeholder.guessed_title
                    real.manual = True
                    real.valid = placeholder.valid
                    real.status = Status.PENDING
                if placeholder.artist:
                    real.artist = placeholder.artist

            # プレースホルダ行を実 Track 行（再生リストは複数）へ差し替える
            self.signals.tracks_ready.emit(placeholder, new_tracks)
            ready.extend(new_tracks)
            if on_ready is not None:
                on_ready(list(new_tracks))
        if attempted:
            self.signals.stage_summary.emit("ダウンロード", attempted - failed, failed)
        return ready

    def _run_fetch(self) -> None:
        """URL 行のメタデータだけを取得し、行を展開する（DL しない）。

        対象は未取得のプレースホルダ行（stem == url）のみ。取得済みの行
        （stem が動画タイトルに置き換わっている）とローカル行はスキップする
        ので、再実行しても再取得や手動編集の上書きは起きない。
        """
        attempted = 0  # 取得を試みた行数（スキップ行は集計に含めない）
        failed = 0
        for placeholder in self._tracks:
            self._check_cancel()
            if placeholder.url is None or placeholder.filepath is not None:
                continue
            if placeholder.stem != placeholder.url:
                continue  # 取得済み（展開済み）の行は再取得しない

            attempted += 1
            placeholder.status = Status.FETCHING
            placeholder.error = ""
            self.signals.track_updated.emit(placeholder)
            try:
                new_tracks = core.fetch_metadata(
                    placeholder.url,
                    cancel=self._cancel,
                    expand_playlist=self._expand_playlist,
                    # yt-dlp の出力をログパネルへ流す（DL と同じ経路）
                    logger=logging.getLogger("yt_dlp"),
                )
            except CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 行単位のエラーは他行を止めない
                placeholder.status = Status.ERROR
                placeholder.error = f"情報の取得に失敗しました: {e}"
                self.signals.track_updated.emit(placeholder)
                failed += 1
                continue

            # プレースホルダ行をメタデータ行（再生リストは複数）へ差し替える
            self.signals.tracks_ready.emit(placeholder, new_tracks)
        if attempted:
            self.signals.stage_summary.emit("情報取得", attempted - failed, failed)

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
        # 推定中であることを先に見せる（DL と並走するので、DL 中の行と
        # 推定中の行が同時に並ぶ）。core.infer_titles も同じ状態を設定する。
        for t in targets:
            t.status = Status.INFERRING
            t.error = ""
            self.signals.track_updated.emit(t)
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
        done, skipped, errors = self._write_tracks(tracks)
        self.signals.write_summary.emit(done, skipped, errors)

    def _write_tracks(self, tracks: Sequence[Track]) -> tuple[int, int, int]:
        """タグを書き込み (完了, スキップ, 失敗) を返す（emit はしない）。

        並走モードでは書き込みもバッチごとに走るため、集計の通知は
        呼び出し元（_InferStage）が最後に 1 回だけ行う。
        """
        if not tracks:
            return (0, 0, 0)
        core.write_tags(list(tracks), on_result=lambda t: self.signals.track_updated.emit(t))
        done = sum(1 for t in tracks if t.status is Status.DONE)
        errors = sum(1 for t in tracks if t.status is Status.ERROR)
        return (done, len(tracks) - done - errors, errors)


class _InferStage:
    """DL と並走してタイトル推定（＋自動書き込み）を進める段。

    PipelineWorker（= DL 側）が feed() で完了行を渡し、推定対象がバッチ数に
    達したぶんだけ内部スレッドへ投入する。DL のネットワーク待ちと LLM の
    推論待ちが重なるので、行数がバッチ数を超えるほど効いてくる。

    スレッドは max_workers=1 の ThreadPoolExecutor 1 本だけ。LLM への
    リクエストが同時に 2 本走らないようにするためで、バッチは投入順に
    直列で処理される。ワーカーと同じくウィジェットには触らず、シグナルの
    emit だけを行う（queued connection なのでスレッドは問わない）。

    行の所有権: feed() へ渡した Track を DL 側が触ることはないので、
    1 つの Track を同時に触るスレッドは常に 1 本。
    """

    def __init__(self, worker: "PipelineWorker"):
        self._worker = worker
        self._batch_size = max(1, worker._batch_size)
        self._buf: list[Track] = []  # DL 側スレッドのみが触る（ロック不要）
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="infer")
        self._lock = threading.Lock()  # 以下の集計・エラーを両スレッドで共有
        self._error: BaseException | None = None
        self._attempted = 0  # 書き込みを試みた行数（0 なら集計を出さない）
        self._done = 0
        self._skipped = 0
        self._errors = 0

    # -- DL 側スレッドから呼ばれる ------------------------------------------

    def feed(self, tracks: list[Track]) -> None:
        """DL が終わった行を受け取り、溜まったらバッチとして投入する。"""
        self._buf.extend(tracks)
        while self._count_targets(self._buf) >= self._batch_size:
            self._submit(self._take_chunk())

    def close(self, flush: bool = True) -> None:
        """端数を流し込み、投入済みバッチの完了まで待つ。

        Args:
            flush: False（キャンセル時）なら残りは投入せず、未着手のバッチも
                捨てる。実行中の 1 バッチだけは待つ（シグナルを finished より
                後に emit させないため）。
        """
        while flush and self._buf:
            self._submit(self._take_chunk())
        self._buf = []
        self._pool.shutdown(wait=True, cancel_futures=not flush)

    def raise_if_failed(self) -> None:
        """推定・書き込み中に起きた最初の例外を、ワーカースレッドで送出する。

        並走している都合上、失敗しても DL は最後まで走らせる（DL が一番高い
        コストなので、LLM が落ちていても取得ぶんは残す）。2 バッチ目以降は
        投入を止めるため、同じ失敗が行数ぶん繰り返されることはない。
        """
        if self._error is not None:
            raise self._error

    def emit_write_summary(self) -> None:
        """バッチごとの書き込み結果を合算して 1 回だけ通知する。"""
        if self._attempted:
            self._worker.signals.write_summary.emit(self._done, self._skipped, self._errors)

    # -- 内部 ---------------------------------------------------------------

    def _is_target(self, track: Track) -> bool:
        """_run_infer が推定対象とみなす行か（バッチ数を数えるのに使う）。"""
        return track.status in (Status.QUEUED, Status.PENDING) and (
            self._worker._force or not track.manual
        )

    def _count_targets(self, tracks: list[Track]) -> int:
        return sum(1 for t in tracks if self._is_target(t))

    def _take_chunk(self) -> list[Track]:
        """推定対象をちょうど batch_size 件含む最短の先頭部分を切り出す。

        推定対象でない行（手動編集済みなど）も一緒に運ぶ。自動書き込みの
        対象になるので、どこかのバッチに乗せないと書き残しになる。
        """
        count = 0
        for i, t in enumerate(self._buf):
            if self._is_target(t):
                count += 1
                if count == self._batch_size:
                    chunk = self._buf[: i + 1]
                    del self._buf[: i + 1]
                    return chunk
        chunk, self._buf = self._buf, []
        return chunk

    def _submit(self, chunk: list[Track]) -> None:
        if not chunk:
            return
        with self._lock:
            failed = self._error is not None
        if failed or self._worker._cancel.is_set():
            return  # 失敗・キャンセル後は投入しない（行は QUEUED のまま残る）
        self._pool.submit(self._process, chunk)

    # -- 推定スレッド側 ------------------------------------------------------

    def _process(self, chunk: list[Track]) -> None:
        try:
            self._worker._run_infer(chunk)
            if not self._worker._auto_write:
                return  # PENDING（確認待ち）で停止
            self._worker._check_cancel()
            writable = [t for t in chunk if t.status is Status.PENDING]
            if not writable:
                return
            done, skipped, errors = self._worker._write_tracks(writable)
            with self._lock:
                self._attempted += len(writable)
                self._done += done
                self._skipped += skipped
                self._errors += errors
        except CancelledError:
            pass  # キャンセルは _run_full 側の _check_cancel で扱う
        except BaseException as e:  # noqa: BLE001 - 最初の 1 件をワーカーへ運ぶ
            with self._lock:
                if self._error is None:
                    self._error = e


class YtdlpSignals(QObject):
    """YtdlpWorker → メインスレッドのシグナル定義。"""

    # 進行中の説明（「最新版を確認しています...」など）
    status = Signal(str)
    # 終了。(成功したか, 表示メッセージ, 再起動が必要か)
    done = Signal(bool, str, bool)


class YtdlpWorker(QRunnable):
    """yt-dlp の取得・更新をバックグラウンドで行う（QRunnable）。

    PipelineWorker と同じスレッド境界の規約に従う（ウィジェットに触らず
    シグナルだけを emit する）。ネットワーク待ちで UI を固めないための分離で、
    通信は ytdlp_runtime 側が担う。

    本体と一緒に EJS（yt-dlp-ejs）も揃える。YouTube の署名・n チャレンジを
    解くスクリプトで、無いと yt-dlp が実行のたびに GitHub / npm を叩きに行く
    （既定では禁止されているので解けずに速度が落ちる）。

    Args:
        check_only: True なら最新版の有無を確認するだけで取得しない。
    """

    def __init__(self, check_only: bool = False):
        super().__init__()
        self.signals = YtdlpSignals()
        self._check_only = check_only

    def run(self) -> None:
        try:
            if self._check_only:
                self._check()
            else:
                self._update()
        except ytdlp_runtime.YtdlpUnavailable as e:
            self.signals.done.emit(False, str(e), False)
        except Exception as e:  # noqa: BLE001 - 全例外を UI へ通知する
            self.signals.done.emit(False, f"予期しないエラー: {e}", False)

    def _check(self) -> None:
        self.signals.status.emit("最新版を確認しています...")
        current = ytdlp_runtime.installed_version()
        latest, _ = ytdlp_runtime.latest_release()
        if current is None:
            self.signals.done.emit(True, f"未取得です（最新版 {latest}）", False)
            return
        if ytdlp_runtime.parse_version(current) < ytdlp_runtime.parse_version(latest):
            self.signals.done.emit(True, f"更新があります: {current} → {latest}", False)
            return
        # 本体が最新でも EJS が欠けていると YouTube の制限が解除できない
        installed_dir = ytdlp_runtime.installed_dir()
        wanted = ytdlp_runtime.required_ejs_version(installed_dir) if installed_dir else None
        if wanted is not None and ytdlp_runtime.ejs_version(installed_dir) != wanted:
            self.signals.done.emit(True, f"最新です（{current}）／ EJS {wanted} が未取得です", False)
        else:
            self.signals.done.emit(True, f"最新です（{current}）", False)

    def _update(self) -> None:
        self.signals.status.emit("最新版を確認しています...")
        current = ytdlp_runtime.installed_version()
        latest, url = ytdlp_runtime.latest_release()
        if current is None or ytdlp_runtime.parse_version(current) < ytdlp_runtime.parse_version(latest):
            self.signals.status.emit(f"yt-dlp {latest} を取得しています...")
            target = ytdlp_runtime.install(latest, url)
            message = (
                f"yt-dlp {latest} を取得しました" if current is None
                else f"yt-dlp {latest} に更新しました"
            )
        else:
            target = ytdlp_runtime.installed_dir()
            message = f"最新です（{current}）"
        message += self._ensure_ejs(target)
        # 既にロード済みなら、差し替え（本体・EJS とも）の反映には再起動が要る。
        # yt_dlp は import 時に EJS の有無を見るため、あとから入れても効かない。
        self.signals.done.emit(True, message, core.YoutubeDL is not None)

    def _ensure_ejs(self, target) -> str:
        """EJS（yt-dlp-ejs）を yt-dlp に合わせて用意し、結果の一文を返す。

        失敗しても更新自体は成功扱いにする（EJS が無くてもダウンロードは
        できて、速度と一部の形式が落ちるだけ。ここで全体を失敗にすると
        本体の更新まで無かったことになってしまう）。
        """
        if target is None:
            return ""
        try:
            self.signals.status.emit("EJS（チャレンジ解決スクリプト）を確認しています...")
            version = ytdlp_runtime.install_ejs(target)
        except ytdlp_runtime.YtdlpUnavailable as e:
            return f"／ EJS の取得に失敗: {e}"
        return f"／ EJS {version}"
