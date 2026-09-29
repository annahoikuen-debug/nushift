"""Streamlit UI のテスト。

``src/shiftai/ui/`` が存在しない場合（UI エージェントが未作業の場合）と
``streamlit_app.py`` が未作成の場合は **skip** する。CI で壊れないことを最優先する。
Streamlit サーバーは起動しない（``AppTest`` はプロセス内だけで完結する）。
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parent.parent
UI_PACKAGE = ROOT / "src" / "shiftai" / "ui"
APP_FILE = ROOT / "streamlit_app.py"


@pytest.fixture(scope="module")
def ui_package():
    """``shiftai.ui`` パッケージ。無ければ skip。"""
    if not UI_PACKAGE.is_dir():
        pytest.skip("src/shiftai/ui/ がまだ存在しません（UI 実装待ち）")
    return pytest.importorskip("shiftai.ui")


def test_ui_packageを読み込める(ui_package):
    """``shiftai.ui`` が例外なく import できること。"""
    assert ui_package is not None
    assert hasattr(ui_package, "__path__")


def test_uiの各モジュールをplain_importできる(ui_package):
    """配下の全モジュールが import 時に例外を投げないこと（副作用のある import 禁止）。"""
    names = [info.name for info in pkgutil.iter_modules(ui_package.__path__)]
    assert names, "shiftai.ui にモジュールが 1 つもありません"
    for name in sorted(names):
        module = importlib.import_module(f"shiftai.ui.{name}")
        assert module.__doc__, f"shiftai.ui.{name} に docstring がありません"


def test_app_fileは存在するかskipする():
    """``streamlit_app.py`` が無ければ skip（CI を壊さないため）。"""
    if not APP_FILE.is_file():
        pytest.skip("streamlit_app.py がまだ作成されていません")
    assert APP_FILE.is_file()


def test_app_fileが構文として正しい():
    """``streamlit_app.py`` が Python としてパースできること。"""
    if not APP_FILE.is_file():
        pytest.skip("streamlit_app.py がまだ作成されていません")
    import ast

    ast.parse(APP_FILE.read_text(encoding="utf-8"))


def _title_values(app: Any) -> list[str]:
    """``AppTest.title`` の文字列を返す。

    Streamlit のバージョンにより ``str`` の場合と要素リストの場合があるため、両方に対応する。
    """
    titles = app.title
    if isinstance(titles, str):
        return [titles]
    return [element.value for element in titles]


def test_app_testで例外が出ない():
    """Streamlit の ``AppTest`` でアプリを 1 フレーム流して例外が無いこと。"""
    if not APP_FILE.is_file():
        pytest.skip("streamlit_app.py がまだ作成されていません")
    pytest.importorskip("streamlit.testing.v1")
    from streamlit.testing.v1 import AppTest

    from shiftai.config import APP_TITLE

    app = AppTest.from_file(str(APP_FILE), default_timeout=120)
    app.run()
    assert not app.exception, [str(e.value) for e in app.exception]
    titles = _title_values(app)
    assert any(APP_TITLE in text for text in titles), titles
