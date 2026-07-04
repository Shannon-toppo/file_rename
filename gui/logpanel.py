# -*- coding: utf-8 -*-
"""折りたたみ式のログパネルと、Qt シグナル経由のロギングハンドラ。

スレッド境界の注意: logging のハンドラは **ワーカースレッドから呼ばれ得る**
（mv2title のパースフォールバック warning は LLM 呼び出し中に出る）。
そのため QtLogHandler はウィジェットに一切触らず、フォーマット済み文字列を
Signal で emit するだけにする（Signal.emit はスレッド安全）。ウィジェットへの
追記は LogPanel 側が QueuedConnection で受けてメインスレッドで行う。
"""
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QPlainTextEdit

# パネルが保持する最大行数（無限に溜めない）
_MAX_LINES = 1000

# ログレベルの選択肢（内部値, 表示ラベル）。設定ダイアログのコンボで使う。
# 既定は WARNING（yt-dlp の進捗行などの流量を抑える）
LOG_LEVELS = (
    ("DEBUG", "詳細（すべて）"),
    ("INFO", "情報"),
    ("WARNING", "警告（既定）"),
    ("ERROR", "エラーのみ"),
)


class QtLogHandler(QObject, logging.Handler):
    """logging のレコードを Qt シグナルへ変換するハンドラ。

    ウィジェット非依存・スレッド安全。message シグナルを QueuedConnection で
    受ければ、どのスレッドからのログでもメインスレッドで表示できる。
    """

    message = Signal(str)

    def __init__(self):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:  # logging.Handler の emit
        try:
            self.message.emit(self.format(record))
        except Exception:  # noqa: BLE001 - ログ経路では絶対に例外を漏らさない
            pass


class LogPanel(QPlainTextEdit):
    """読み取り専用のログ表示パネル（既定は非表示、[ログ] トグルで開閉）。"""

    def __init__(self, handler: QtLogHandler, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_LINES)
        self.setPlaceholderText("ログはここに表示されます（レベルは設定で変更可）")
        # ハンドラはワーカースレッドから emit するため必ず QueuedConnection で受ける
        handler.message.connect(self.appendPlainText, Qt.ConnectionType.QueuedConnection)


def attach_handler(
    handler: QtLogHandler, logger_names: tuple[str, ...] = ("mv2title", "core", "yt_dlp")
) -> None:
    """ハンドラを対象ロガーへアタッチする。

    フィルタはハンドラ側のレベル 1 箇所で行う方針のため、対象ロガー自体の
    レベルは DEBUG へ下げてレコードがハンドラまで届くようにする。実際の表示
    レベルは MainWindow が handler.setLevel(...) で切り替える（既定 WARNING）。
    変更前のロガーレベルはハンドラに退避し、detach_handler で復元する。
    """
    handler.setLevel(logging.WARNING)
    prev_levels: dict[str, int] = {}
    for name in logger_names:
        logger = logging.getLogger(name)
        prev_levels[name] = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    handler._prev_levels = prev_levels  # detach 用の退避（グローバル状態を戻す）


def detach_handler(
    handler: QtLogHandler, logger_names: tuple[str, ...] = ("mv2title", "core", "yt_dlp")
) -> None:
    """ハンドラを対象ロガーから外し、変更したロガーレベルを復元する。

    ウィンドウ close 時に呼ぶ（ハンドラの蓄積と、プロセス全体の logging
    設定を DEBUG に変えっぱなしにするのを防ぐ）。
    """
    prev_levels: dict[str, int] = getattr(handler, "_prev_levels", {})
    for name in logger_names:
        logger = logging.getLogger(name)
        logger.removeHandler(handler)
        if name in prev_levels:
            logger.setLevel(prev_levels[name])
