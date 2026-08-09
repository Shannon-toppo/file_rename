# -*- coding: utf-8 -*-
"""Qt 標準 UI の日本語化（翻訳ファイルの読み込み）。

テキスト入力欄の右クリックメニュー（元に戻す/切り取り/コピー/貼り付け/
すべてを選択）やファイルダイアログのボタンは Qt 側が持っている文言なので、
Qt 同梱の qtbase_ja.qm を読み込まないと英語のまま出る。アプリ自身の文言は
最初から日本語なので、ここで面倒を見るのは Qt 標準部分だけ。

QApplication 生成直後に install_qt_translators(app) を呼ぶ（gui/__main__.py）。
"""
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QTranslator

# 読み込む翻訳カタログ。qtbase に QtCore/QtGui/QtWidgets の標準文言が入る
_CATALOGS = ("qtbase_ja",)


def translation_dirs() -> list[Path]:
    """qm ファイルを探すディレクトリ（優先順）。

    ① QLibraryInfo が示す translations（通常はこれで当たる。PyInstaller の
    frozen ビルドでも qt.conf 経由で _internal 配下を指す）
    ② PySide6 パッケージ直下（qt.conf が無い等で ① が外れたときの保険）
    """
    dirs: list[Path] = []
    configured = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if configured:
        dirs.append(Path(configured))
    import PySide6

    dirs.append(Path(PySide6.__file__).resolve().parent / "Qt" / "translations")
    return dirs


def install_qt_translators(app) -> list[QTranslator]:
    """Qt 標準文言の日本語カタログを app へインストールする。

    見つからなければ何もしない（英語のまま動作する）。戻り値は
    インストールできた QTranslator のリスト（テスト用）。
    """
    dirs = [str(d) for d in translation_dirs()]
    installed: list[QTranslator] = []
    for name in _CATALOGS:
        # app を親にして GC で翻訳が外れないようにする
        translator = QTranslator(app)
        if any(translator.load(name, d) for d in dirs):
            app.installTranslator(translator)
            installed.append(translator)
    return installed
