"""UI のウィジェットキー管理と月次コンテキストの渡し忘れを検知する静的ガード。

対象は ``docs/18_実装計画_残課題とリグレッション防止.md`` の WP-1 / WP-2 である。

* **WP-1（ウィジェットキー）**: ``state.reset_all()`` は ``WIDGET_KEYS`` と
  ``DYNAMIC_WIDGET_PREFIXES`` に載ったキーしか消せない。載っていない
  状態保持ウィジェットがあると「すべて初期化」のあとに旧設定が復活する
  （過去に ``patterns_enabled`` と ``pattern_snap`` で起きた類のバグ）。
  本ファイルは ``src/shiftai/ui`` の AST を走査し、状態保持ウィジェットの
  ``key=`` が必ず管理下にあることを機械的に検査する。
* **WP-2（月次コンテキスト）**: ``MonthlySolveContext`` には ``standard`` /
  ``weights`` フィールドがあるが、生成側で渡し忘れると
  ``context.standard`` が ``None`` のままになり、スコアの資格判定と
  重み付けが月次モードで無視される。週モードは渡しているので、
  週・月の両方を検査対象にする。

新しいウィジェットを足したときに本テストが赤くなったら、
``state.WIDGET_KEYS``（リテラルキー）か ``state.DYNAMIC_WIDGET_PREFIXES``
（ ``f"..."`` キー）に登録する。登録できない一時キーは
``EXEMPT_PREFIXES`` に**理由付きで**追加する。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from shiftai.ui import state

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "src" / "shiftai" / "ui"
SRC_DIR = ROOT / "src" / "shiftai"

#: 値を保持し、リセット対象になるウィジェット。
#: ``button`` / ``download_button`` は押下状態を保持しないため対象外、
#: ``dataframe`` / ``metric`` 等の表示系は結果側のキー（``DEFAULT_KEYS``）で
#: 管理されるため対象外。
STATEFUL_WIDGETS = frozenset(
    {
        "checkbox",
        "color_picker",
        "data_editor",
        "date_input",
        "file_uploader",
        "multiselect",
        "number_input",
        "radio",
        "select_slider",
        "selectbox",
        "slider",
        "text_input",
        "time_input",
        "toggle",
    }
)

#: リテラルで登録できないが、別経路でリセットされる動的キー。
#: 理由が無い限り追加しない（追加すると本ガードの検査が効かなくなる）。
EXEMPT_PREFIXES: dict[str, str] = {
    "editor_": "tab_data.reset_tables() が 3 表の data_editor を pop する",
    "upload_": "tab_data.reset_tables() が 3 表の file_uploader を pop する",
}


# ---------------------------------------------------------------------------
# AST ユーティリティ
# ---------------------------------------------------------------------------


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """モジュール直下の ``NAME = "文字列"`` を辞書で返す。"""
    consts: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
            if isinstance(target, ast.Name) and isinstance(value, ast.Constant):
                if isinstance(value.value, str):
                    consts[target.id] = value.value
    return consts


def _widget_key_nodes() -> Iterator[tuple[str, int, str, ast.expr, dict[str, str]]]:
    """``src/shiftai/ui`` の状態保持ウィジェットの ``key=`` を列挙する。"""
    for path in sorted(UI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        consts = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            if func.value.id != "st" or func.attr not in STATEFUL_WIDGETS:
                continue
            for keyword in node.keywords:
                if keyword.arg == "key":
                    yield (path.name, node.lineno, func.attr, keyword.value, consts)


def _resolve_key(
    node: ast.expr, consts: dict[str, str]
) -> tuple[str, str]:
    """``key=`` の式を ``("literal", 値)`` / ``("prefix", 接頭辞)`` / ``("unresolved", 表現)`` に。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ("literal", node.value)
    if isinstance(node, ast.JoinedStr):
        # 先頭からフォーマット差分に出るまでの静的な部分を接頭辞とみなす。
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
                continue
            break
        return ("prefix", "".join(parts))
    if isinstance(node, ast.Name):
        if node.id in consts:
            return ("literal", consts[node.id])
        return ("unresolved", node.id)
    if isinstance(node, ast.Attribute):
        # ``state.KEY_XXX`` のような属性参照。 ``shiftai.ui.state`` の定数から解決する。
        value = getattr(state, node.attr, None)
        if isinstance(value, str):
            return ("literal", value)
        return ("unresolved", node.attr)
    return ("unresolved", ast.dump(node))


def _monthly_context_calls(filename: str) -> list[ast.Call]:
    path = SRC_DIR / filename
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "MonthlySolveContext":
            calls.append(node)
    return calls


def _call_keyword_names(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


# ---------------------------------------------------------------------------
# WP-1: ウィジェットキーの管理下
# ---------------------------------------------------------------------------


def test_状態保持ウィジェットのリテラルキーが管理下にある():
    """``key="文字列"`` の状態保持ウィジェットは ``WIDGET_KEYS`` か ``DEFAULT_KEYS`` にあること。"""
    registered = set(state.WIDGET_KEYS) | set(state.DEFAULT_KEYS)
    offenders: list[str] = []
    for filename, lineno, widget, key_node, consts in _widget_key_nodes():
        kind, value = _resolve_key(key_node, consts)
        if kind != "literal":
            continue
        if value not in registered:
            offenders.append(f"{filename}:{lineno}: {widget}(key={value!r}) が未登録")
    assert not offenders, (
        "reset_all() で消えないウィジェットキーがある（WIDGET_KEYS に登録すること）:\n"
        + "\n".join(offenders)
    )


def test_状態保持ウィジェットの動的キーが接頭辞で管理下にある():
    """``key=f"..."`` の状態保持ウィジェットは接頭辞が管理下にあること。"""
    offenders: list[str] = []
    managed = set(state.DYNAMIC_WIDGET_PREFIXES)
    for filename, lineno, widget, key_node, consts in _widget_key_nodes():
        kind, value = _resolve_key(key_node, consts)
        if kind != "prefix":
            continue
        if value in managed or value in EXEMPT_PREFIXES:
            continue
        offenders.append(f"{filename}:{lineno}: {widget}(key=f\"{value}...\") の接頭辞が未管理")
    assert not offenders, (
        "reset_all() で消えない動的ウィジェットキーがある"
        "（DYNAMIC_WIDGET_PREFIXES に登録するか、理由付きで EXEMPT_PREFIXES に追加すること）:\n"
        + "\n".join(offenders)
    )


def test_ウィジェットキーの式がすべて解決できる():
    """``key=`` の式がリテラル・f-string・既知の定数に限られること。

    変数経由の ``key=`` は登録漏れを検出できないため、解決できなければ赤にする。
    """
    offenders: list[str] = []
    for filename, lineno, widget, key_node, consts in _widget_key_nodes():
        kind, value = _resolve_key(key_node, consts)
        if kind == "unresolved":
            offenders.append(f"{filename}:{lineno}: {widget} の key={value} を解決できない")
    assert not offenders, "解決できない key= がある（定数化するか EXEMPT に理由付きで追加）:\n" + "\n".join(
        offenders
    )


def test_除外リストの接頭辞はリセット経路を持つ():
    """``EXEMPT_PREFIXES`` の接頭辞が実際のソースで pop されていること。

    理由の書かれた消し忘れを防ぐ（書いた理由が嘘だったらテストが赤になる）。
    """
    sources = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(UI_DIR.glob("*.py"))
    }
    for prefix, reason in EXEMPT_PREFIXES.items():
        needle = f"pop(f\"{prefix}"
        found = any(needle in body for body in sources.values())
        assert found, f"{prefix!r}: {reason} というが pop(f\"{prefix}... がソースに無い"


# ---------------------------------------------------------------------------
# WP-2: 月次コンテキストへの standard / weights の渡し
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["__main__.py", "ui/tab_solve.py", "weekly.py"],
)
def test_月次コンテキストがstandardを渡している(filename: str):
    """全生成箇所が ``MonthlySolveContext(..., standard=...)`` を渡すこと。

    渡し忘れると ``context.standard`` が ``None`` のままになり、
    月次モードのスコア（``SHORTFALL_QUALIFIED``）が資格定義を無視する。
    """
    calls = _monthly_context_calls(filename)
    assert calls, f"{filename} に MonthlySolveContext の呼び出しが無い"
    offenders = [
        f"{filename}:{call.lineno}"
        for call in calls
        if "standard" not in _call_keyword_names(call)
    ]
    assert not offenders, "standard を渡していない月次コンテキストがある:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "filename",
    ["__main__.py", "ui/tab_solve.py"],
)
def test_週モードも引き続きstandardを渡している(filename: str):
    """月次対応のついでに週モードの ``standard`` 渡しを失わないこと。"""
    path = SRC_DIR / filename
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "solve_weekly"
    ]
    assert calls, f"{filename} に solve_weekly の呼び出しが無い"
    offenders = [
        f"{filename}:{call.lineno}"
        for call in calls
        if "standard" not in _call_keyword_names(call)
    ]
    assert not offenders, "solve_weekly が standard を受け取っていない:\n" + "\n".join(offenders)


def test_月次コンテキストのweightsフィールドが存在する():
    """``MonthlySolveContext`` は ``standard`` と ``weights`` を受け付けること。

    フィールドが消えたら WP-2 の渡し（および本ガード）が無意味になる。
    """
    from shiftai.monthly import MonthlySolveContext

    fields = set(getattr(MonthlySolveContext, "__dataclass_fields__", {}))
    assert "standard" in fields, "MonthlySolveContext.standard が無い"
    assert "weights" in fields, "MonthlySolveContext.weights が無い"
