# -*- coding: utf-8 -*-
"""MainWindow: テーブル UI と自動フローの結線。

ワーカーは 1 本だけ走らせる（実行中は開始系ボタンを無効化）。ワーカーからの
シグナルはすべてメインスレッドのスロットで受け、そこでのみモデルを更新する
（スレッド規約は workers.py を参照）。
"""
import logging
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QRect, QSettings, Qt, QThreadPool, QUrl
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

import core
from core import Status, Track

from .clipboard import resolve_paste_targets, selection_to_tsv
from .commands import ClearTitleCommand, EditArtistCommand, EditTitleCommand
from .logpanel import LogPanel, QtLogHandler, attach_handler, detach_handler
from .model import COL_ARTIST, COL_STATUS, COL_STEM, COL_TITLE, PERCENT_ROLE, TrackTableModel
from .player import PreviewPlayer, format_time
from .settings_dialog import SettingsDialog
from .workers import MODE_FETCH, MODE_FULL, MODE_INFER, MODE_WRITE, PipelineWorker


def apply_color_scheme(name: str) -> None:
    """アプリ全体のテーマを切り替える（"system" / "light" / "dark"）。

    Qt 6.8+ の QStyleHints.setColorScheme を使う。"system"（= Unknown）は
    OS のテーマ追従に戻す。未対応の Qt では何もしない。
    """
    hints = QGuiApplication.styleHints()
    if not hasattr(hints, "setColorScheme"):
        return
    scheme = {
        "light": Qt.ColorScheme.Light,
        "dark": Qt.ColorScheme.Dark,
    }.get(name, Qt.ColorScheme.Unknown)
    hints.setColorScheme(scheme)


def _as_bool(value) -> bool:
    """QSettings の値を bool へ正規化する（bool が文字列で返ることがあるため）。"""
    return value in (True, "true", "True", 1, "1")


def _ffmpeg_install_hint() -> str:
    """OS 別の ffmpeg インストール案内（警告ダイアログ用）。"""
    if sys.platform == "darwin":
        return "Homebrew の場合: brew install ffmpeg"
    return "winget の場合: winget install Gyan.FFmpeg（インストール後はアプリを再起動）"


class MainWindow(QMainWindow):
    """file_rename GUI のメインウィンドウ。"""

    def __init__(self, restore_settings: bool = True):
        super().__init__()
        self.setWindowTitle("file_rename GUI")
        self.resize(920, 560)

        self._model = TrackTableModel()
        self._pool = QThreadPool.globalInstance()
        self._cancel = threading.Event()
        self._running = False
        # タイトル編集・ペースト・Delete クリアの undo/redo
        self._undo = QUndoStack(self)
        # 設定ダイアログで変更できる動作設定（QSettings で永続化）
        self._out_dir: Path | None = None  # None = core.FILES_DIR
        self._batch_size: int = core.BATCH_SIZE
        self._expand_playlist: bool = False  # 混在 URL をリスト展開するか
        self._normalize: bool = True  # DL 時に音量ノーマライズを掛けるか（既定 ON）
        self._loudness: float = core.NORMALIZE_TARGET_I  # ノーマライズ基準値 (LUFS)
        self._trim_silence: bool = False  # 末尾の無音削除（試験的、既定 OFF）
        self._theme: str = "system"  # "system" / "light" / "dark"
        # ログパネルの表示レベル（"DEBUG"/"INFO"/"WARNING"/"ERROR"）。
        # フィルタはハンドラ側 1 箇所で行う（logpanel.attach_handler 参照）
        self._log_level: str = "WARNING"
        # LLM 接続設定の上書き（キーは core.ENV_KEYS。空文字 = .env の値を使う）
        self._llm_overrides: dict[str, str] = {}

        self._build_ui()
        # 試聴プレーヤ（ノーマライズ・無音削除の結果確認用）。QtMultimedia の
        # 実プレーヤは初回再生時に遅延生成される（gui/player.py 参照）
        self._player = PreviewPlayer(self)
        self._player.playing_changed.connect(self._on_playing_changed)
        self._player.position_changed.connect(self._on_player_position)
        self._player.duration_changed.connect(self._on_player_duration)
        # ウィンドウサイズ・列幅・トグル類の永続化。テストでは QSettings を
        # 汚さないよう restore_settings=False で復元/保存を無効化する。
        self._settings = QSettings("mv2title", "file_rename_gui") if restore_settings else None
        if self._settings is not None:
            self._restore_settings()
            apply_color_scheme(self._theme)
        # 復元後のログレベルをハンドラへ反映（restore 無効時は既定 WARNING）
        self._log_handler.setLevel(getattr(logging, self._log_level))
        # 復元した接続設定の上書きを環境変数へ反映（Config.from_env が読む）
        if any(self._llm_overrides.values()):
            core.apply_env_overrides(self._llm_overrides)
        if shutil.which("ffmpeg") is None:
            self.statusBar().showMessage(
                "警告: ffmpeg が見つかりません。mp3/wav 変換に必要です（PATH を確認してください）"
            )
            # ステータスバーだけでは見落とすため起動時に明示的に警告する。
            # テスト（restore_settings=False）ではモーダルを出さない
            if self._settings is not None:
                QMessageBox.warning(
                    self,
                    "ffmpeg が見つかりません",
                    "ffmpeg が見つかりません。ダウンロード後の音声変換・ノーマライズ・"
                    "無音削除に必要です。\n"
                    f"インストールして PATH を通してください。{_ffmpeg_install_hint()}",
                )
        else:
            self.statusBar().showMessage("準備完了")

    # -- UI 構築 -------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 上段: URL 入力 + 追加系ボタン
        top = QHBoxLayout()
        self._url_edit = QPlainTextEdit()
        self._url_edit.setPlaceholderText("URL を改行区切りで貼り付け（複数可）")
        self._url_edit.setFixedHeight(64)  # 3 行程度
        top.addWidget(self._url_edit, stretch=1)

        btn_col = QVBoxLayout()
        add_btn = QPushButton("追加")
        add_btn.clicked.connect(self._on_add_urls)
        list_btn = QPushButton("リスト読込")
        list_btn.setToolTip(
            "URL を 1 行ずつ記入したテキストファイルを読み込む（空行と # 始まりの行は無視）"
        )
        list_btn.clicked.connect(self._on_load_list)
        file_btn = QPushButton("ファイル追加")
        file_btn.clicked.connect(self._on_add_files)
        import_btn = QPushButton("files/ 取り込み")
        import_btn.clicked.connect(self._on_import_dir)
        for b in (add_btn, list_btn, file_btn, import_btn):
            btn_col.addWidget(b)
        top.addLayout(btn_col)
        root.addLayout(top)

        # ツールバー行: 実行 / 停止 / 形式 / 自動書き込み
        bar = QHBoxLayout()
        self._run_btn = QPushButton("▶ 実行")
        self._run_btn.clicked.connect(self._on_run)
        self._stop_btn = QPushButton("■ 停止")
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setEnabled(False)
        self._fetch_btn = QPushButton("情報取得")
        self._fetch_btn.setToolTip(
            "URL 行のタイトル・チャンネル名だけを取得する（ダウンロードはしない）。\n"
            "再生リストは動画ごとの行に展開されるので、DL 前に内容を確認できる"
        )
        self._fetch_btn.clicked.connect(self._on_fetch_info)
        bar.addWidget(self._run_btn)
        bar.addWidget(self._stop_btn)
        bar.addWidget(self._fetch_btn)
        bar.addSpacing(16)
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(core.SUPPORTED_FORMATS)
        self._fmt_combo.setCurrentText("mp3")
        bar.addWidget(self._fmt_combo)
        self._auto_write = QCheckBox("自動書き込み")
        self._auto_write.setChecked(True)
        bar.addWidget(self._auto_write)
        bar.addStretch(1)
        settings_btn = QPushButton("設定")
        settings_btn.clicked.connect(self._on_settings)
        bar.addWidget(settings_btn)
        root.addLayout(bar)

        # 上段の追加系ボタンと [設定] の幅を統一する（最長ラベル基準。
        # 縦積みの列は自動で最長幅に揃うが、[設定] は別レイアウトなので明示する）
        same_width = (add_btn, list_btn, file_btn, import_btn, settings_btn)
        width = max(b.sizeHint().width() for b in same_width)
        for b in same_width:
            b.setFixedWidth(width)

        # LLM 未接続で DL のみの縮退モードへ切り替わったときの警告バナー
        # （既定は非表示）。ステータスバー 1 行では見落とし、行が キュー の
        # まま残る理由が分からなくなるため、テーブルの直上に目立つ色で出す。
        # モーダルにはしない（DL 自体は続くので流れを止めない）
        self._banner = QFrame()
        self._banner.setVisible(False)
        # 黄系背景 + 濃色文字を固定（ダークテーマの白文字で読めなくならないように）
        self._banner.setStyleSheet(
            "QFrame { background-color: #faf4c7; border: 1px solid #c8b860;"
            " border-radius: 4px; }"
            " QLabel { color: #202020; border: none; }"
            " QPushButton { color: #202020; background: transparent; border: none; }"
        )
        banner_lay = QHBoxLayout(self._banner)
        banner_lay.setContentsMargins(8, 4, 4, 4)
        self._banner_label = QLabel("")
        self._banner_label.setWordWrap(True)
        banner_close = QPushButton("✕")
        banner_close.setFixedWidth(24)
        banner_close.setToolTip("この警告を閉じる")
        banner_close.clicked.connect(lambda: self._banner.setVisible(False))
        banner_lay.addWidget(self._banner_label, stretch=1)
        banner_lay.addWidget(banner_close)
        root.addWidget(self._banner)

        # 中央: テーブル
        self._view = _DropTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.setColumnWidth(COL_STEM, 240)
        self._view.setColumnWidth(COL_TITLE, 200)
        self._view.setColumnWidth(COL_ARTIST, 140)
        # Excel 風: F2 / 直接タイプ / ダブルクリック / 選択セルクリックで編集開始
        # （実行中は _set_running が NoEditTriggers に切り替える）
        self._edit_triggers = (
            QAbstractItemView.EditTrigger.AnyKeyPressed
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        self._view.setEditTriggers(self._edit_triggers)
        # 編集はコマンド化して QUndoStack へ積む（二重適用を避けるため
        # setModelData で model.setData を直接呼ばず、コマンド経由にする）
        self._view.setItemDelegateForColumn(COL_TITLE, _UndoEditDelegate(self))
        self._view.setItemDelegateForColumn(COL_ARTIST, _UndoEditDelegate(self))
        # 元タイトルは編集不可だが、本文の部分コピーのため読み取り専用エディタを開く
        self._view.setItemDelegateForColumn(COL_STEM, _ReadOnlyCopyDelegate(self))
        # 状態列: DL 中は進捗バーを描画（テキストの % だけでは視認しづらいため）
        self._view.setItemDelegateForColumn(COL_STATUS, _ProgressDelegate(self._view))
        # ヘッダクリックでソート（実行中は _set_running で無効化する）
        self._view.setSortingEnabled(True)
        # 右クリックメニュー
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        root.addWidget(self._view, stretch=1)

        # 試聴コントロール（処理結果の確認用）: ▶/⏸ ボタン + シークバー + 時間表示。
        # ▶ は選択行を再生（同じファイルなら一時停止/再開のトグル）、末尾試聴は
        # 無音削除の確認のため末尾 TAIL_SECS 秒だけ再生する
        preview = QHBoxLayout()
        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(36)
        self._play_btn.setToolTip(
            "選択行の音声を再生 / 一時停止（ノーマライズ・無音削除の結果確認用）"
        )
        self._play_btn.clicked.connect(self._on_preview)
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.setToolTip("ドラッグで再生位置を移動")
        self._seek_slider.sliderMoved.connect(self._on_seek)
        self._time_label = QLabel("0:00 / 0:00")
        self._duration_ms = 0  # 時間表示用（duration_changed で更新）
        self._tail_btn = QPushButton("♪ 末尾試聴")
        self._tail_btn.setToolTip(
            f"選択行の末尾 {PreviewPlayer.TAIL_SECS:.0f} 秒だけ再生する（無音削除の確認用）"
        )
        self._tail_btn.clicked.connect(self._on_preview_tail)
        preview.addWidget(self._play_btn)
        preview.addWidget(self._seek_slider, stretch=1)
        preview.addWidget(self._time_label)
        preview.addWidget(self._tail_btn)
        root.addLayout(preview)

        # 下段: 選択行への操作
        bottom = QHBoxLayout()
        reinfer_btn = QPushButton("選択行を再推定")
        reinfer_btn.clicked.connect(self._on_reinfer)
        write_btn = QPushButton("選択行を書き込み")
        write_btn.clicked.connect(self._on_write_selected)
        del_btn = QPushButton("行削除")
        del_btn.clicked.connect(self._on_delete_rows)
        artist_btn = QPushButton("チャンネル名→アーティスト")
        artist_btn.setToolTip(
            "選択行（未選択なら全行）のアーティスト欄にチャンネル名をそのまま入れる"
        )
        artist_btn.clicked.connect(self._on_fill_artists)
        for b in (reinfer_btn, artist_btn, write_btn, del_btn):
            bottom.addWidget(b)
        bottom.addStretch(1)
        log_btn = QPushButton("ログ")
        log_btn.setCheckable(True)
        bottom.addWidget(log_btn)
        root.addLayout(bottom)

        # 折りたたみ式のログパネル（既定は非表示）。ハンドラはワーカースレッド
        # からも呼ばれるため、QtLogHandler → QueuedConnection → パネルの構成
        # （スレッド規約は gui/logpanel.py 参照）
        self._log_handler = QtLogHandler()
        self._log_panel = LogPanel(self._log_handler)
        self._log_panel.setVisible(False)
        self._log_panel.setFixedHeight(120)
        attach_handler(self._log_handler)
        log_btn.toggled.connect(self._log_panel.setVisible)
        root.addWidget(self._log_panel)

        # 実行中に無効化するボタン群（1 本ルールの担保）
        self._busy_buttons = [
            self._run_btn,
            self._fetch_btn,
            reinfer_btn,
            write_btn,
            artist_btn,
            add_btn,
            list_btn,
            file_btn,
            import_btn,
            del_btn,
            self._play_btn,
            self._tail_btn,
        ]

        # undo/redo（Ctrl+Z / Ctrl+Y）。ウィンドウにアクションを載せる
        undo_action = self._undo.createUndoAction(self, "元に戻す")
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        redo_action = self._undo.createRedoAction(self, "やり直し")
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.addAction(undo_action)
        self.addAction(redo_action)

        # undo コマンドは行番号(int)を保持するため、行の並び・構成が変わったら
        # 過去のコマンドは無効（別の行に復元されてしまう）。行削除・差し替え
        # （rowsRemoved）とソート（layoutChanged）でスタックを破棄する。
        # 末尾への行追加(rowsInserted のみ)は既存行がずれないので対象外。
        self._model.rowsRemoved.connect(lambda *_: self._undo.clear())
        self._model.layoutChanged.connect(lambda *_: self._undo.clear())

    # -- 行追加系 ------------------------------------------------------------

    def add_urls(self, urls: list[str]) -> int:
        """URL ごとにプレースホルダ行を追加する。追加件数を返す。"""
        tracks = [Track(stem=u, url=u, status=Status.QUEUED) for u in urls]
        self._model.add_tracks(tracks)
        return len(tracks)

    def add_files(self, paths: list[Path]) -> int:
        """ローカルファイル行を追加する。追加件数を返す。

        既にリストへ入っているファイル（filepath が同じ行）はスキップする
        （[files/ 取り込み] を押すたびに同じ行が増えないように）。
        """
        existing = {
            t.filepath.resolve() for t in self._model.tracks() if t.filepath is not None
        }
        tracks = []
        for p in paths:
            key = p.resolve()
            if key in existing:
                continue
            existing.add(key)
            tracks.append(core.track_from_file(p))
        self._model.add_tracks(tracks)
        return len(tracks)

    def _on_add_urls(self) -> None:
        text = self._url_edit.toPlainText()
        urls = [line.strip() for line in text.splitlines() if line.strip()]
        if not urls:
            self.statusBar().showMessage("URL が入力されていません")
            return
        n = self.add_urls(urls)
        self._url_edit.clear()
        self.statusBar().showMessage(f"{n} 件の URL を追加しました")

    def _on_load_list(self) -> None:
        """URL を 1 行ずつ記入したテキストファイルを読み込んで行追加する。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "URL リストを読み込み", "", "URL リスト (*.txt);;すべてのファイル (*)"
        )
        if path:
            self._add_url_list(Path(path))

    def _add_url_list(self, path: Path) -> int:
        """URL リストファイルを読み込んで行追加する。追加件数を返す（失敗は 0）。"""
        try:
            urls = core.read_url_list(path)
        except OSError as e:
            self.statusBar().showMessage(f"URL リストを読み込めません: {e}")
            return 0
        if not urls:
            self.statusBar().showMessage(f"有効な URL がありません: {path.name}")
            return 0
        n = self.add_urls(urls)
        self.statusBar().showMessage(f"{path.name} から {n} 件の URL を追加しました")
        return n

    def _on_add_files(self) -> None:
        pattern = "音声ファイル (" + " ".join(f"*{e}" for e in core.SUPPORTED_EXTS) + ")"
        paths, _ = QFileDialog.getOpenFileNames(self, "音声ファイルを追加", "", pattern)
        if not paths:
            return
        n = self.add_files([Path(p) for p in paths])
        skipped = len(paths) - n
        msg = f"{n} 件のファイルを追加しました"
        if skipped:
            msg += f"（追加済み {skipped} 件はスキップ）"
        self.statusBar().showMessage(msg)

    def _on_import_dir(self) -> None:
        files = core.list_music_files()
        if not files:
            self.statusBar().showMessage(f"{core.FILES_DIR} に音声ファイルがありません")
            return
        n = self.add_files(files)
        if n:
            self.statusBar().showMessage(f"files/ から {n} 件を取り込みました")
        else:
            self.statusBar().showMessage("files/ のファイルはすべて取り込み済みです")

    # -- パイプライン起動 ----------------------------------------------------

    def _confirm_ffmpeg(self) -> bool:
        """DL 実行前の ffmpeg 確認。見つからなければ警告し、続行するか尋ねる。

        変換・ノーマライズは失敗するが DL 自体は動くため、続行の選択肢は残す。
        テスト（restore_settings=False）ではダイアログを出さず続行する。
        """
        if shutil.which("ffmpeg") is not None:
            return True
        if self._settings is None:
            return True
        ret = QMessageBox.warning(
            self,
            "ffmpeg が見つかりません",
            "ffmpeg が見つからないため、ダウンロード後の音声変換・ノーマライズは"
            "失敗します。\n"
            f"{_ffmpeg_install_hint()}\n\nこのまま実行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes

    def _on_run(self) -> None:
        tracks = self._model.tracks()
        if not tracks:
            self.statusBar().showMessage("処理対象がありません")
            return
        if not self._confirm_ffmpeg():
            self.statusBar().showMessage("ffmpeg 未検出のため実行を中止しました")
            return
        worker = PipelineWorker(
            tracks,
            mode=MODE_FULL,
            fmt=self._fmt_combo.currentText(),
            auto_write=self._auto_write.isChecked(),
            cancel=self._reset_cancel(),
            batch_size=self._batch_size,
            out_dir=self._out_dir,
            expand_playlist=self._expand_playlist,
            normalize=self._normalize,
            loudness=self._loudness,
            trim_silence=self._trim_silence,
        )
        self._start(worker, "実行中...")

    def _on_reinfer(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.statusBar().showMessage("行が選択されていません")
            return
        tracks = [self._model.track_at(r) for r in rows]
        worker = PipelineWorker(
            tracks,
            mode=MODE_INFER,
            force=True,
            cancel=self._reset_cancel(),
            batch_size=self._batch_size,
        )
        self._start(worker, "再推定中...")

    def _on_write_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.statusBar().showMessage("行が選択されていません")
            return
        tracks = [self._model.track_at(r) for r in rows]
        worker = PipelineWorker(tracks, mode=MODE_WRITE, cancel=self._reset_cancel())
        self._start(worker, "書き込み中...")

    def _on_fetch_info(self) -> None:
        """URL 行のメタデータだけを取得する（DL なし。再生リストの内容確認用）。"""
        tracks = self._model.tracks()
        # 対象は未取得のプレースホルダ行のみ（取得済み・ローカル行は対象外）
        if not any(t.url is not None and t.filepath is None and t.stem == t.url for t in tracks):
            self.statusBar().showMessage("情報を取得できる URL 行がありません")
            return
        worker = PipelineWorker(
            tracks,
            mode=MODE_FETCH,
            cancel=self._reset_cancel(),
            expand_playlist=self._expand_playlist,
        )
        self._start(worker, "情報取得中...")

    def _start(self, worker: PipelineWorker, message: str) -> None:
        if self._running:
            self.statusBar().showMessage("処理が実行中です")
            return
        # queued connection でメインスレッドに乗せる
        worker.signals.track_updated.connect(
            self._model.refresh_track, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.tracks_ready.connect(
            self._model.replace_track, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.progress.connect(
            self._model.set_percent, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.error.connect(self._on_worker_error, Qt.ConnectionType.QueuedConnection)
        worker.signals.connection_failed.connect(
            self._on_connection_failed, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.write_summary.connect(
            self._on_write_summary, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.stage_summary.connect(
            self._on_stage_summary, Qt.ConnectionType.QueuedConnection
        )
        worker.signals.finished.connect(self._on_worker_finished, Qt.ConnectionType.QueuedConnection)
        self._set_running(True)
        self.statusBar().showMessage(message)
        self._pool.start(worker)

    # -- ワーカーのシグナル受信（メインスレッド）----------------------------

    def _on_worker_error(self, message: str) -> None:
        self.statusBar().showMessage(f"エラー: {message}")

    def _on_connection_failed(self, message: str) -> None:
        """LLM 未接続 → DL のみの縮退モードへ切り替わったときの通知。

        ステータスバーは見落としやすいので、テーブル上部のバナーにも出す
        （次の実行開始時に自動で消える。✕ でも閉じられる）。
        """
        self.statusBar().showMessage(
            f"LLM エンドポイントに接続できません。DL のみ実行します（{message}）"
        )
        self._banner_label.setText(
            "LLM エンドポイントに接続できないため、ダウンロードのみ実行しました。"
            "行は「キュー」のまま残っています。サーバ起動後（または [設定] の接続設定を"
            f"確認後）にもう一度 [▶ 実行] すると推定から続きが処理されます。（{message}）"
        )
        self._banner.setVisible(True)

    def _on_write_summary(self, done: int, skipped: int, errors: int) -> None:
        """書き込み結果の集計をステータスバーに表示（完了が分かりづらい問題の対策）。"""
        self.statusBar().showMessage(
            f"書き込み: 完了 {done} 件 / スキップ {skipped} 件 / 失敗 {errors} 件"
        )

    def _on_stage_summary(self, stage: str, done: int, errors: int) -> None:
        """情報取得 / DL 段の集計を表示する（失敗が「完了」に埋もれないように）。"""
        self.statusBar().showMessage(f"{stage}: 完了 {done} 件 / 失敗 {errors} 件")

    def _on_worker_finished(self) -> None:
        self._set_running(False)
        if self.statusBar().currentMessage().endswith("中..."):
            self.statusBar().showMessage("完了")

    # -- 設定 ---------------------------------------------------------------

    def _on_settings(self) -> None:
        dlg = SettingsDialog(
            self,
            out_dir=self._out_dir,
            fmt=self._fmt_combo.currentText(),
            batch_size=self._batch_size,
            auto_write=self._auto_write.isChecked(),
            expand_playlist=self._expand_playlist,
            normalize=self._normalize,
            loudness=self._loudness,
            trim_silence=self._trim_silence,
            theme=self._theme,
            log_level=self._log_level,
            llm_overrides=dict(self._llm_overrides),
        )
        if not dlg.exec():
            return
        self.apply_settings(dlg.values())

    def apply_settings(self, values: dict) -> None:
        """設定ダイアログの値を反映し、QSettings へ保存する。"""
        out_dir = values["out_dir"]
        # 既定の FILES_DIR と同じなら None（=core 既定）として扱う
        self._out_dir = None if out_dir == core.FILES_DIR else out_dir
        self._batch_size = int(values["batch_size"])
        self._expand_playlist = bool(values.get("expand_playlist", False))
        self._normalize = bool(values.get("normalize", True))
        self._loudness = float(values.get("loudness", core.NORMALIZE_TARGET_I))
        self._trim_silence = bool(values.get("trim_silence", False))
        new_theme = str(values.get("theme", "system"))
        if new_theme != self._theme:
            self._theme = new_theme
            apply_color_scheme(new_theme)
        # ログパネルの表示レベルをハンドラへ反映（フィルタはハンドラ 1 箇所）
        self._log_level = str(values.get("log_level", "WARNING"))
        self._log_handler.setLevel(getattr(logging, self._log_level))
        # LLM 接続設定の上書き（キー無し = ダイアログ以外からの呼び出しは維持）
        llm = values.get("llm_overrides")
        if llm is not None:
            self._llm_overrides = {k: str(llm.get(k, "")) for k in core.ENV_KEYS}
            core.apply_env_overrides(self._llm_overrides)
        self._fmt_combo.setCurrentText(values["fmt"])
        self._auto_write.setChecked(bool(values["auto_write"]))
        if self._settings is not None:
            self._settings.setValue(
                "options/out_dir", str(self._out_dir) if self._out_dir else ""
            )
            self._settings.setValue("options/batch_size", self._batch_size)
            self._settings.setValue("options/expand_playlist", self._expand_playlist)
            self._settings.setValue("options/normalize", self._normalize)
            self._settings.setValue("options/loudness", self._loudness)
            self._settings.setValue("options/trim_silence", self._trim_silence)
            self._settings.setValue("options/theme", self._theme)
            self._settings.setValue("options/log_level", self._log_level)
            # 接続設定の上書き（API キーも QSettings に平文で入る。個人利用前提）
            for key in core.ENV_KEYS:
                self._settings.setValue(
                    f"options/llm_{key.lower()}", self._llm_overrides.get(key, "")
                )
        self.statusBar().showMessage("設定を保存しました")

    # -- 下段操作 ------------------------------------------------------------

    def _on_delete_rows(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        self._model.remove_rows(rows)
        self.statusBar().showMessage(f"{len(rows)} 行を削除しました（ファイルは残ります）")

    # -- 試聴（プレビュー再生）----------------------------------------------

    def _on_preview(self) -> None:
        """▶ ボタン: 選択行を再生する。同じファイルの再生中は一時停止、一時停止中は再開。"""
        track = self._preview_target()
        if track is None:
            # 選択が無くても再生中/一時停止中なら現在の曲をトグルする
            if self._player.is_playing:
                self._player.pause()
            elif self._player.is_paused:
                self._player.resume()
            else:
                self.statusBar().showMessage("試聴できる行がありません（ファイル未取得の行は不可）")
            return
        path = Path(track.filepath)
        if not path.exists():
            self.statusBar().showMessage(f"ファイルが見つかりません: {path}")
            return
        if self._player.current_path == path:
            # 同じファイルなら一時停止/再開のトグル（先頭からやり直さない）
            if self._player.is_playing:
                self._player.pause()
                return
            if self._player.is_paused:
                self._player.resume()
                return
        self._player.play(path)
        self.statusBar().showMessage(f"試聴中: {path.name}")

    def _on_preview_tail(self) -> None:
        """選択行の末尾だけ再生する（無音削除の確認用）。"""
        track = self._preview_target()
        if track is None:
            self.statusBar().showMessage("試聴できる行がありません（ファイル未取得の行は不可）")
            return
        path = Path(track.filepath)
        if not path.exists():
            self.statusBar().showMessage(f"ファイルが見つかりません: {path}")
            return
        self._player.play(path, tail_only=True)
        self.statusBar().showMessage(f"試聴中（末尾のみ）: {path.name}")

    def _preview_target(self) -> Track | None:
        """試聴対象: 選択行のうち filepath を持つ最初の行（無ければ None）。"""
        return next(
            (
                self._model.track_at(r)
                for r in self._selected_rows()
                if self._model.track_at(r).filepath is not None
            ),
            None,
        )

    def _on_seek(self, position_ms: int) -> None:
        """シークバーのドラッグで再生位置を移動する。"""
        self._player.seek(position_ms)

    def _on_playing_changed(self, playing: bool) -> None:
        """再生状態に合わせて ▶/⏸ ボタンの表示を切り替える。"""
        self._play_btn.setText("⏸" if playing else "▶")
        # 一時停止中は「試聴中」の表示を保つ（停止・再生終了時のみ戻す）
        if (
            not playing
            and not self._player.is_paused
            and self.statusBar().currentMessage().startswith("試聴中")
        ):
            self.statusBar().showMessage("準備完了")

    def _on_player_position(self, position_ms: int) -> None:
        """再生位置をシークバーと時間表示へ反映する（ドラッグ中は上書きしない）。"""
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(position_ms)
        self._update_time_label(position_ms)

    def _on_player_duration(self, duration_ms: int) -> None:
        """曲の長さが確定したらシークバーの範囲と時間表示を更新する。"""
        self._duration_ms = duration_ms
        self._seek_slider.setRange(0, max(0, duration_ms))
        self._update_time_label(self._seek_slider.value())

    def _update_time_label(self, position_ms: int) -> None:
        self._time_label.setText(
            f"{format_time(position_ms)} / {format_time(self._duration_ms)}"
        )

    # -- 補助 ---------------------------------------------------------------

    def _selected_rows(self) -> list[int]:
        return sorted({idx.row() for idx in self._view.selectionModel().selectedRows()})

    def _reset_cancel(self) -> threading.Event:
        self._cancel = threading.Event()
        return self._cancel

    def _set_running(self, running: bool) -> None:
        self._running = running
        if running:
            # 実行中は対象ファイルが変換で書き換わり得るため試聴を止める
            self._player.stop()
            # 前回の縮退モード警告は再実行で解消され得るため自動で消す
            self._banner.setVisible(False)
        for b in self._busy_buttons:
            b.setEnabled(not running)
        self._seek_slider.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        # 実行中はソート禁止（ワーカーの行同一性・進捗 dict を壊さないため）
        self._view.setSortingEnabled(not running)
        # 実行中はセル編集も禁止（ワーカーが Track を書き換え中のため。
        # ペースト/Delete は各メソッド側でガード済み、ここは直接編集のガード）
        self._view.setEditTriggers(
            self._edit_triggers if not running else QAbstractItemView.EditTrigger.NoEditTriggers
        )

    def _on_stop(self) -> None:
        self._cancel.set()
        self.statusBar().showMessage("停止を要求しました...")

    # -- Excel 風操作: コピー / ペースト / Delete / 編集コマンド化 -----------

    def copy_selection(self) -> None:
        """選択セル範囲を TSV でクリップボードへコピーする。"""
        indexes = self._view.selectionModel().selectedIndexes()
        tsv = selection_to_tsv(self._model, indexes)
        if tsv:
            QGuiApplication.clipboard().setText(tsv)

    def paste_clipboard(self) -> None:
        """クリップボードの TSV を現在セルを左上として貼り付ける。

        編集可能列（推定タイトル）に落ちるセルのみ反映する。反映は
        EditTitleCommand として 1 つの macro にまとめ、1 回の undo で戻す。
        """
        if self._running:
            return  # 実行中はワーカーが Track を触るため貼り付けを抑止
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        start = self._view.currentIndex()
        if not start.isValid():
            return
        self.paste_tsv_via_commands(start, text)

    def paste_tsv_via_commands(self, start_index, text: str) -> int:
        """TSV を Edit 系コマンドの macro として貼り付ける。反映セル数を返す。

        clipboard.resolve_paste_targets で「編集可能列（タイトル/アーティスト）
        のみ」の対象を求め、実際の適用はコマンド経由（＝undo 可能）にする。
        複数セルは 1 つの macro にまとめ、1 回の undo で全部戻す。
        """
        edits = resolve_paste_targets(self._model, start_index, text)
        if not edits:
            return 0
        self._undo.beginMacro("貼り付け")
        for row, col, value in edits:
            self.push_edit(row, col, value)
        self._undo.endMacro()
        return len(edits)

    def push_edit(self, row: int, col: int, value: str) -> None:
        """デリゲート確定を列に応じた Edit 系コマンドとしてスタックへ積む。"""
        if col == COL_ARTIST:
            self._undo.push(EditArtistCommand(self._model, row, value))
        else:
            self._undo.push(EditTitleCommand(self._model, row, value))

    def _on_fill_artists(self) -> None:
        """選択行（未選択なら全行）のアーティスト欄にチャンネル名をコピーする。

        推定はしない（ユーザー要望）。undo は 1 回でまとめて戻る。
        """
        if self._running:
            return
        rows = self._selected_rows() or list(range(self._model.rowCount()))
        targets = [r for r in rows if self._model.track_at(r).channel]
        if not targets:
            self.statusBar().showMessage("チャンネル名を持つ行がありません")
            return
        self._undo.beginMacro("チャンネル名をアーティストへ")
        for row in targets:
            channel = self._model.track_at(row).channel or ""
            self._undo.push(EditArtistCommand(self._model, row, channel))
        self._undo.endMacro()
        # 対象行を選択状態にしてフォーカスをテーブルへ戻す。ボタン押下で
        # フォーカスが外れると選択が非アクティブ色（ダークテーマではほぼ
        # 不可視）になり「解除された」ように見えるため、そのまま
        # [選択行を書き込み] へ進める状態を明示的に作る。
        self._select_rows(targets)
        self.statusBar().showMessage(f"{len(targets)} 行のアーティスト欄にチャンネル名を入れました")

    def _select_rows(self, rows: list[int]) -> None:
        """指定行を行選択し、テーブルへフォーカスを戻す。"""
        sel = self._view.selectionModel()
        sel.clearSelection()
        flags = (
            QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        )
        for row in rows:
            sel.select(self._model.index(row, 0), flags)
        self._view.setFocus()

    def clear_selected_titles(self) -> None:
        """選択行の推定タイトルをクリアする（Delete）。macro で 1 回 undo。"""
        if self._running:
            return
        rows = self._selected_rows()
        if not rows:
            return
        self._undo.beginMacro("タイトルをクリア")
        for row in rows:
            self._undo.push(ClearTitleCommand(self._model, row))
        self._undo.endMacro()
        self.statusBar().showMessage(f"{len(rows)} 行のタイトルをクリアしました")

    # -- 右クリックメニュー --------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        menu = self.build_context_menu()
        menu.exec(self._view.viewport().mapToGlobal(pos))

    def build_context_menu(self) -> QMenu:
        """テーブル上の右クリックメニューを構築して返す（テストで検査可能）。

        パイプライン実行中は再推定/書き込み/行削除を無効化する。
        「URL をブラウザで開く」は url を持つ行がある場合のみ有効。
        """
        menu = QMenu(self._view)
        rows = self._selected_rows()

        reinfer = menu.addAction("選択行を再推定")
        reinfer.triggered.connect(self._on_reinfer)
        write = menu.addAction("選択行を書き込み")
        write.triggered.connect(self._on_write_selected)
        delete = menu.addAction("行削除")
        delete.triggered.connect(self._on_delete_rows)
        retry = menu.addAction("エラー行を再試行待ちに戻す")
        retry.triggered.connect(self._on_reset_errors)
        menu.addSeparator()
        open_url = menu.addAction("URL をブラウザで開く")
        open_url.triggered.connect(self._on_open_urls)

        # 実行中は破壊的/パイプライン操作を無効化
        for act in (reinfer, write, delete):
            act.setEnabled(bool(rows) and not self._running)
        # 選択行に ERROR がある場合のみ有効
        has_error = any(self._model.track_at(r).status is Status.ERROR for r in rows)
        retry.setEnabled(has_error and not self._running)
        # url を持つ選択行が 1 つでもあれば有効
        has_url = any(self._model.track_at(r).url for r in rows)
        open_url.setEnabled(has_url)
        return menu

    def _on_reset_errors(self) -> None:
        """選択中のエラー行を再試行待ちへ戻す（再処理は [▶ 実行] などで）。"""
        if self._running:
            return
        rows = [
            r for r in self._selected_rows() if self._model.track_at(r).status is Status.ERROR
        ]
        for row in rows:
            self._model.reset_error(row)
        if rows:
            self.statusBar().showMessage(
                f"{len(rows)} 行を再試行待ちに戻しました（[▶ 実行] で再処理されます）"
            )

    def _on_open_urls(self) -> None:
        """選択行の url をブラウザで開く。"""
        for row in self._selected_rows():
            url = self._model.track_at(row).url
            if url:
                QDesktopServices.openUrl(QUrl(url))

    # -- QSettings による永続化 ---------------------------------------------

    def _restore_settings(self) -> None:
        """ウィンドウサイズ・列幅・トグル類を復元する。"""
        s = self._settings
        assert s is not None
        geometry = s.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        header_state = s.value("table/header")
        # 列数が変わった（列を追加/削除した）後は、古い saveState を復元すると
        # 幅が 1 列ずれるため、保存時の列数が一致するときだけ復元する。
        saved_cols = s.value("table/columns")
        if header_state is not None and str(saved_cols) == str(self._model.columnCount()):
            self._view.horizontalHeader().restoreState(header_state)
        auto = s.value("options/auto_write")
        if auto is not None:
            self._auto_write.setChecked(_as_bool(auto))
        fmt = s.value("options/format")
        if fmt in core.SUPPORTED_FORMATS:
            self._fmt_combo.setCurrentText(fmt)
        out_dir = s.value("options/out_dir")
        if out_dir:
            self._out_dir = Path(str(out_dir))
        batch = s.value("options/batch_size")
        if batch is not None:
            try:
                self._batch_size = max(1, int(batch))
            except (TypeError, ValueError):
                pass
        expand = s.value("options/expand_playlist")
        if expand is not None:
            self._expand_playlist = _as_bool(expand)
        normalize = s.value("options/normalize")
        if normalize is not None:
            self._normalize = _as_bool(normalize)
        loudness = s.value("options/loudness")
        if loudness is not None:
            try:
                self._loudness = float(loudness)
            except (TypeError, ValueError):
                pass
        trim = s.value("options/trim_silence")
        if trim is not None:
            self._trim_silence = _as_bool(trim)
        theme = s.value("options/theme")
        if theme in ("system", "light", "dark"):
            self._theme = theme
        log_level = s.value("options/log_level")
        if log_level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            self._log_level = log_level
        self._llm_overrides = {
            key: str(s.value(f"options/llm_{key.lower()}") or "") for key in core.ENV_KEYS
        }

    def _save_settings(self) -> None:
        s = self._settings
        if s is None:
            return
        s.setValue("window/geometry", self.saveGeometry())
        s.setValue("table/header", self._view.horizontalHeader().saveState())
        s.setValue("table/columns", self._model.columnCount())
        s.setValue("options/auto_write", self._auto_write.isChecked())
        s.setValue("options/format", self._fmt_combo.currentText())

    def closeEvent(self, event) -> None:
        self._player.stop()
        self._save_settings()
        # ログハンドラを外す（テスト等で多重生成してもロガーに蓄積しないように）
        detach_handler(self._log_handler)
        super().closeEvent(event)

    # -- ドラッグ&ドロップ（_DropTableView から委譲）------------------------

    def handle_dropped_paths(self, paths: list[Path]) -> None:
        """ドロップされた音声ファイルは行追加、.txt は URL リストとして読み込む。"""
        supported = [p for p in paths if p.suffix.lower() in core.SUPPORTED_EXTS]
        url_lists = [p for p in paths if p.suffix.lower() == ".txt"]
        if not supported and not url_lists:
            self.statusBar().showMessage("対応する音声ファイル・URL リスト(.txt)がありません")
            return
        n = self.add_files(supported) if supported else 0
        n += sum(self._add_url_list(p) for p in url_lists)
        if n:
            self.statusBar().showMessage(f"ドロップで {n} 件を追加しました")
        elif supported and not url_lists:
            # URL リストの失敗時は _add_url_list が理由を表示済みなので上書きしない
            self.statusBar().showMessage("ドロップされたファイルはすべて追加済みです")


class _DropTableView(QTableView):
    """対応拡張子のローカルファイルをドロップで行追加できる QTableView。"""

    def __init__(self, window: MainWindow):
        super().__init__()
        self._window = window
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        urls = event.mimeData().urls()
        paths = [Path(u.toLocalFile()) for u in urls if u.isLocalFile()]
        if paths:
            self._window.handle_dropped_paths(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def keyPressEvent(self, event) -> None:
        """Excel 風のキーボード操作を処理する。

        - Ctrl+C: 選択セル範囲を TSV でコピー
        - Ctrl+V: クリップボードの TSV を貼り付け（タイトル列のみ）
        - Delete: 選択行の推定タイトルをクリア
        それ以外は既定処理（F2/直接タイプでの編集開始・Enter 移動等）へ委譲。
        """
        if event.matches(QKeySequence.StandardKey.Copy):
            self._window.copy_selection()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self._window.paste_clipboard()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._window.clear_selected_titles()
            event.accept()
            return
        super().keyPressEvent(event)


class _ProgressDelegate(QStyledItemDelegate):
    """状態列のデリゲート。DL 中の行には進捗バーを自前で描画する。

    QStyle の CE_ProgressBar はテーマ（特に Windows のダークテーマ）次第で
    文字が読みづらくなるため、スタイルに依存しない自前描画にする:
    白地のグルーヴ + 青のチャンク + 濃色のパーセント文字。
    モデルの PERCENT_ROLE が数値を返す間（= DOWNLOADING で進捗既知）だけ
    バーを描き、それ以外は既定の描画（状態テキスト + 背景色）に任せる。
    """

    _BORDER = QColor(140, 140, 140)
    _GROOVE = QColor(252, 252, 252)
    _CHUNK = QColor(120, 180, 250)
    _TEXT = QColor(32, 32, 32)

    def paint(self, painter, option, index) -> None:
        percent = index.data(PERCENT_ROLE)
        if percent is None:
            super().paint(painter, option, index)
            return
        painter.save()
        # 行の背景色（DL 中の薄青）を先に塗って、他の列と見た目を揃える
        bg = index.data(Qt.ItemDataRole.BackgroundRole)
        if bg is not None:
            painter.fillRect(option.rect, bg)
        rect = option.rect.adjusted(3, 4, -4, -5)
        # グルーヴ（白地）と枠
        painter.setPen(self._BORDER)
        painter.setBrush(self._GROOVE)
        painter.drawRect(rect)
        # チャンク（進捗分の青）
        ratio = max(0.0, min(percent, 100.0)) / 100.0
        chunk = QRect(rect.x() + 1, rect.y() + 1, int((rect.width() - 1) * ratio), rect.height() - 1)
        painter.fillRect(chunk, self._CHUNK)
        # パーセント文字（チャンク/グルーヴどちらの上でも読める濃色）。
        # 表示文字列はモデルの状態列テキスト（「DL中 2/5 45%」等）を使う
        painter.setPen(self._TEXT)
        text = index.data(Qt.ItemDataRole.DisplayRole) or f"DL中 {int(percent)}%"
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(text))
        painter.restore()


class _UndoEditDelegate(QStyledItemDelegate):
    """推定タイトル列のデリゲート。確定を EditTitleCommand としてスタックへ積む。

    既定の setModelData は model.setData を直接呼ぶが、それだと undo スタックを
    経由しない。ここで setData を呼ばず MainWindow.push_edit へ回すことで、
    「UI からの編集はすべて QUndoStack に積む」構造にする（二重適用も防ぐ）。
    """

    def __init__(self, window: MainWindow):
        super().__init__(window)
        self._window = window

    def setModelData(self, editor, model, index) -> None:
        # エディタから確定値を取り出す（QLineEdit 前提だが汎用に property 経由）
        value = editor.property(editor.metaObject().userProperty().name())
        text = "" if value is None else str(value)
        self._window.push_edit(index.row(), index.column(), text)


class _ReadOnlyCopyDelegate(QStyledItemDelegate):
    """元タイトル列のデリゲート。本文の部分選択・コピーだけを許す。

    セルをダブルクリック等で開くと読み取り専用の QLineEdit が出るので、
    ユーザーは一部分を選択して Ctrl+C できるが、値は書き換わらない
    （setModelData を no-op にしているため）。
    """

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setReadOnly(True)
        return editor

    def setModelData(self, editor, model, index) -> None:
        # 読み取り専用なので何も書き戻さない
        return
