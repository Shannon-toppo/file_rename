# -*- coding: utf-8 -*-
"""設定ダイアログ: 保存先フォルダ・既定形式・バッチサイズ・自動書き込み既定値。

接続設定（BASE_URL / API_KEY / MODEL / SYSTEM_PROMPT）は .env（開発時は
../mv2title/.env、凍結配布時は exe / .app 隣）を既定とし、このダイアログで
上書きできる（空欄 = .env の値を使う。上書きは QSettings に保存され、
core.apply_env_overrides で環境変数へ反映される）。[接続テスト] は
core.check_connection（timeout 3 秒、UI ブロック許容）で疎通を確認する。
"""
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
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
        normalize: bool = True,
        loudness: float = core.NORMALIZE_TARGET_I,
        trim_silence: bool = False,
        theme: str = "system",
        log_level: str = "WARNING",
        llm_overrides: dict | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setModal(True)
        # 保存先パスや BASE_URL が途中で切れない程度の最小幅を確保する
        self.setMinimumWidth(560)

        # 項目が多いので QGroupBox で「ダウンロード」「タイトル推定 (LLM)」
        # 「表示」の 3 グループに分ける（ウィジェット名・values() は従来のまま）
        root = QVBoxLayout(self)
        dl_group = QGroupBox("ダウンロード")
        form = QFormLayout(dl_group)
        # mac スタイルの既定はフィールドを sizeHint 幅のまま中央寄せするため、
        # パスや URL の欄が狭く切れる。行いっぱいまで伸ばす
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

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

        # 動画＋リスト混在 URL（watch?v=...&list=...）の扱い
        self._expand_check = QCheckBox("再生リスト付き動画 URL はリスト全体を展開する")
        self._expand_check.setChecked(expand_playlist)
        self._expand_check.setToolTip(
            "OFF: watch?v=...&list=... はその動画 1 本のみダウンロード（既定）\n"
            "ON: 含まれる再生リストの全動画を展開してダウンロード"
        )
        form.addRow("再生リスト", self._expand_check)

        # 音量ノーマライズ（DL 時に loudnorm を掛けて音量を揃える。既定 ON）
        self._normalize_check = QCheckBox("ダウンロード時に音量をノーマライズする")
        self._normalize_check.setChecked(normalize)
        self._normalize_check.setToolTip(
            "ON: ffmpeg の loudnorm で音量を EBU R128 相当へ揃える（既定）\n"
            "OFF: 元の音量のまま変換する"
        )
        form.addRow("音量ノーマライズ", self._normalize_check)

        # ノーマライズの基準値（loudnorm の統合ラウドネス I。TP / LRA は固定）
        self._loudness_spin = QDoubleSpinBox()
        self._loudness_spin.setRange(-30.0, -5.0)
        self._loudness_spin.setSingleStep(0.5)
        self._loudness_spin.setDecimals(1)
        self._loudness_spin.setSuffix(" LUFS")
        self._loudness_spin.setValue(loudness)
        self._loudness_spin.setToolTip(
            "loudnorm の統合ラウドネス目標 (I)。音楽配信の標準は -14 LUFS。\n"
            "値を上げるほど音圧が上がり、下げるほど静かになる。"
        )
        # ノーマライズ OFF のときは編集不可にする（値自体は保持）
        self._loudness_spin.setEnabled(self._normalize_check.isChecked())
        self._normalize_check.toggled.connect(self._loudness_spin.setEnabled)
        form.addRow("ノーマライズ基準値", self._loudness_spin)

        # 末尾の無音削除（試験的。DL 時の変換にのみ適用）
        self._trim_check = QCheckBox("末尾の無音区間を削除する（試験的）")
        self._trim_check.setChecked(trim_silence)
        self._trim_check.setToolTip(
            "ffmpeg の silenceremove で末尾の無音を削る。\n"
            "-50dB 以下だけを無音とみなし、1 秒は残す保守的な設定\n"
            "（フェードアウトや余韻など音楽本体は削らない）。"
        )
        form.addRow("無音削除", self._trim_check)
        root.addWidget(dl_group)

        # 推定と接続設定（.env を既定とし、ここで上書きできる。空欄 = .env の値）
        llm_group = QGroupBox("タイトル推定 (LLM)")
        llm = dict(llm_overrides or {})
        defaults = core.env_defaults()
        env_file = core.find_env_file()
        llm_form = QFormLayout(llm_group)
        llm_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # バッチサイズ（1 回の LLM リクエストに含めるタイトル数）
        self._batch_spin = QSpinBox()
        self._batch_spin.setRange(1, 50)
        self._batch_spin.setValue(batch_size)
        llm_form.addRow("推定バッチサイズ", self._batch_spin)

        # 自動書き込みの既定値
        self._auto_check = QCheckBox("起動時に自動書き込みを ON にする")
        self._auto_check.setChecked(auto_write)
        llm_form.addRow("自動書き込み", self._auto_check)

        def _placeholder(key: str, secret: bool = False) -> str:
            value = defaults.get(key)
            if not value:
                if key == "MODEL":
                    # ライブラリ側が黙って既定値で推論するため、実効値を明示する
                    # （「未設定」表示のまま既定モデル名でリクエストが飛ぶと、
                    # サーバー側のモデル名と合わず推論だけ失敗する事故になる）
                    return f"未設定（既定値 {core.DEFAULT_MODEL} を使用）"
                return "未設定（.env にもありません）"
            return ".env の値: " + ("(設定済み)" if secret else value)

        self._base_url_edit = QLineEdit(llm.get("BASE_URL", ""))
        self._base_url_edit.setPlaceholderText(_placeholder("BASE_URL"))
        llm_form.addRow("接続先 (BASE_URL)", self._base_url_edit)

        self._api_key_edit = QLineEdit(llm.get("API_KEY", ""))
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText(_placeholder("API_KEY", secret=True))
        llm_form.addRow("API キー (API_KEY)", self._api_key_edit)

        self._model_edit = QLineEdit(llm.get("MODEL", ""))
        self._model_edit.setPlaceholderText(_placeholder("MODEL"))
        llm_form.addRow("モデル (MODEL)", self._model_edit)

        self._prompt_edit = QPlainTextEdit(llm.get("SYSTEM_PROMPT", ""))
        self._prompt_edit.setPlaceholderText(_placeholder("SYSTEM_PROMPT"))
        self._prompt_edit.setFixedHeight(56)
        llm_form.addRow("システムプロンプト", self._prompt_edit)

        if env_file is not None:
            env_note = f"空欄の項目は {env_file} の値を使います。"
        else:
            env_note = (
                f".env が見つかりません（{core.app_dir() / '.env'} に置くか、上の欄に入力してください）。"
            )
        info = QLabel(env_note)
        info.setWordWrap(True)
        llm_form.addRow(info)

        # 接続テスト
        test_row = QHBoxLayout()
        test_btn = QPushButton("接続テスト")
        test_btn.clicked.connect(self._on_test_connection)
        self._test_result = QLabel("")
        self._test_result.setWordWrap(True)
        test_row.addWidget(test_btn)
        test_row.addWidget(self._test_result, stretch=1)
        llm_form.addRow(test_row)
        root.addWidget(llm_group)

        # 表示（アプリ全体の見た目とログ出力）
        view_group = QGroupBox("表示")
        view_form = QFormLayout(view_group)

        # テーマ（アプリ全体の配色。既定は OS のテーマに追従）
        self._theme_combo = QComboBox()
        for value, label in THEMES:
            self._theme_combo.addItem(label, value)
        idx = self._theme_combo.findData(theme)
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)
        view_form.addRow("テーマ", self._theme_combo)

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
        view_form.addRow("ログレベル", self._log_level_combo)
        root.addWidget(view_group)

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
        # timeout 3 秒までの UI ブロックは許容（設定画面の明示操作のため）。
        # ダイアログの上書き値を一時的に環境変数へ載せてテストし、確定前なので
        # 終わったら元へ戻す（OK せずに閉じても環境を汚さない）。
        saved = {k: os.environ.get(k) for k in core.ENV_KEYS}
        try:
            core.apply_env_overrides(self._llm_values())
            ok, msg = core.check_connection()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        # 成功時の msg は「接続 OK: ...」で始まるため、"OK: " を重ねない
        self._test_result.setText(msg if ok else "NG: " + msg)

    # -- 結果 ---------------------------------------------------------------

    def _llm_values(self) -> dict[str, str]:
        """接続設定の上書き値（空欄は空文字 = 上書きしない）を返す。"""
        return {
            "BASE_URL": self._base_url_edit.text().strip(),
            "API_KEY": self._api_key_edit.text().strip(),
            "MODEL": self._model_edit.text().strip(),
            "SYSTEM_PROMPT": self._prompt_edit.toPlainText().strip(),
        }

    def values(self) -> dict:
        """ダイアログの現在値を dict で返す（OK 後に呼ぶ）。"""
        return {
            "llm_overrides": self._llm_values(),
            "out_dir": Path(self._dir_edit.text().strip() or str(core.FILES_DIR)),
            "fmt": self._fmt_combo.currentText(),
            "batch_size": self._batch_spin.value(),
            "auto_write": self._auto_check.isChecked(),
            "expand_playlist": self._expand_check.isChecked(),
            "normalize": self._normalize_check.isChecked(),
            "loudness": self._loudness_spin.value(),
            "trim_silence": self._trim_check.isChecked(),
            "theme": self._theme_combo.currentData(),
            "log_level": self._log_level_combo.currentData(),
        }
