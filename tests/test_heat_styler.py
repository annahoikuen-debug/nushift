"""``components.heat_styler`` の回帰テスト。

``heat_styler`` は matplotlib を使わず自前で色補間する ``Styler`` を組み立てる。
2026-09 時点で次の 2 つのバグが修正され、本ファイルはその修正を固定する。

1. ``NaN`` を含む DataFrame で正規化範囲が ``NaN`` に汚染され、
   ``ValueError: cannot convert float NaN to integer`` になっていた
   （``np.isfinite`` による除外で修正）。
2. ``style.format("{:.0f}")`` が文字列列にも適用され、
   ``ValueError: Unknown format code 'f' for object of type 'str'`` になっていた。
   ``tab_requirements._render_gap_preview`` は日付列つきの ``gap_matrix`` を
   ``subset`` 指定で渡しているため、优化後にタブを開き直すと必ず落ちていた
   （``format`` を数値列にだけ適用する形で修正）。

一部が「例外を出さない」だけでなく「実際に色が付き、その色が正しい」ことを
併せて検証する。色は HTML の ``<td>`` ごとに解析して判定する。
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd
import pytest

from shiftai.ui import components

NAN = float("nan")

pytestmark = pytest.mark.timeout(300)


# --------------------------------------------------------------------------
# 補助
# --------------------------------------------------------------------------

_TD_RE = re.compile(r'<td\b[^>]*\bid="[^"]*?_row(\d+)_col(\d+)"[^>]*>(.*?)</td>', re.S)
_CSS_RE = re.compile(r"#T_\w+_row(\d+)_col(\d+)\s*\{([^}]*)\}")
_BG_RE = re.compile(r"background-color:\s*([^;\"]+)")


def _render(frame: pd.DataFrame, **kwargs: Any) -> tuple[str | None, Any]:
    """``heat_styler`` を呼んで HTML を作る。空表は ``(None, frame)`` を返す。"""
    styled = components.heat_styler(frame, **kwargs)
    if styled is frame:  # 空 DataFrame は ``Styler`` を組み立てずにそのまま返す
        return None, styled
    return styled.to_html(), styled


def _css(html: str) -> dict[tuple[int, int], str]:
    """``<style>`` ブロックから ``{(行, 列): 宣言文字列}`` を取り出す。"""
    return {(int(r), int(c)): body for r, c, body in _CSS_RE.findall(html)}


def _texts(html: str) -> list[list[str]]:
    """HTML を ``[[セル文字列, ...], ...]``（行 × 列）へ分解する。"""
    grid: dict[tuple[int, int], str] = {}
    for row, col, raw in _TD_RE.findall(html):
        grid[(int(row), int(col))] = re.sub(r"<[^>]+>", "", raw).strip()
    height = max(r for r, _ in grid) + 1
    width = max(c for _, c in grid) + 1
    return [[grid.get((r, c), "") for c in range(width)] for r in range(height)]


def _color(html: str, row: int, col: int) -> str | None:
    """``(行, 列)`` セルの背景色。無色なら ``None``。"""
    body = _css(html).get((row, col), "")
    match = _BG_RE.search(body)
    return match.group(1).strip() if match else None


# --------------------------------------------------------------------------
# 1. 修正前は落ちていた／今，通る組み合わせ
# --------------------------------------------------------------------------


def _case(
    frame: pd.DataFrame,
    kwargs: dict[str, Any] | None = None,
    reason: str = "",
) -> Any:
    html, styled = _render(frame, **(kwargs or {}))
    if html is not None:
        assert html, reason or "to_html() が空文字列を返した"
    return styled


@pytest.mark.parametrize(
    ("case_id", "frame", "kwargs", "reason"),
    [
        (
            "文字列列とsubset",
            pd.DataFrame(
                {
                    "日付": ["2026-09-28", "2026-09-29"],
                    "a": [3, 4],
                    "b": [2, 5],
                }
            ),
            {"subset": ["a", "b"]},
            "tab_requirements._render_gap_preview が日付列つきの gap_matrix を "
            "subset 指定で渡すため、修正前は format('{:.0f}') が日付に適用されて "
            "ValueError: Unknown format code 'f' になりタブを開き直すと落ちた",
        ),
        (
            "全セルNaN",
            pd.DataFrame({"a": [NAN, NAN], "b": [NAN, NAN]}),
            {},
            "有限値が 1 つも無いと min/max が NaN のまま伝播し、"
            "変換時に ValueError: cannot convert float NaN to integer になった",
        ),
        (
            "一部NaN",
            pd.DataFrame({"a": [1.0, NAN], "b": [NAN, NAN]}),
            {},
            "NaN が 1 つでも混ざると min/max が汚染される。過不足表のような "
            "部分欠損が現実には一番多い",
        ),
        (
            "値が1つだけ",
            pd.DataFrame({"a": [3], "b": [3]}),
            {},
            "min == max になり span（分母）が 0 になる。"
            "ガードが無いと ZeroDivisionError ではなく色変換の例外になる",
        ),
        (
            "文字列列のみsubset無し",
            pd.DataFrame({"x": ["a", "b"], "n": [1, 2]}),
            {},
            "subset を付けない運用では文字列列も format の対象候補になる。"
            "文字列かどうかの判定が正しいことを到这里までの subset 指定だけに頼らず確認する",
        ),
        (
            "subsetが全NaN",
            pd.DataFrame({"x": ["a"], "n": [NAN]}),
            {"subset": ["n"]},
            "subset の中だけが空だと、表全体の数値列には値がある場合でも "
            "正規化範囲の算出が壊れないこと",
        ),
        (
            "空DataFrame",
            pd.DataFrame(),
            {},
            "0 行 × 0 列では Styler を組み立てられないため、DataFrame をそのまま返す",
        ),
        (
            "vmin大于vmax",
            pd.DataFrame({"a": [1, 2]}),
            {"vmin": 5, "vmax": 1},
            "vmin > vmax という不正入力でも span が負にならず 0 除算もしないこと",
        ),
        (
            "負の値を含む",
            pd.DataFrame({"a": [-3, 2, 0]}),
            {},
            "過不足Heatmapは負値を必ず含む。ratio が 0 未満にならないこと",
        ),
        (
            "1行だけ",
            pd.DataFrame({"a": [7]}),
            {},
            "1 行だけだと各列が min == max になり span が 0。複数行とは別経路の縮退",
        ),
        (
            "infを含む",
            pd.DataFrame({"a": [np.inf, 1.0]}),
            {},
            "inf は NaN と共に『有限値ではない値』。np.isfinite で両方を弾く必要がある",
        ),
        (
            "objectdtypeの数値",
            pd.DataFrame({"a": pd.Series([1, 2], dtype=object)}),
            {},
            "CSV 由来の列は object dtype のままで入ることがある。"
            "dtype.kind による format 対象判定と色付けの両方が耐えること",
        ),
    ],
)
def test_heat_stylerは_to_htmlまで到達する(case_id, frame, kwargs, reason):
    """表の各ケースが例外なくレンダリングできること（修正の回帰防止）。"""
    _case(frame, kwargs, reason)


def test_heat_stylerは空DataFrameをそのまま返す():
    """空表に色付けしない（``Styler`` を組み立てると落ちる）こと。"""
    empty = pd.DataFrame()
    assert components.heat_styler(empty) is empty


# --------------------------------------------------------------------------
# 2. 色付けが実際に起きているか
# --------------------------------------------------------------------------


def test_heat_stylerは通常ケースで背景色を付ける():
    """``background-color`` が出力に含まれ、色付けが実際に起きていること。"""
    html, _ = _render(pd.DataFrame({"A": [0, 1, 2], "B": [2, 1, 0]}))
    assert "background-color" in html


def test_heat_stylerは最小値と最大値で色が変わる():
    """最小セルと最大セルが同一色にならないこと（線形補間が効いている）。"""
    html, _ = _render(pd.DataFrame({"A": [0, 5]}))
    low, high = _color(html, 0, 0), _color(html, 1, 0)
    assert low and high
    assert low != high


def test_heat_stylerはvmin_vmaxの範囲で正規化する():
    """``vmin``/``vmax`` を明示すると、その範囲の端が色見本の両端になること。

    値域 0〜10 の表に ``vmin=0, vmax=100`` を渡すと最大値の比率は 0.1 に落ち、
    「実際の値域で計算した場合」より淡い色になる。範囲が実際の値域ではなく
    指定値で決まることの証拠。
    """
    frame = pd.DataFrame({"A": [0, 10]})
    wide, _ = _render(frame, vmin=0.0, vmax=100.0)
    auto, _ = _render(frame)
    assert _color(wide, 1, 0) != _color(auto, 1, 0)  # 既定の値域より淡い
    assert _color(wide, 0, 0) == _color(auto, 0, 0)  # 最小値 vmin=0 は共通
    assert _color(wide, 1, 0) != _color(wide, 0, 0)


def test_heat_stylerはNaNセルに色を付けない():
    """``NaN`` セルは ``paint`` が ``""`` を返し色なしのまま描かれること。"""
    html, _ = _render(pd.DataFrame({"A": [NAN, 5.0]}))
    assert _color(html, 0, 0) is None
    assert _color(html, 1, 0) is not None
    assert _texts(html)[0][0] == "—"  # na_rep でダッシュ表示


def test_heat_stylerはinfセルに色を付けない():
    """``inf`` も有限値ではないため色なし，色なしのまま描画されること。"""
    html, _ = _render(pd.DataFrame({"A": [np.inf, 1.0]}))
    assert _color(html, 0, 0) is None
    assert _color(html, 1, 0) is not None


def test_heat_stylerはsubsetの中だけに色を付ける():
    """``subset`` 指定時、その列にしか背景色が付かないこと。"""
    html, _ = _render(pd.DataFrame({"A": [0, 5], "B": [0, 5]}), subset=["A"])
    assert _color(html, 0, 0) is not None
    assert _color(html, 1, 0) is not None
    assert _color(html, 0, 1) is None
    assert _color(html, 1, 1) is None
    assert html.count("background-color") == 2


# --------------------------------------------------------------------------
# 3. 文字列列が壊れないこと
# --------------------------------------------------------------------------


def test_heat_stylerは文字列列を数値フォーマットにしない():
    """``'{:.0f}'`` の生文字列も ``nan`` の生文字列も出力に混ざらないこと。"""
    frame = pd.DataFrame(
        {"日付": ["2026-09-28", "2026-09-29"], "a": [3.0, float("nan")]}
    )
    html, _ = _render(frame, subset=["a"])
    assert "{:.0f}" not in html
    assert "nan" not in html
    assert "NaN" not in html


def test_heat_stylerは日付文字列をそのまま表示する():
    """日付文字列が ``'{:.0f}'`` や空文字に化けないこと（回帰の中心）。"""
    frame = pd.DataFrame({"日付": ["2026-09-28", "2026-09-29"], "a": [3, 4]})
    html, _ = _render(frame, subset=["a"])
    texts = [text for row in _texts(html) for text in row]
    assert "2026-09-28" in texts
    assert "2026-09-29" in texts


def test_heat_stylerは文字列列に色を付けない():
    """数値に変換できない文字列は ``paint`` が ``""`` を返し色なしになること。"""
    html, _ = _render(pd.DataFrame({"x": ["a", "b"], "n": [1, 2]}))
    assert _color(html, 0, 0) is None
    assert _color(html, 1, 0) is None
    assert _color(html, 0, 1) is not None
