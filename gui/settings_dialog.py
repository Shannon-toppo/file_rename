# -*- coding: utf-8 -*-
"""設定ダイアログ: 保存先フォルダ・既定形式・バッチサイズ・自動書き込み既定値。

接続設定（BASE_URL / MODEL）は ../mv2title/.env が持ち主なので編集させず、
現在値と .env の場所を読み取り専用で案内する。[接続テスト] は
core.check_connection（timeout 3 秒、UI ブロック許容）で疎通を確認する。
"""
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import core

from .logpanel import LOG_LEVELS

# テーマの選択肢（内部値, 表示ラベル）。既定はシステム追従
THEMES = (
    ("system", "システムに合わせる"),
    ("light", "ライト"),
    ("dark", "ダーク"),
)


class SettingsDialog(QDialog):
    """GUI の動作設定を編集するモーダルダイアログ。values() で結果を取り出す。"""

    def __init__(
        self,
        parent=None,
        *,
        out_dir: Path | None = None,
        fmt: str = "mp3",
        batch_size: int = core.BATCH_SIZE,
        auto_write: bool = True,
        expand_playlist: bool = False,
        theme: str = "system",
        log_level: str = "WARNING",
    ):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setModal(True)

        root = QVBoxLayout(self)
        form = QFormLayout()

        # 保存先フォルダ（参照ボタン付き）
        dir_row = QHBoxLayout()
        self._dir_edit = QLineEdit(str(out_dir if out_dir is not None else core.FILES_DIR))
        browse = QPushButton("参照...")
        browse.clicked.connect(self._on_browse)
        dir_row.addWidget(self._dir_edit, stretch=1)
        dir_row.addWidget(browse)
        form.addRow("保存先フォルダ", dir_row)

        # 既定形式
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(core.SUPPORTED_FORMATS)
        if fmt in core.SUPPORTED_FORMATS:
            self._fmt_combo.setCurrentText(fmt)
        form.addRow("既定の音声形式", self._fmt_combo)

        # バッチサイズ（1 回の LLM リクエストに含めるタイトル数）
        self._batch_spin = QSpinBox()
        self._batch_spin.setRange(1, 50)
        self._batch_spin.setValue(batch_size)
        form.addRow("推定バッチサイズ", self._batch_spin)

        # 自動書き込みの既定値
        self._auto_check = QCheckBox("起動時に自動書き込みを ON にする")
        self._auto_check.setChecked(auto_write)
        form.addRow("自動書き込み", self._auto_check)

        # 動画＋リスト混在 URL（watch?v=...&list=...）の扱い
        self._expand_check = QCheckBox("再生リスト付き動画 URL はリスト全体を展開する")
        self._expand_check.setChecked(expand_playlist)
        self._expand_check.setToolTip(
            "OFF: watch?v=...&list=... はその動画 1 本のみダウンロード（既定）\n"
            "ON: 含まれる再生リストの全動画を展開してダウンロード"
        )
        form.addRow("再生リスト", self._expand_check)

        # テーマ（アプリ全体の配色。既定は OS のテーマに追従）
        self._theme_combo = QComboBox()
        for value, label in THEMES:
            self._theme_combo.addItem(label, value)
        idx = self._theme_combo.findData(theme)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        form.addRow("テーマ", self._theme_combo)

        # ログレベル（[ログ] パネルに表示する最小レベル。既定は警告）
        self._log_level_combo = QComboBox()
        for value, label in LOG_LEVELS:
            self._log_level_combo.addItem(label, value)
        log_idx = self._log_level_combo.findData(log_level)
        if log_idx >= 0:
            self._log_level_combo.setCurrentIndex(log_idx)
        self._log_level_combo.setToolTip(
            "ログパネルに表示する最小レベル。\n"
            "「詳細（すべて）」は yt-dlp の進捗行も流れるため流量が多い。"
        )
        form.addRow("ログレベル", self._log_level_combo)
        root.addLayout(form)

        # 接続情報（読み取り専用の案内。編集は ../mv2title/.env で行う）
        env_path = core._ROOT / "mv2title" / ".env"
        try:
            config = core.Config.from_env()
            base_url, model = config.base_url, config.model
        except ValueError:
            base_url, model = "(未設定)", "-"
        info = QLabel(
            f"接続先 (BASE_URL): {base_url}\n"
            f"モデル (MODEL): {model}\n"
            f"変更する場合は {env_path} を編集してください。"
        )
        info.setWordWrap(True)
        root.addWidget(info)

        # 接続テスト
        test_row = QHBoxLayout()
        test_btn = QPushButton("接続テスト")
        test_btn.clicked.connect(self._on_test_connection)
        self._test_result = QLabel("")
        self._test_result.setWordWrap(True)
        test_row.addWidget(test_btn)
        test_row.addWidget(self._test_result, stretch=1)
        root.addLayout(test_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -- スロット ------------------------------------------------------------

    def _on_browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "保存先フォルダを選択", self._dir_edit.text())
        if path:
            self._dir_edit.setText(path)

    def _on_test_connection(self) -> None:
        # timeout 3 秒までの UI ブロックは許容（設定画面の明示操作のため）
        ok, msg = core.check_connection()
        self._test_result.setText(("OK: " if ok else "NG: ") + msg)

    # -- 結果 ---------------------------------------------------------------

    def values(self) -> dict:
        """ダイアログの現在値を dict で返す（OK 後に呼ぶ）。"""
        return {
            "out_dir": Path(self._dir_edit.text().strip() or str(core.FILES_DIR)),
            "fmt": self._fmt_combo.currentText(),
            "batch_size": self._batch_spin.value(),
            "auto_write": self._auto_check.isChecked(),
            "expand_playlist": self._expand_check.isChecked(),
            "theme": self._theme_combo.currentData(),
            "log_level": self._log_level_combo.currentData(),
        }
