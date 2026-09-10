# -*- coding: utf-8 -*-
"""MainWindow: テーブル UI と自動フローの結線。

ワーカーは 1 本だけ走らせる（実行中は開始系ボタンを無効化）。ワーカーからの
シグナルはすべてメインスレッドのスロットで受け、そこでのみモデルを更新する
（スレッド規約は workers.py を参照）。
"""
import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QItemSelectionModel, QRect, QSettings, Qt, QThreadPool, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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
import ytdlp_runtime
from core import Status, Track

from .clipboard import resolve_paste_targets, selection_to_tsv
from .commands import (
    ClearTitleCommand,
    EditAlbumCommand,
    EditArtistCommand,
    EditTitleCommand,
)
from .logpanel import LogPanel, QtLogHandler, attach_handler, detach_handler
from .model import (
    COL_ALBUM,
    COL_ARTIST,
    COL_STATUS,
    COL_STEM,
    COL_TITLE,
    EDITABLE_COLUMNS,
    PERCENT_ROLE,
    TrackTableModel,
)
from .player import PreviewPlayer, format_time
from .settings_dialog import SettingsDialog
from .textmatch import compile_pattern, has_match, replace_in
from .workers import (
    MODE_FETCH,
    MODE_FULL,
    MODE_INFER,
    MODE_WRITE,
    PipelineWorker,
    YtdlpWorker,
)


# 選択行のハイライト色。テーマ既定の色は淡く、特に非アクティブ時（フォーカスが
# 他ウィジェットにあるとき）は色付き行の上でほとんど見えないため固定する
_SELECTION_BG = QColor(47, 111, 208)  # 濃い青
_SELECTION_TEXT = QColor(255, 255, 255)
# フィルハンドル（選択セル右下の小さな四角）の見た目。白地 + 濃紺の枠にする
# ことで、選択行（濃い青）の上でも未選択行（白/淡色）の上でも見える
_FILL_HANDLE_PX = 9
_FILL_HANDLE_BG = QColor(255, 255, 255)
_FILL_HANDLE_BORDER = QColor(20, 60, 120)
_FILL_PREVIEW_PEN = QColor(47, 111, 208)


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


def _deno_install_hint() -> str:
    """OS 別の deno インストール案内（警告ダイアログ用）。"""
    if sys.platform == "darwin":
        return "Homebrew の場合: brew install deno"
    return "winget の場合: winget install DenoLand.Deno（インストール後はアプリを再起動）"


def file_manager_name() -> str:
    """OS 標準のファイルマネージャー名（メニュー文言用）。"""
    if sys.platform == "darwin":
        return "Finder"
    if sys.platform == "win32":
        return "エクスプローラー"
    return "ファイルマネージャー"


def reveal_in_file_manager(path: Path) -> bool:
    """ファイルをファイルマネージャーで「選択した状態」で表示する。成否を返す。

    親フォルダを開くだけだと、同じフォルダに似た名前の曲が並んでいるときに
    どれか分からない。mac の `open -R` / Windows の `explorer /select,` は
    どちらも対象ファイルを選択して開くので、それを使う（Linux 等には相当が
    無いので親フォルダを開くだけにする）。
    """
    path = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)], check=True)
            return True
        if sys.platform == "win32":
            # explorer は成功しても終了コード 1 を返すため check はしない
            subprocess.run(["explorer", f"/select,{path}"])
            return True
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent))))
    except (OSError, subprocess.SubprocessError):
        return False


def replace_shortcut() -> QKeySequence:
    """置換欄へ移るショートカット（Windows は Ctrl+H、mac は ⌃H = Control+H）。

    Qt の "Ctrl" は mac では ⌘ に読み替えられるため、Qt 標準の Replace
    （Ctrl+H）のままだと mac では ⌘H = OS 予約の「アプリを隠す」になる。
    mac の Control キーは Qt では "Meta" なので、明示的に Meta+H を使う。
    なお mac のテキスト欄では ⌃H が Backspace にも割り当てられているが、
    QLineEdit / QPlainTextEdit は ShortcutOverride で横取りしない（実機の
    cocoa で確認済み）ので、検索欄にフォーカスがあってもこちらが効く。
    """
    if sys.platform == "darwin":
        return QKeySequence("Meta+H")
    return QKeySequence(QKeySequence.StandardKey.Replace)


# 置換バーの「対象列」コンボの選択肢（表示名, 列番号のタプル）
_REPLACE_TARGETS = (
    ("推定タイトル・アーティスト・アルバム", EDITABLE_COLUMNS),
    ("推定タイトル", (COL_TITLE,)),
    ("アーティスト", (COL_ARTIST,)),
    ("アルバム", (COL_ALBUM,)),
)


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
        # 直近の add_files でタグを読めた件数（ステータスバーの文言用）
        self._last_tagged = 0
        # タイトル編集・ペースト・Delete クリアの undo/redo
        self._undo = QUndoStack(self)
        # 設定ダイアログで変更できる動作設定（QSettings で永続化）
        self._out_dir: Path | None = None  # None = core.FILES_DIR
        self._batch_size: int = core.BATCH_SIZE
        # 推定リクエストに構造化出力(response_format)を付けるか（既定 ON）
        self._use_schema: bool = core.USE_SCHEMA
        self._max_downloads: int = core.MAX_DOWNLOADS  # URL 行の同時 DL 本数
        self._expand_playlist: bool = False  # 混在 URL をリスト展開するか
        self._normalize: bool = True  # DL 時に音量ノーマライズを掛けるか（既定 ON）
        self._loudness: float = core.NORMALIZE_TARGET_I  # ノーマライズ基準値 (LUFS)
        self._trim_silence: bool = False  # 末尾の無音削除（試験的、既定 OFF）
        self._best_quality: bool = False  # 取得フォーマットを音質優先で選ぶか
        # 変換後のビットレート（None = ffmpeg 既定 / "source" = 取得元と同じ /
        # 整数 = kbps 固定。core.parse_bitrate 参照）
        self._audio_bitrate: int | str | None = None
        # YouTube Music の URL は推定を挟まずメタデータの曲名をそのまま使う
        self._ytmusic_direct: bool = True
        self._theme: str = "system"  # "system" / "light" / "dark"
        # ログパネルの表示レベル（"DEBUG"/"INFO"/"WARNING"/"ERROR"）。
        # フィルタはハンドラ側 1 箇所で行う（logpanel.attach_handler 参照）
        self._log_level: str = "WARNING"
        # LLM 接続設定の上書き（キーは core.ENV_KEYS。空文字 = .env の値を使う）
        self._llm_overrides: dict[str, str] = {}
        # 検索（行の絞り込み）の状態。_filter_active は「今どれかの行を隠して
        # いるか」で、隠していないときに再適用を丸ごと省くために持つ
        self._search_text: str = ""
        self._filter_active: bool = False
        # 置換した行は検索語に一致しなくなっても隠さない（置換直後に行が
        # 消えると削除されたように見えるため）。id(track) → track で持つ
        # （Track を生かしておき id の再利用を防ぐ）。検索条件が変わったら捨てる
        self._pinned: dict[int, Track] = {}

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
        self._check_external_tools()
        self._check_ytdlp()

    # -- UI 構築 -------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 上段: URL 入力 + 追加系ボタン
        top = QHBoxLayout()
        self._url_edit = QPlainTextEdit()
        self._url_edit.setPlaceholderText(
            "URL を改行区切りで貼り付け（複数可、Ctrl+Enter で追加）"
        )
        self._url_edit.setFixedHeight(64)  # 3 行程度
        # Ctrl+Enter（mac は ⌘+Enter）で貼り付け→追加が手だけで完結する。
        # QShortcut はフォーカス経由の発火なので、キーイベントを直接見る
        # イベントフィルタにする（eventFilter を参照）
        self._url_edit.installEventFilter(self)
        top.addWidget(self._url_edit, stretch=1)

        btn_grid = QGridLayout()
        add_btn = QPushButton("追加")
        add_btn.clicked.connect(self._on_add_urls)
        list_btn = QPushButton("リスト読込")
        list_btn.setToolTip(
            "URL を 1 行ずつ記入したテキストファイルを読み込む（空行と # 始まりの行は無視）"
        )
        list_btn.clicked.connect(self._on_load_list)
        file_btn = QPushButton("ファイル追加")
        file_btn.clicked.connect(self._on_add_files)
        import_btn = QPushButton("保存先から取り込み")
        import_btn.setToolTip(
            "保存先フォルダ（[設定] で変更可。既定は files/）にある音声ファイルを"
            "まとめて追加する（追加済みの行は増えない）。\n"
            "既存のメタデータ（曲名 / アーティスト / アルバム）を読み込んで表示し、"
            "そのまま編集できる"
        )
        import_btn.clicked.connect(self._on_import_dir)
        # 2×2 グリッド（縦 4 段だと URL 欄より背が高くなり、小さいウィンドウで
        # テーブルの表示領域を圧迫するため）
        for i, b in enumerate((add_btn, list_btn, file_btn, import_btn)):
            btn_grid.addWidget(b, i // 2, i % 2)
        top.addLayout(btn_grid)
        root.addLayout(top)

        # ツールバー行: 実行 / 停止 / 形式 / 自動書き込み
        bar = QHBoxLayout()
        self._run_btn = QPushButton("▶ 実行")
        self._run_btn.setToolTip(
            "URL 行のダウンロード → タイトル推定 → タグ書き込みまでを一括実行する\n"
            "（自動書き込み OFF のときは「確認待ち」で止まる）"
        )
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
        # 裸のコンボだと何の形式か分からないためラベルを付ける
        fmt_label = QLabel("DL形式:")
        bar.addWidget(fmt_label)
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(core.SUPPORTED_FORMATS)
        self._fmt_combo.setCurrentText("mp3")
        self._fmt_combo.setToolTip(
            "ダウンロード時に変換する音声形式（既にあるローカルファイル行には影響しない）\n"
            "opus は YouTube が配信している形式そのもの。音量ノーマライズを OFF に\n"
            "すると再エンコードなし（無劣化）で保存できる。"
        )
        bar.addWidget(self._fmt_combo)
        self._auto_write = QCheckBox("自動書き込み")
        self._auto_write.setChecked(True)
        self._auto_write.setToolTip(
            "ON: 推定したタイトルをそのままタグへ書き込む\n"
            "OFF: 「確認待ち」で止まり、確認・修正後に [選択行を書き込み] で書き込む"
        )
        bar.addWidget(self._auto_write)
        # 再生リスト付きの動画 URL（watch?v=...&list=...）をどう扱うか。
        # [設定] の「再生リスト付き動画 URL はリスト全体を展開する」と同じ設定を
        # 裏返して出したもの（実行のたびに切り替えたい設定なので、ダイアログを
        # 開かずに触れる場所に置く）。両者は _on_noplaylist_toggled /
        # apply_settings で同期する
        self._noplaylist_check = QCheckBox("再生リストを無視")
        self._noplaylist_check.setChecked(not self._expand_playlist)
        self._noplaylist_check.setToolTip(
            "ON: watch?v=...&list=... のような再生リスト付き URL でも、"
            "その動画 1 本だけを対象にする（既定）\n"
            "OFF: URL に含まれる再生リスト全体を展開して処理する\n"
            "（[設定] の「再生リスト」と同じ設定）"
        )
        self._noplaylist_check.toggled.connect(self._on_noplaylist_toggled)
        bar.addWidget(self._noplaylist_check)
        bar.addStretch(1)
        search_btn = QPushButton("検索・置換")
        native = QKeySequence.SequenceFormat.NativeText
        search_btn.setToolTip(
            "キーワードで行を絞り込み、推定タイトル・アーティスト・アルバムの"
            "文字列を置き換える\n"
            f"（{QKeySequence(QKeySequence.StandardKey.Find).toString(native)} で検索欄、"
            f"{replace_shortcut().toString(native)} で置換欄へ）"
        )
        search_btn.clicked.connect(self.open_search)
        bar.addWidget(search_btn)
        settings_btn = QPushButton("設定")
        settings_btn.clicked.connect(self._on_settings)
        bar.addWidget(settings_btn)
        root.addLayout(bar)

        # 上段の追加系ボタンと [検索・置換]/[設定] の幅を統一する（最長ラベル基準。
        # グリッドの列幅を揃え、別レイアウトのボタンも同じ幅にする）
        same_width = (add_btn, list_btn, file_btn, import_btn, search_btn, settings_btn)
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

        # 検索バー（行数が多いときの絞り込み用。既定は非表示で、[検索]
        # ボタンか Ctrl+F で開く）。一致しない行は QTableView 側で隠す
        # （QSortFilterProxyModel を使わない理由は _apply_filter を参照）
        self._search_bar = QWidget()
        self._search_bar.setVisible(False)
        search_box = QVBoxLayout(self._search_bar)
        search_box.setContentsMargins(0, 0, 0, 0)
        search_box.setSpacing(4)
        search_lay = QHBoxLayout()
        search_title = QLabel("検索:")
        search_lay.addWidget(search_title)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            "キーワードを入力すると一致する行だけ表示（Enter で表へ / Esc で解除）"
        )
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setToolTip(
            "元タイトル・チャンネル・推定タイトル・アーティスト・アルバム・状態・"
            "形式のいずれかに含まれる行を表示する。\n"
            "表示の絞り込みだけで、[▶ 実行] の対象は全行のまま。\n"
            "下段の置換欄では、この文字列を置き換える"
        )
        self._search_edit.textChanged.connect(self._on_search_changed)
        # Esc で解除 / Enter でテーブルへフォーカス（eventFilter を参照）
        self._search_edit.installEventFilter(self)
        search_lay.addWidget(self._search_edit, stretch=1)
        self._search_label = QLabel("")
        search_lay.addWidget(self._search_label)
        self._wildcard_check = QCheckBox("ワイルドカード")
        self._wildcard_check.setToolTip(
            "* = 任意の文字列（0 文字以上）、? = 任意の 1 文字（Excel と同じ記法）。\n"
            "例: 「(*)」で括弧ごと、「feat.*」で feat. 以降を丸ごと指定できる。\n"
            "* や ? の文字そのものを探すときは ~* / ~? / ~~ と書く"
        )
        self._wildcard_check.toggled.connect(self._on_search_option_changed)
        search_lay.addWidget(self._wildcard_check)
        self._case_check = QCheckBox("大/小文字を区別")
        self._case_check.toggled.connect(self._on_search_option_changed)
        search_lay.addWidget(self._case_check)
        search_close = QPushButton("✕")
        search_close.setFixedWidth(28)
        search_close.setToolTip("絞り込みを解除して検索欄を閉じる")
        search_close.clicked.connect(self.close_search)
        search_lay.addWidget(search_close)
        search_box.addLayout(search_lay)

        # 置換欄（検索欄の下段。検索欄と常に一緒に出す — open_search 参照）。
        # 検索語に一致した部分を編集可能列（推定タイトル / アーティスト /
        # アルバム）で置き換える。Edit 系コマンド経由なので undo（Ctrl+Z）で戻せる
        replace_lay = QHBoxLayout()
        replace_title = QLabel("置換後:")
        replace_lay.addWidget(replace_title)
        self._replace_edit = QLineEdit()
        self._replace_edit.setPlaceholderText(
            "置換後の文字列（空欄なら削除）。Enter で 1 件ずつ / Ctrl+Enter ですべて置換"
        )
        self._replace_edit.installEventFilter(self)
        replace_lay.addWidget(self._replace_edit, stretch=1)
        self._replace_label = QLabel("")
        replace_lay.addWidget(self._replace_label)
        replace_lay.addWidget(QLabel("対象:"))
        self._replace_target = QComboBox()
        for label, cols in _REPLACE_TARGETS:
            self._replace_target.addItem(label, cols)
        self._replace_target.setToolTip(
            "置き換える列（元タイトル・チャンネルなどは書き込み対象でないため置換しない）"
        )
        self._replace_target.currentIndexChanged.connect(lambda *_: self._update_replace_label())
        replace_lay.addWidget(self._replace_target)
        next_keys = QKeySequence.keyBindings(QKeySequence.StandardKey.FindNext)[0].toString(native)
        self._find_next_btn = QPushButton("次へ")
        self._find_next_btn.setToolTip(
            f"次の一致セルへ移動する（置換はしない）。{next_keys}、Shift を足すと前へ"
        )
        self._find_next_btn.clicked.connect(lambda: self.find_next())
        replace_lay.addWidget(self._find_next_btn)
        self._replace_one_btn = QPushButton("置換して次へ")
        self._replace_one_btn.setToolTip(
            "選択中のセルが一致していれば置き換えて、次の一致セルへ移動する\n"
            "（一致していなければ、置き換えずに次の一致セルへ移動するだけ）。"
            "置換欄で Enter"
        )
        self._replace_one_btn.clicked.connect(self.replace_current)
        replace_lay.addWidget(self._replace_one_btn)
        self._replace_all_btn = QPushButton("すべて置換")
        self._replace_all_btn.setToolTip(
            "表示中の行の一致をすべて置き換える（Ctrl+Z で一括して元に戻せる）。"
            "置換欄で Ctrl+Enter"
        )
        self._replace_all_btn.clicked.connect(self.replace_all)
        replace_lay.addWidget(self._replace_all_btn)
        search_box.addLayout(replace_lay)
        # 「検索:」「置換:」の幅を揃えて入力欄の左端を合わせる
        label_width = max(search_title.sizeHint().width(), replace_title.sizeHint().width())
        search_title.setFixedWidth(label_width)
        replace_title.setFixedWidth(label_width)
        root.addWidget(self._search_bar)

        # 中央: テーブル
        self._view = _DropTableView(self)
        self._view.setModel(self._model)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # 選択行を分かりやすくする: ハイライト色を明示し、非アクティブ状態
        # （ボタンを押した直後などフォーカスがテーブル外にあるとき）でも
        # 同じ色で塗る。既定の Inactive パレットは淡いグレーで、状態色
        # （緑/黄/赤）の行に重なるとほぼ判別できないため。
        pal = self._view.palette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
            pal.setColor(group, QPalette.ColorRole.Highlight, _SELECTION_BG)
            pal.setColor(group, QPalette.ColorRole.HighlightedText, _SELECTION_TEXT)
        self._view.setPalette(pal)
        # 行番号（縦ヘッダ）側も選択行を強調する（行単位の選択を目で追いやすく）
        self._view.verticalHeader().setHighlightSections(True)
        self._view.horizontalHeader().setStretchLastSection(True)
        self._view.setColumnWidth(COL_STEM, 240)
        self._view.setColumnWidth(COL_TITLE, 200)
        self._view.setColumnWidth(COL_ARTIST, 140)
        self._view.setColumnWidth(COL_ALBUM, 140)
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
        self._view.setItemDelegateForColumn(COL_ALBUM, _UndoEditDelegate(self))
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
        # range(0,0) だと macOS では溝ごと描画されず「何も無い空白」に見える
        # ため、待機中も 0-1 のダミー範囲で溝だけ出しておく
        self._seek_slider.setRange(0, 1)
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
            # 置換はセル編集と同じ扱い（実行中はワーカーが Track を書き換える）
            self._replace_one_btn,
            self._replace_all_btn,
        ]

        # undo/redo（Ctrl+Z / Ctrl+Y）。ウィンドウにアクションを載せる
        undo_action = self._undo.createUndoAction(self, "元に戻す")
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        redo_action = self._undo.createRedoAction(self, "やり直し")
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.addAction(undo_action)
        self.addAction(redo_action)

        # 検索（Ctrl+F、mac は ⌘F）。ウィンドウのアクションにしておくと
        # テーブル・URL 欄どちらにフォーカスがあっても効く
        find_action = QAction("検索", self)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(self.open_search)
        self.addAction(find_action)
        # 置換（Ctrl+H、mac は ⌘⌥F。replace_shortcut 参照）
        replace_action = QAction("置換", self)
        replace_action.setShortcut(replace_shortcut())
        replace_action.triggered.connect(self.open_replace)
        self.addAction(replace_action)
        # 次 / 前の一致セルへ（F3・Shift+F3、mac は ⌘G・⌘⇧G）
        find_next_action = QAction("次を検索", self)
        find_next_action.setShortcuts(QKeySequence.StandardKey.FindNext)
        find_next_action.triggered.connect(lambda: self.find_next())
        self.addAction(find_next_action)
        find_prev_action = QAction("前を検索", self)
        find_prev_action.setShortcuts(QKeySequence.StandardKey.FindPrevious)
        find_prev_action.triggered.connect(lambda: self.find_next(backward=True))
        self.addAction(find_prev_action)

        # undo コマンドは行番号(int)を保持するため、行の並び・構成が変わったら
        # 過去のコマンドは無効（別の行に復元されてしまう）。行削除・差し替え
        # （rowsRemoved）とソート（layoutChanged）でスタックを破棄する。
        # 末尾への行追加(rowsInserted のみ)は既存行がずれないので対象外。
        self._model.rowsRemoved.connect(lambda *_: self._clear_undo_history())
        self._model.layoutChanged.connect(lambda *_: self._clear_undo_history())

        # 絞り込みは行番号で「隠す/表示する」ため、行の増減・並べ替え・内容変更で
        # 追従させる（行内容が変わると一致・不一致も変わる）。dataChanged は
        # DL 中に頻繁に飛ぶので、変化した行だけ評価し直す
        self._model.rowsInserted.connect(lambda *_: self._apply_filter())
        self._model.rowsRemoved.connect(lambda *_: self._apply_filter())
        self._model.layoutChanged.connect(lambda *_: self._apply_filter())
        self._model.dataChanged.connect(self._on_data_changed)

    def _clear_undo_history(self) -> None:
        """行の並び・構成の変化で無効になった undo 履歴を破棄する。

        ソートや行削除の直後に黙って Ctrl+Z が効かなくなると不可解なため、
        履歴があった場合だけステータスバーで知らせる（パイプライン実行中の
        行差し替えでは進捗表示を上書きしない）。
        """
        had_history = self._undo.count() > 0
        self._undo.clear()
        if had_history and not self._running:
            self.statusBar().showMessage(
                "行の並び・構成が変わったため、編集履歴（元に戻す）をクリアしました"
            )

    def eventFilter(self, obj, event) -> bool:
        """URL 欄・検索欄・置換欄のキー操作を拾う。

        - URL 欄の Ctrl+Enter（mac は ⌘+Enter）で [追加] を実行
        - 検索欄の Esc で絞り込み解除、Enter でテーブルへフォーカス移動
        - 置換欄の Enter で 1 件置換、Ctrl+Enter ですべて置換、Esc で閉じる
        """
        if event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key = event.key()
        is_enter = key in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        mods = event.modifiers()
        if obj is self._url_edit and is_enter and mods & Qt.KeyboardModifier.ControlModifier:
            self._on_add_urls()
            return True
        if obj in (self._search_edit, self._replace_edit) and key == Qt.Key.Key_Escape:
            self.close_search()
            return True
        if obj is self._search_edit and is_enter:
            # 絞り込んだ行をそのまま操作できるようテーブルへ移る
            self._view.setFocus()
            return True
        if obj is self._replace_edit and is_enter:
            if mods & Qt.KeyboardModifier.ControlModifier:
                self.replace_all()
            else:
                self.replace_current()
            return True
        return super().eventFilter(obj, event)

    # -- 検索（行の絞り込み）------------------------------------------------

    def open_search(self) -> None:
        """検索・置換バーを開いて検索欄へフォーカスする（[検索・置換] / Ctrl+F）。

        置換欄は検索欄と常に一緒に出す（検索だけのモードは持たない）。モードを
        分けると置換欄の開閉ボタンが要り、「置換」と書かれたボタンが並んで
        どれが何か分かりにくくなるため。置換欄は 1 行ぶんの高さしか取らない。
        """
        self._open_bar(self._search_edit)

    def open_replace(self) -> None:
        """検索・置換バーを開く（Ctrl+H、mac は ⌃H）。

        検索語が未入力なら検索欄に、入力済みなら置換欄にフォーカスする。
        """
        self._open_bar(self._replace_edit if self._search_text else self._search_edit)

    def _open_bar(self, edit: QLineEdit) -> None:
        self._search_bar.setVisible(True)
        edit.setFocus()
        edit.selectAll()

    def close_search(self) -> None:
        """絞り込みを解除して検索・置換バーを閉じる（✕ / Esc）。

        隠したまま閉じると「行が消えた」ように見えるため、必ず解除してから
        閉じる（clear → textChanged → _apply_filter で全行が戻る）。
        """
        self._search_edit.clear()
        self._search_bar.setVisible(False)
        self._view.setFocus()

    def _on_search_changed(self, text: str) -> None:
        self._search_text = text
        self._pinned.clear()  # 検索条件が変わったら「置換済みで残す行」は解除
        self._apply_filter()
        self._update_replace_label()

    def _on_search_option_changed(self, *_) -> None:
        """ワイルドカード / 大小文字の切り替え。絞り込みを掛け直す。"""
        self._pinned.clear()
        # 何も隠していなくても、新しい条件で隠す行が出るので必ず評価する
        self._filter_active = True
        self._apply_filter()
        self._update_replace_label()

    def _on_data_changed(self, top_left, bottom_right, roles=None) -> None:
        """行の内容が変わったら、その行だけ絞り込みを評価し直す。"""
        if self._filter_active or self._search_text.strip():
            self._apply_filter(top_left.row(), bottom_right.row())

    def _apply_filter(self, first: int | None = None, last: int | None = None) -> None:
        """検索語に一致しない行を隠す（first..last 指定でその範囲だけ再評価）。

        QSortFilterProxyModel は使わない。ワーカーは行を同一性(is)で探し、
        進捗 dict を行番号で持つため、proxy の行マッピングと噛み合わない
        （model.sort と同じ理由）。ビュー側で行を隠すだけなら行番号は一切
        変わらないので、実行中に絞り込んでも安全。
        """
        active = bool(self._search_text.strip())
        if not active and not self._filter_active:
            return  # 何も隠していない状態が続くだけなら触らない
        row_count = self._model.rowCount()
        if first is None:
            rows = range(row_count)
        else:
            rows = range(max(0, first), min(last, row_count - 1) + 1)
        for row in rows:
            hidden = (
                not self._model.matches(
                    row,
                    self._search_text,
                    wildcard=self._wildcard_check.isChecked(),
                    case_sensitive=self._case_check.isChecked(),
                )
                and id(self._model.track_at(row)) not in self._pinned
            )
            self._view.setRowHidden(row, hidden)
        self._filter_active = active
        self._update_search_label()

    def _update_search_label(self) -> None:
        """検索欄の右側に「表示 / 全体」の件数を出す（0 件のときは明示する）。"""
        if not self._search_text.strip():
            self._search_label.setText("")
            return
        total = self._model.rowCount()
        shown = len(self._visible_rows())
        self._search_label.setText(f"{shown} / {total} 件" if shown else "一致なし")

    def _visible_rows(self) -> list[int]:
        """絞り込みで隠れていない行の行番号。"""
        return [r for r in range(self._model.rowCount()) if not self._view.isRowHidden(r)]

    # -- 置換 / 一致セルへの移動 ----------------------------------------------

    def _find_pattern(self):
        """置換・一致セル移動用のパターンを作る（検索語が空なら None）。

        絞り込みは前後の空白を落とした語で行うが、置換は打ったとおりの語を
        使う（「 (Official)」の先頭の空白ごと消したい、二重スペースを 1 つに
        したい、など空白自体が置換の対象になるため）。空白を落とした語の
        一致は元の語の一致を必ず含むので、置換対象が隠れた行に残ることはない。
        """
        return compile_pattern(
            self._search_text, self._wildcard_check.isChecked(), self._case_check.isChecked()
        )

    def _replace_columns(self) -> tuple[int, ...]:
        return self._replace_target.currentData() or EDITABLE_COLUMNS

    def _cell_value(self, row: int, col: int) -> str:
        """セルの編集値（✎ やエラー表示を含まない素の値）。置換はこれに当てる。"""
        index = self._model.index(row, col)
        return str(self._model.data(index, Qt.ItemDataRole.EditRole) or "")

    def _matching_cells(self) -> list[tuple[int, int]]:
        """表示中の行で、置換対象の列のうち検索語に一致するセル (行, 列) を
        表の並び順で返す（[次へ] / [置換して次へ] の移動先）。"""
        pattern = self._find_pattern()
        if pattern is None:
            return []
        return [
            (row, col)
            for row in self._visible_rows()
            for col in self._replace_columns()
            if has_match(self._cell_value(row, col), pattern)
        ]

    def find_next(self, backward: bool = False) -> bool:
        """現在セルの次（backward=True なら前）の一致セルへ移動する。

        末尾まで行ったら先頭へ回り込む。一致が 1 つも無ければ False。
        検索欄が閉じていれば開く（F3 / ⌘G を押しても何も起きないと戸惑うため）。
        """
        if self._search_bar.isHidden():
            self.open_search()
        cells = self._matching_cells()
        if not cells:
            self.statusBar().showMessage(
                "一致するセルはありません" if self._search_text else "検索する文字列を入力してください"
            )
            return False
        cur = self._view.currentIndex()
        if backward:
            pos = (cur.row(), cur.column()) if cur.isValid() else (self._model.rowCount(), 0)
            before = [c for c in cells if c < pos]
            row, col = before[-1] if before else cells[-1]
        else:
            pos = (cur.row(), cur.column()) if cur.isValid() else (-1, -1)
            after = [c for c in cells if c > pos]
            row, col = after[0] if after else cells[0]
        index = self._model.index(row, col)
        # フォーカスは移さない（検索欄・置換欄で Enter を続けて押せるように）
        self._view.setCurrentIndex(index)
        self._view.scrollTo(index)
        return True

    def _pin_rows(self, rows) -> None:
        for row in rows:
            track = self._model.track_at(row)
            self._pinned[id(track)] = track

    def replace_current(self) -> bool:
        """現在セルを置換して次の一致セルへ移る（[置換] / 置換欄の Enter）。

        Excel の [置換] と同じく、現在セルが一致していなければ置き換えずに
        次の一致セルへ移動するだけ。1 回目で位置を確かめ、2 回目以降で 1 件
        ずつ置き換えていける。置換したら True。
        """
        if self._running:
            return False
        pattern = self._find_pattern()
        if pattern is None:
            self.statusBar().showMessage("検索する文字列を入力してください")
            return False
        cur = self._view.currentIndex()
        replaced = False
        if (
            cur.isValid()
            and not self._view.isRowHidden(cur.row())
            and cur.column() in self._replace_columns()
        ):
            row, col = cur.row(), cur.column()
            old = self._cell_value(row, col)
            new, count = replace_in(old, pattern, self._replace_edit.text())
            if count and new.strip() != old:
                self._pin_rows([row])
                self._undo.beginMacro("置換")
                self.push_edit(row, col, new)
                self._undo.endMacro()
                replaced = True
        found = self.find_next()
        self._update_replace_label()
        if replaced:
            self.statusBar().showMessage(
                "置換しました" + ("（次の一致へ移動）" if found else "（ほかに一致はありません）")
            )
        elif found:
            self.statusBar().showMessage(
                "一致したセルへ移動しました。もう一度 [置換] で置き換えます"
            )
        return replaced

    def replace_all(self) -> int:
        """表示中の行の一致をすべて置換する（[すべて置換] / Ctrl+Enter）。

        置換したセル数を返す。1 つの undo macro にまとめるので、Ctrl+Z 1 回で
        全部戻る。置換した行は検索語に一致しなくなっても表示に残し（_pinned）、
        選択状態にしてそのまま確認・書き込みへ進めるようにする。
        """
        if self._running:
            return 0
        pattern = self._find_pattern()
        if pattern is None:
            self.statusBar().showMessage("検索する文字列を入力してください")
            return 0
        replacement = self._replace_edit.text()
        edits: list[tuple[int, int, str]] = []
        occurrences = 0
        for row in self._visible_rows():
            for col in self._replace_columns():
                old = self._cell_value(row, col)
                new, count = replace_in(old, pattern, replacement)
                if count and new.strip() != old:
                    edits.append((row, col, new))
                    occurrences += count
        if not edits:
            self.statusBar().showMessage("置換できる一致はありません")
            return 0
        rows = sorted({row for row, _, _ in edits})
        self._pin_rows(rows)
        self._undo.beginMacro("すべて置換")
        for row, col, value in edits:
            self.push_edit(row, col, value)
        self._undo.endMacro()
        self._select_rows(rows)
        self._update_replace_label()
        self.statusBar().showMessage(
            f"{len(rows)} 行・{len(edits)} セルを置換しました（{occurrences} 箇所）。"
            "Ctrl+Z で元に戻せます"
        )
        return len(edits)

    def _update_replace_label(self) -> None:
        """置換欄の右側に、置換対象になるセル数を出す。"""
        if not self._search_text:
            self._replace_label.setText("")
            return
        n = len(self._matching_cells())
        self._replace_label.setText(f"{n} セル一致" if n else "一致なし")

    # -- 行追加系 ------------------------------------------------------------

    def add_urls(self, urls: list[str]) -> int:
        """URL ごとにプレースホルダ行を追加する。追加件数を返す。

        追加後は全行を選択状態にする（select_all_rows 参照）。
        """
        tracks = [Track(stem=u, url=u, status=Status.QUEUED) for u in urls]
        self._model.add_tracks(tracks)
        self.select_all_rows()
        return len(tracks)

    def add_files(self, paths: list[Path]) -> int:
        """ローカルファイル行を追加する。追加件数を返す。

        既にリストへ入っているファイル（filepath が同じ行）はスキップする
        （[files/ 取り込み] を押すたびに同じ行が増えないように）。
        行の初期値には既存のタグ（曲名 / 作者 / アルバム名）を読み込む
        （core.track_from_file 参照）。うち何件でタグが読めたかは
        _last_tagged に残し、ステータスバーの文言に使う。
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
        self._last_tagged = sum(
            1 for t in tracks if t.guessed_title or t.artist or t.album
        )
        self._model.add_tracks(tracks)
        self.select_all_rows()
        return len(tracks)

    def select_all_rows(self) -> None:
        """全行を選択状態にする（行追加後の既定状態）。

        追加直後は選択が空で、[選択行を書き込み] などが「行が選択されて
        いません」で空振りする。追加した行をすぐ操作対象にできるよう、
        行を足したら全選択に戻す。
        """
        if self._model.rowCount() == 0:
            return
        self._view.selectAll()

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
        if self._last_tagged:
            msg += f"（うち {self._last_tagged} 件はメタデータを読み込み）"
        if skipped:
            msg += f"（追加済み {skipped} 件はスキップ）"
        self.statusBar().showMessage(msg)

    def _on_import_dir(self) -> None:
        # 取り込み元は設定の保存先フォルダに合わせる（既定は core.FILES_DIR）。
        # FILES_DIR 固定だと保存先を変えたときにボタンの対象と食い違う
        target = self._out_dir or core.FILES_DIR
        files = core.list_music_files(target)
        if not files:
            self.statusBar().showMessage(f"{target} に音声ファイルがありません")
            return
        n = self.add_files(files)
        if n:
            msg = f"{target.name}/ から {n} 件を取り込みました"
            if self._last_tagged:
                msg += f"（うち {self._last_tagged} 件はメタデータを読み込み）"
            self.statusBar().showMessage(msg)
        else:
            self.statusBar().showMessage(f"{target.name}/ のファイルはすべて取り込み済みです")

    # -- 外部ツール ----------------------------------------------------------

    def _check_external_tools(self) -> None:
        """起動時に ffmpeg / deno の有無を確認する。

        どちらも同梱していない（PATH 上の実体を使う）。ステータスバーには
        まとめて 1 行で出し、見落とし防止のモーダルは不足しているものごとに
        出す。テスト（restore_settings=False）ではモーダルを出さない。
        """
        ffmpeg_missing = shutil.which("ffmpeg") is None
        # yt-dlp が既定で有効にする JS ランタイムは deno だけ（node が PATH に
        # あっても --js-runtimes 指定なしでは使われない）ので deno だけを見る
        deno_missing = shutil.which("deno") is None

        notes = []
        if ffmpeg_missing:
            notes.append("ffmpeg が見つかりません。mp3/wav 変換に必要です")
        if deno_missing:
            notes.append("deno が見つかりません。ダウンロード速度が大幅に低下します")
        if notes:
            self.statusBar().showMessage(
                "警告: " + " / ".join(notes) + "（PATH を確認してください）"
            )
        else:
            self.statusBar().showMessage("準備完了")

        if self._settings is None:
            return
        if ffmpeg_missing:
            QMessageBox.warning(
                self,
                "ffmpeg が見つかりません",
                "ffmpeg が見つかりません。ダウンロード後の音声変換・ノーマライズ・"
                "無音削除に必要です。"
                f"\nインストールして PATH を通してください。{_ffmpeg_install_hint()}",
            )
        if deno_missing:
            QMessageBox.warning(
                self,
                "deno が見つかりません",
                "deno が見つかりません。YouTube のダウンロードで使う JavaScript "
                "ランタイムです。無くてもダウンロードはできますが、YouTube 側の"
                "制限が解除できず速度が大幅に落ちます（実測で 10 分の 1 以下）。"
                f"\nインストールして PATH を通してください。{_deno_install_hint()}",
            )

    def _confirm_deno(self) -> bool:
        """DL 実行前の deno 確認。見つからなければ警告し、続行するか尋ねる。

        ffmpeg と違いダウンロード自体は成功する（遅くなるだけ）ため、既定の
        選択肢は [はい] にしてある。テスト（restore_settings=False）では
        ダイアログを出さず続行する。
        """
        if shutil.which("deno") is not None:
            return True
        if self._settings is None:
            return True
        ret = QMessageBox.warning(
            self,
            "deno が見つかりません",
            "deno が見つからないため、ダウンロード速度が大幅に低下します"
            "（実測で 10 分の 1 以下）。"
            f"\n{_deno_install_hint()}"
            "\n\nこのまま実行しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return ret == QMessageBox.StandardButton.Yes

    # -- yt-dlp --------------------------------------------------------------

    def _check_ytdlp(self) -> None:
        """起動時に yt-dlp と EJS の有無を確認し、欠けていれば取得を促す。

        yt-dlp は exe に同梱していない（ytdlp_runtime 参照）。ffmpeg と違い
        これが無いとダウンロード自体ができないため、警告ではなく取得の可否を
        尋ねる。EJS（yt-dlp-ejs）は欠けてもダウンロードは動くが、YouTube の
        署名・n チャレンジが解けず速度と取得できる形式が落ちる。どちらも
        [はい] 一発で直るので同じ導線に乗せる（ダイアログは 1 つだけ）。
        テスト（restore_settings=False）ではモーダルを出さない。
        """
        if not ytdlp_runtime.is_available():
            title = "yt-dlp を取得しますか？"
            message = """ダウンロードの実行部（yt-dlp）がまだ取得されていません。
取得しないとダウンロードは実行できません。

今すぐ取得しますか？（数 MB のダウンロードが発生します）"""
            self.statusBar().showMessage("yt-dlp が未取得です（[設定] から取得できます）")
        elif self._ejs_missing():
            title = "EJS を取得しますか？"
            message = """YouTube の制限を解除するスクリプト（EJS）が未取得です。
このままでもダウンロードはできますが、速度が大幅に落ち、取得できない形式が出ます。

今すぐ取得しますか？（1 MB 未満のダウンロードです）"""
            self.statusBar().showMessage("EJS が未取得です（[設定] の [更新] で取得できます）")
        else:
            return
        if self._settings is None:
            return
        ret = QMessageBox.question(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self._start_ytdlp_fetch()

    def _ejs_missing(self) -> bool:
        """展開済み yt-dlp に、対応版の EJS が伴っていないか。

        開発環境（venv の yt-dlp を使う＝展開済みが無い）や、yt-dlp が版を
        要求していない場合は「欠けていない」とみなす（取得のしようがない）。
        """
        target = ytdlp_runtime.installed_dir()
        if target is None:
            return False
        wanted = ytdlp_runtime.required_ejs_version(target)
        return wanted is not None and ytdlp_runtime.ejs_version(target) != wanted

    def _start_ytdlp_fetch(self) -> None:
        """yt-dlp の取得をワーカーで走らせ、経過をステータスバーへ出す。"""
        worker = YtdlpWorker(check_only=False)
        worker.signals.status.connect(self.statusBar().showMessage)
        worker.signals.done.connect(self._on_ytdlp_fetch_done)
        self._pool.start(worker)

    def _on_ytdlp_fetch_done(self, ok: bool, message: str, needs_restart: bool) -> None:
        self.statusBar().showMessage(message if ok else "yt-dlp の取得に失敗: " + message)
        if not ok and self._settings is not None:
            QMessageBox.warning(self, "yt-dlp を取得できませんでした", message)

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
        if not ytdlp_runtime.is_available():
            self.statusBar().showMessage("yt-dlp が未取得のため実行できません（[設定] から取得してください）")
            return
        if not self._confirm_ffmpeg():
            self.statusBar().showMessage("ffmpeg 未検出のため実行を中止しました")
            return
        if not self._confirm_deno():
            self.statusBar().showMessage("deno 未検出のため実行を中止しました")
            return
        worker = PipelineWorker(
            tracks,
            mode=MODE_FULL,
            fmt=self._fmt_combo.currentText(),
            auto_write=self._auto_write.isChecked(),
            cancel=self._reset_cancel(),
            batch_size=self._batch_size,
            max_downloads=self._max_downloads,
            out_dir=self._out_dir,
            expand_playlist=self._expand_playlist,
            normalize=self._normalize,
            loudness=self._loudness,
            trim_silence=self._trim_silence,
            best_quality=self._best_quality,
            audio_bitrate=self._audio_bitrate,
            ytmusic_direct=self._ytmusic_direct,
            use_schema=self._use_schema,
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
            use_schema=self._use_schema,
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
            ytmusic_direct=self._ytmusic_direct,
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
            use_schema=self._use_schema,
            max_downloads=self._max_downloads,
            auto_write=self._auto_write.isChecked(),
            ytmusic_direct=self._ytmusic_direct,
            expand_playlist=self._expand_playlist,
            normalize=self._normalize,
            loudness=self._loudness,
            trim_silence=self._trim_silence,
            best_quality=self._best_quality,
            audio_bitrate=self._audio_bitrate,
            theme=self._theme,
            log_level=self._log_level,
            llm_overrides=dict(self._llm_overrides),
        )
        # 親（self）付きで作るため、放っておくと開くたびにダイアログが
        # 窓の子として溜まり、終了時の後始末まで生き残る。使い終えたら消す
        try:
            if not dlg.exec():
                return
            self.apply_settings(dlg.values())
        finally:
            dlg.deleteLater()

    def _on_noplaylist_toggled(self, checked: bool) -> None:
        """[再生リストを無視] トグル。設定ダイアログの「展開する」と表裏の値を持つ。

        ダイアログを開かずに切り替えた場合もそのまま残ってほしいので、
        ここで QSettings へ書く（キーは設定ダイアログ側と同じ）。
        """
        self._expand_playlist = not checked
        if self._settings is not None:
            self._settings.setValue("options/expand_playlist", self._expand_playlist)

    def apply_settings(self, values: dict) -> None:
        """設定ダイアログの値を反映し、QSettings へ保存する。"""
        out_dir = values["out_dir"]
        # 既定の FILES_DIR と同じなら None（=core 既定）として扱う
        self._out_dir = None if out_dir == core.FILES_DIR else out_dir
        self._batch_size = int(values["batch_size"])
        self._use_schema = bool(values.get("use_schema", core.USE_SCHEMA))
        self._max_downloads = max(1, int(values.get("max_downloads", core.MAX_DOWNLOADS)))
        self._ytmusic_direct = bool(values.get("ytmusic_direct", True))
        self._expand_playlist = bool(values.get("expand_playlist", False))
        # ツールバーのチェックボックスは同じ設定の裏返し。ここで揃える
        self._noplaylist_check.setChecked(not self._expand_playlist)
        self._normalize = bool(values.get("normalize", True))
        self._loudness = float(values.get("loudness", core.NORMALIZE_TARGET_I))
        self._trim_silence = bool(values.get("trim_silence", False))
        self._best_quality = bool(values.get("best_quality", False))
        self._audio_bitrate = core.parse_bitrate(values.get("audio_bitrate"))
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
            self._settings.setValue("options/use_schema", self._use_schema)
            self._settings.setValue("options/max_downloads", self._max_downloads)
            self._settings.setValue("options/ytmusic_direct", self._ytmusic_direct)
            self._settings.setValue("options/expand_playlist", self._expand_playlist)
            self._settings.setValue("options/normalize", self._normalize)
            self._settings.setValue("options/loudness", self._loudness)
            self._settings.setValue("options/trim_silence", self._trim_silence)
            self._settings.setValue("options/best_quality", self._best_quality)
            # None は空文字で保存する（QSettings に None を入れると型が揺れる）
            self._settings.setValue(
                "options/audio_bitrate",
                "" if self._audio_bitrate is None else str(self._audio_bitrate),
            )
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
        """曲の長さが確定したらシークバーの範囲と時間表示を更新する。

        下限 1 を保つのは、range(0,0) だと macOS でスライダーの溝ごと
        消えるため（停止時に duration 0 が飛んでくることがある）。
        """
        self._duration_ms = duration_ms
        self._seek_slider.setRange(0, max(1, duration_ms))
        self._update_time_label(self._seek_slider.value())

    def _update_time_label(self, position_ms: int) -> None:
        self._time_label.setText(
            f"{format_time(position_ms)} / {format_time(self._duration_ms)}"
        )

    # -- 補助 ---------------------------------------------------------------

    def _selected_rows(self) -> list[int]:
        """選択行の行番号（昇順）。検索で隠れている行は除く。

        絞り込み中でも選択自体は残る（選択は行番号で持たれ、隠しても解除
        されない）ため、除かないと「画面に出ていない行」まで書き込み・削除の
        対象になってしまう。見えている行だけを操作対象にする。
        """
        return sorted(
            {
                idx.row()
                for idx in self._view.selectionModel().selectedRows()
                if not self._view.isRowHidden(idx.row())
            }
        )

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
        # フィルハンドル（ドラッグコピー）も同じ理由で実行中は隠す
        self._view.set_fill_enabled(not running)

    def _on_stop(self) -> None:
        self._cancel.set()
        # 押されたことを見せる。次の実行で _set_running(True) が戻す。
        # 停止要求はすぐ効くが、ffmpeg 変換中だけはファイルを中途半端に
        # 残さないため書き終わりまで待つ（core.download_tracks の pp_hook）
        self._stop_btn.setEnabled(False)
        self.statusBar().showMessage("停止を要求しました（変換中の曲は書き終えてから止まります）...")

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
        elif col == COL_ALBUM:
            self._undo.push(EditAlbumCommand(self._model, row, value))
        else:
            self._undo.push(EditTitleCommand(self._model, row, value))

    def fill_from_handle(self, col: int, top: int, bottom: int, target_row: int) -> int:
        """フィルハンドルのドラッグ結果を適用する。反映セル数を返す。

        Excel と同じく、元セル（top..bottom 行）の値をドラッグ先の行へ
        繰り返しコピーする（元が複数行ならパターンとして循環）。上方向へ
        ドラッグした場合は下から順に対応させる。反映は Edit 系コマンドの
        macro なので 1 回の undo で全部戻る。
        """
        if self._running or col not in EDITABLE_COLUMNS:
            return 0
        row_count = self._model.rowCount()
        if row_count == 0:
            return 0
        target = max(0, min(target_row, row_count - 1))
        source = [
            str(self._model.data(self._model.index(r, col), Qt.ItemDataRole.EditRole) or "")
            for r in range(top, bottom + 1)
        ]
        if target > bottom:  # 下方向: 元の並びのまま繰り返す
            edits = [
                (bottom + 1 + i, source[i % len(source)]) for i in range(target - bottom)
            ]
        elif target < top:  # 上方向: 元の末尾から逆順に対応させる
            edits = [
                (top - 1 - i, source[-1 - (i % len(source))]) for i in range(top - target)
            ]
        else:
            return 0  # 元の範囲内で離した = 変更なし

        self._undo.beginMacro("フィルコピー")
        for row, value in edits:
            self.push_edit(row, col, value)
        self._undo.endMacro()
        # Excel 同様、元セル + コピー先を選択状態にして続けて操作できるようにする。
        # setCurrentIndex は選択をクリアするので、先に現在セルを移してから選択する
        filled = [row for row, _ in edits]
        current_row = max(filled) if target > bottom else min(filled)
        self._view.setCurrentIndex(self._model.index(current_row, col))
        self._select_rows(sorted(set(range(top, bottom + 1)) | set(filled)))
        self.statusBar().showMessage(f"{len(edits)} 行にコピーしました")
        return len(edits)

    def _on_fill_artists(self) -> None:
        """選択行（未選択なら全行）のアーティスト欄にチャンネル名をコピーする。

        推定はしない（ユーザー要望）。undo は 1 回でまとめて戻る。
        """
        if self._running:
            return
        # 未選択なら全行が対象（絞り込み中は見えている行だけ）
        rows = self._selected_rows() or self._visible_rows()
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
        reveal = menu.addAction(f"ファイルを {file_manager_name()} で開く")
        reveal.triggered.connect(self._on_reveal_file)
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
        # ファイルを持つ選択行（DL 済み / 取り込んだローカル行）があれば有効
        has_file = any(self._model.track_at(r).filepath is not None for r in rows)
        reveal.setEnabled(has_file)
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

    def _on_reveal_file(self) -> None:
        """選択行のファイルをファイルマネージャーで表示する。

        対象は選択行のうちファイルを持つ最初の 1 行（試聴の _preview_target と
        同じ考え方）。複数行を選んだまま実行してウィンドウが選択数だけ開く、
        という事故を避ける。
        """
        track = next(
            (
                self._model.track_at(r)
                for r in self._selected_rows()
                if self._model.track_at(r).filepath is not None
            ),
            None,
        )
        if track is None:
            self.statusBar().showMessage("ファイルを持つ行が選択されていません")
            return
        path = Path(track.filepath)
        if not path.exists():
            self.statusBar().showMessage(f"ファイルが見つかりません: {path}")
            return
        if reveal_in_file_manager(path):
            self.statusBar().showMessage(f"{file_manager_name()} で表示: {path.name}")
        else:
            self.statusBar().showMessage(f"{file_manager_name()} で開けませんでした: {path}")

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
        use_schema = s.value("options/use_schema")
        if use_schema is not None:
            self._use_schema = _as_bool(use_schema)
        max_dl = s.value("options/max_downloads")
        if max_dl is not None:
            try:
                self._max_downloads = max(1, int(max_dl))
            except (TypeError, ValueError):
                pass
        ytmusic = s.value("options/ytmusic_direct")
        if ytmusic is not None:
            self._ytmusic_direct = _as_bool(ytmusic)
        expand = s.value("options/expand_playlist")
        if expand is not None:
            self._expand_playlist = _as_bool(expand)
            self._noplaylist_check.setChecked(not self._expand_playlist)
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
        best_quality = s.value("options/best_quality")
        if best_quality is not None:
            self._best_quality = _as_bool(best_quality)
        bitrate = s.value("options/audio_bitrate")
        if bitrate is not None:
            try:
                self._audio_bitrate = core.parse_bitrate(bitrate)
            except ValueError:
                pass  # 壊れた値は既定（ffmpeg 任せ）のまま
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
    """対応拡張子のローカルファイルをドロップで行追加できる QTableView。

    行が 1 つも無いときは、主フローとドロップ対応（見た目からは分からない）
    の案内文を描画する（paintEvent）。

    加えて Excel 風のフィルハンドルを持つ: 編集可能列（推定タイトル /
    アーティスト）のセルにいるとき、選択範囲の右下に小さな四角を描き、
    それを上下へドラッグすると元の値がその範囲へコピーされる
    （実際の反映は MainWindow.fill_from_handle が undo コマンド経由で行う）。
    """

    _EMPTY_HINT = (
        "URL を貼って [追加] → [▶ 実行] でダウンロードとタイトル付けが始まります\n\n"
        "音声ファイルや URL リスト (.txt) をここへドロップしても追加できます"
    )

    def __init__(self, window: MainWindow):
        super().__init__()
        self._window = window
        self.setAcceptDrops(True)
        # ハンドル上でカーソル形状を変えるため、ボタン非押下の移動も受け取る
        self.setMouseTracking(True)
        # フィルハンドルのドラッグ状態: (列, 上端行, 下端行) と現在のドラッグ先行
        self._fill_source: tuple[int, int, int] | None = None
        self._fill_target: int | None = None
        # パイプライン実行中は編集を止めるので、ハンドルも隠す（_set_running）
        self._fill_enabled = True

    def set_fill_enabled(self, enabled: bool) -> None:
        """フィルハンドルの表示・操作可否を切り替える。"""
        self._fill_enabled = enabled
        if not enabled:
            self._fill_source = None
            self._fill_target = None
        self.viewport().update()

    def setModel(self, model) -> None:
        super().setModel(model)
        sel = self.selectionModel()
        if sel is not None:
            # ハンドルの位置は現在セル・選択範囲に追従するので、変化したら再描画
            sel.currentChanged.connect(lambda *_: self.viewport().update())
            sel.selectionChanged.connect(lambda *_: self.viewport().update())

    # -- フィルハンドル ------------------------------------------------------

    def fill_anchor(self) -> tuple[int, int, int] | None:
        """フィルハンドルの基準 (列, 上端行, 下端行) を返す（対象外なら None）。

        現在セルが編集可能列にあるときだけ有効。選択行が現在行を含む連続
        範囲ならその範囲全体、そうでなければ現在行 1 行を元セルとする。
        """
        index = self.currentIndex()
        model = self.model()
        if not self._fill_enabled:
            return None
        if model is None or not index.isValid() or index.column() not in EDITABLE_COLUMNS:
            return None
        sel = self.selectionModel()
        rows = sorted({i.row() for i in sel.selectedRows()}) if sel is not None else []
        # 飛び飛びの選択は「どこからどこまで」が曖昧なので現在行だけを元にする
        if rows and index.row() in rows and rows == list(range(rows[0], rows[-1] + 1)):
            return index.column(), rows[0], rows[-1]
        return index.column(), index.row(), index.row()

    def fill_handle_rect(self) -> QRect | None:
        """フィルハンドル（右下の四角）の矩形。表示できないときは None。"""
        anchor = self.fill_anchor()
        if anchor is None:
            return None
        col, _, bottom = anchor
        rect = self.visualRect(self.model().index(bottom, col))
        if rect.isEmpty():
            return None  # スクロールで画面外
        size = _FILL_HANDLE_PX
        return QRect(rect.right() - size + 1, rect.bottom() - size + 1, size, size)

    def _row_at(self, y: int) -> int:
        """ビューポート座標 y の行番号。

        ビューポートの外まで引いた場合は「表示中の端の 1 行先」を返す。
        いきなり最終行まで飛ばさず、スクロール（mouseMoveEvent の scrollTo）
        と合わせて 1 行ずつ伸ばすため。
        """
        row = self.rowAt(y)
        if row >= 0:
            return row
        last = self.model().rowCount() - 1
        if y < 0:  # 上へはみ出した
            top = self.rowAt(0)
            return max(0, top - 1) if top >= 0 else 0
        bottom = self.rowAt(self.viewport().rect().bottom())
        return last if bottom < 0 else min(bottom + 1, last)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        model = self.model()
        if model is not None and model.rowCount() == 0:
            painter = QPainter(self.viewport())
            color = self.palette().text().color()
            color.setAlphaF(0.5)  # 薄字（プレースホルダ相当）
            painter.setPen(color)
            painter.drawText(
                self.viewport().rect(),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._EMPTY_HINT,
            )
            painter.end()
            return
        self._paint_fill_handle()

    def _paint_fill_handle(self) -> None:
        handle = self.fill_handle_rect()
        if handle is None:
            return
        painter = QPainter(self.viewport())
        # ドラッグ中はコピー先の範囲を枠線で示す（どこまで入るかの予告）
        if self._fill_source is not None and self._fill_target is not None:
            col, top, bottom = self._fill_source
            first = min(top, self._fill_target)
            last = max(bottom, self._fill_target)
            area = self.visualRect(self.model().index(first, col)).united(
                self.visualRect(self.model().index(last, col))
            )
            pen = QPen(_FILL_PREVIEW_PEN)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(area.adjusted(1, 1, -1, -1))
        painter.setPen(_FILL_HANDLE_BORDER)
        painter.setBrush(_FILL_HANDLE_BG)
        painter.drawRect(handle)
        painter.end()

    def mousePressEvent(self, event) -> None:
        handle = self.fill_handle_rect()
        pos = event.position().toPoint()
        if (
            event.button() == Qt.MouseButton.LeftButton
            and handle is not None
            # 小さな四角なので当たり判定は少し広げる
            and handle.adjusted(-2, -2, 2, 2).contains(pos)
        ):
            self._fill_source = self.fill_anchor()
            self._fill_target = self._fill_source[2] if self._fill_source else None
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if self._fill_source is None:
            # ハンドルの上ではカーソルを十字にして掴めることを示す
            handle = self.fill_handle_rect()
            if handle is not None and handle.adjusted(-2, -2, 2, 2).contains(pos):
                self.viewport().setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.viewport().unsetCursor()
            super().mouseMoveEvent(event)
            return
        self._fill_target = self._row_at(pos.y())
        # ビューポート外までドラッグしたときに追従スクロールする
        self.scrollTo(self.model().index(self._fill_target, self._fill_source[0]))
        self.viewport().update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._fill_source is None:
            super().mouseReleaseEvent(event)
            return
        col, top, bottom = self._fill_source
        target = self._fill_target
        self._fill_source = None
        self._fill_target = None
        self.viewport().update()
        event.accept()
        if target is not None:
            self._window.fill_from_handle(col, top, bottom, target)

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
