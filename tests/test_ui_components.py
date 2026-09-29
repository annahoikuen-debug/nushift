"""``shiftai.ui.components`` のテスト。

``components.py`` は「純粋な DataFrame ヘルパー」と「Streamlit ウィジェットを
直接描く関数」が混在している。这里では

* Streamlit 実行コンテキストが無くても動く関数 → 通常の関数呼び出しで検証
* ``st.markdown`` / ``st.columns`` / ``st.expander`` を必要とする関数 →
  ``streamlit.testing.v1.AppTest`` の小さなスクリプト経由で検証

の 2 系統に分ける。サーバーは起動せず、ブラウザもネットワークも使わない。
"""

from __future__ import annotations

import logging
import pickle
from datetime import date, time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import streamlit as st

from shiftai import gap_analysis
from shiftai.domain import CellState, Violation, ViolationSeverity
from shiftai.ui import components, state

logging.getLogger("streamlit").setLevel(logging.ERROR)

pytestmark = pytest.mark.timeout(300)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

app_test = pytest.importorskip("streamlit.testing.v1", reason="AppTest が無い環境ではスキップ")


# --------------------------------------------------------------------------
# フィクスチャ
# --------------------------------------------------------------------------


@pytest.fixture
def slots_list(slots):
    """時間帯の ``list``（``Sequence`` 引数用）。"""
    return list(slots)


@pytest.fixture
def day_shift(solved_day, one_day):
    """1 日分の ``ShiftDay``。"""
    return solved_day.day(one_day[0])


@pytest.fixture
def violations(small_requirements, solved_day, small_staff, small_preferences, standard):
    """実際の違反一覧。"""
    return gap_analysis.check_violations(
        small_requirements,
        solved_day,
        small_staff,
        small_preferences,
        standard=standard,
    )


@pytest.fixture
def load_issues():
    """``LoadIssue`` 相当の簡易オブジェクト。"""
    from shiftai.data_loader import LoadIssue

    return [
        LoadIssue(level="error", row=0, column="職員ID", message="職員IDが空です"),
        LoadIssue(level="warning", row=3, column="週契約時間", message="0 を 0 に補完しました"),
    ]


@pytest.fixture(autouse=True)
def clean_session_state():
    """``st.session_state`` を空にしてテスト間の依存をなくす。"""
    st.session_state.clear()
    yield
    st.session_state.clear()


def _make_result(status_name: str) -> Any:
    """指定ステータスの空 ``SolveResult`` を作る。"""
    from shiftai.domain import SolveResult, SolveStatus

    return SolveResult(
        status=SolveStatus[status_name],
        shift_days=[],
        assignments=[],
        violations=[],
        messages=[],
        stats={},
    )


def _make_violation(
    severity: ViolationSeverity, code: str, message: str, day: date, slot: Any, staff_id: str
) -> Violation:
    """描画テスト用の ``Violation`` を作る。"""
    return Violation(
        severity=severity,
        code=code,
        message=message,
        day=day,
        slot=slot,
        staff_id=staff_id,
        detail={},
    )


def _run_widget_script(tmp_path: Path, body: str, *, payload: Any = None) -> Any:
    """``components`` のウィジェット描画関数をヘッドレスに実行して ``AppTest`` を返す。"""
    blob = None
    if payload is not None:
        blob = tmp_path / "payload.pkl"
        blob.write_bytes(pickle.dumps(payload))
    prelude = (
        "import pickle\n"
        f"PAYLOAD = pickle.load(open({str(blob)!r}, 'rb'))\n"
        if blob is not None
        else "PAYLOAD = None\n"
    )
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "import streamlit as st\n"
        "from shiftai.ui import components, state\n"
        f"{prelude}"
        "state.init_state()\n"
        f"{body}\n"
    )
    app_file = tmp_path / "widget_script.py"
    app_file.write_text(script, encoding="utf-8")
    at = app_test.AppTest.from_file(str(app_file), default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


# --------------------------------------------------------------------------
# ラベル・凡例
# --------------------------------------------------------------------------


def test_violation_labelsはgap_analysisの辞書を優先する():
    """``gap_analysis.VIOLATION_CODE_LABELS`` があればそちらを上書きで優先すること。"""
    labels = components.violation_labels()
    assert labels["COVERAGE_SHORTFALL"]
    for key, value in gap_analysis.VIOLATION_CODE_LABELS.items():
        assert labels[str(key)] == str(value)


def test_violation_labelsは内側の辞書を欠落コードで埋める(monkeypatch):
    """``VIOLATION_CODE_LABELS`` を消しても内蔵辞書で全コードに日本語名が付くこと。"""
    monkeypatch.delattr(gap_analysis, "VIOLATION_CODE_LABELS", raising=False)
    labels = components.violation_labels()
    assert labels == components.FALLBACK_VIOLATION_LABELS
    assert "配置基準を満たしていません" in labels.values()


def test_violation_labelsは空辞書のとき内蔵辞書へフォールバックする(monkeypatch):
    """空の dict は「未定義」扱いになり内蔵辞書が使われること。"""
    monkeypatch.setattr(gap_analysis, "VIOLATION_CODE_LABELS", {}, raising=False)
    assert components.violation_labels() == components.FALLBACK_VIOLATION_LABELS


def test_violation_titleは未知コードをそのまま返す():
    """未知のコードは「名前csharpなし」で自己説明できること。"""
    assert components.violation_title("MYSTERY_CODE") == "MYSTERY_CODE"
    assert components.violation_title("COVERAGE_SHORTFALL") != "COVERAGE_SHORTFALL"


def test_staff_labelsはIDと氏名のラベルを返す(small_staff):
    """グリッドの行ラベルが ``(ID, "ID 氏名")`` の組になること。"""
    labels = components.staff_labels(small_staff)
    assert labels[0] == ("S001", "S001 保育士1")
    assert len(labels) == len(small_staff)


def test_staff_labelsは空でも空リストになる():
    """0 名のときも例外にせず空リストを返すこと。"""
    assert components.staff_labels([]) == []


def test_row_labelは空白区切りの1行になる():
    """``tab_shift._apply_edits`` が ``split(" ", 1)`` で ID を取り出せる形式であること。"""
    assert components.row_label("S001", "保育士1") == "S001 保育士1"
    assert components.row_label("S001", "保育士1").split(" ", 1)[0] == "S001"


def test_format_dayは日本語日付になる():
    """selectbox の ``format_func`` として 9/28(月) 形式を出すこと。"""
    assert components.format_day(date(2026, 9, 28)) == "9/28(月)"


# --------------------------------------------------------------------------
# シフトグリッド
# --------------------------------------------------------------------------


def test_shift_grid_frameは列名と並び順を保つ(day_shift, slots_list, small_staff):
    """列が時間帯順・index が「職員（ID 氏名）」で、行が職員順であること。"""
    frame = components.shift_grid_frame(day_shift, slots_list, small_staff)
    assert list(frame.columns) == [s.label for s in slots_list]
    assert list(frame.index) == [f"{m.staff_id} {m.name}" for m in small_staff]
    assert frame.index.name == "職員（ID 氏名）"
    assert len(frame) == len(small_staff)
    assert set(frame.to_numpy().ravel()) <= {s.value for s in CellState}


def test_shift_grid_frameはshift_dayがNoneでも列だけ返す(slots_list, small_staff):
    """シフト未作成でも「時間帯は分かる」空グリッドを返すこと。"""
    frame = components.shift_grid_frame(None, slots_list, small_staff)
    assert list(frame.columns) == [s.label for s in slots_list]
    assert frame.empty
    assert frame.index.name == "職員"


def test_shift_grid_frameは確定セルにロック記号を付ける(
    day_shift, slots_list, small_staff, one_day
):
    """手動確定したセルだけ ``🔒`` が付き、他のセルが変化しないこと。"""
    day = one_day[0]
    target = (small_staff[0].staff_id, day, slots_list[0].label)
    frame = components.shift_grid_frame(
        day_shift, slots_list, small_staff, day=day, fixed={target: CellState.WORK}
    )
    assert frame.iloc[0, 0].startswith(components.LOCK)
    assert not frame.iloc[1, 0].startswith(components.LOCK)


def test_shift_grid_frameは固定情報がなければ素の値を返す(
    day_shift, slots_list, small_staff, one_day
):
    """``fixed`` 未指定 or 空でも値が Lock 되지 そのまま入ること。"""
    day = one_day[0]
    plain = components.shift_grid_frame(day_shift, slots_list, small_staff, day=day)
    assert not any(
        str(v).startswith(components.LOCK) for v in plain.to_numpy().ravel()
    )
    empty = components.shift_grid_frame(
        day_shift, slots_list, small_staff, day=day, fixed={}
    )
    assert empty.equals(plain)


def test_editable_grid_frameは職員列が先頭でindexが0始まり(
    day_shift, slots_list, small_staff
):
    """``st.data_editor`` の差分検出用に index ではなく列で対応付けること。"""
    frame = components.editable_grid_frame(day_shift, slots_list, small_staff)
    assert list(frame.columns) == ["職員", *[s.label for s in slots_list]]
    assert list(frame["職員"]) == [f"{m.staff_id} {m.name}" for m in small_staff]
    assert list(frame.index) == list(range(len(small_staff)))


def test_editable_grid_frameはshift_dayがNoneでも列だけ返す(slots_list, small_staff):
    """未作成でも列定義迫n't 壊れないこと。"""
    frame = components.editable_grid_frame(None, slots_list, small_staff)
    assert list(frame.columns) == ["職員", *[s.label for s in slots_list]]
    assert frame.empty


def test_editable_column_configは全時間帯に選択肢を持たせる(slots_list):
    """全セルが ``勤務/休憩/オフ`` のみから選べること。"""
    config = components.editable_column_config(slots_list)
    assert "職員" in config
    for slot in slots_list:
        assert slot.label in config
        options = config[slot.label]["type_config"]["options"]
        assert list(options) == [s.value for s in CellState]


def test_diff_editsは変更セルだけを返す(day_shift, slots_list, small_staff):
    """1 セルだけ変えると ``{(0, 時間帯): 新しい値}`` 1 件になること。"""
    baseline = components.editable_grid_frame(day_shift, slots_list, small_staff)
    column = slots_list[0].label
    before = baseline.loc[0, column]
    after = CellState.OFF.value if before != CellState.OFF.value else CellState.WORK.value
    edited = baseline.copy()
    edited.loc[0, column] = after

    changes = components.diff_edits(edited, baseline)

    assert changes == {(0, column): after}


def test_diff_editsは変更が無ければ空辞書(day_shift, slots_list, small_staff):
    """編集していなければ「変更なし」の UI が出る前提で空を返すこと。"""
    baseline = components.editable_grid_frame(day_shift, slots_list, small_staff)
    assert components.diff_edits(baseline, baseline) == {}


@pytest.mark.parametrize(
    ("edited", "baseline"),
    [(None, None), (None, "x"), ("x", None)],
)
def test_diff_editsはNoneを受け取れる(edited, baseline):
    """``st.data_editor`` が ``None`` を返したフレームでも例外にしないこと。"""
    assert components.diff_edits(edited, baseline) == {}


def test_diff_editsは空DataFrameを受け取れる():
    """空テーブル同士は「差分なし」扱いになること。"""
    assert components.diff_edits(pd.DataFrame(), pd.DataFrame()) == {}


def test_diff_editsは職員列を比較対象外にする(day_shift, slots_list, small_staff):
    """職員名だけを変えても確定セルは増えないこと。"""
    baseline = components.editable_grid_frame(day_shift, slots_list, small_staff)
    edited = baseline.copy()
    edited.loc[0, "職員"] = "S999 別人"
    assert components.diff_edits(edited, baseline) == {}


def test_diff_editsは行数の差を安全に見積る(day_shift, slots_list, small_staff):
    """編集後に行が減っても範囲外で ``IndexError`` を出さないこと。"""
    baseline = components.editable_grid_frame(day_shift, slots_list, small_staff)
    column = slots_list[0].label
    after = (
        CellState.OFF.value
        if baseline.loc[0, column] != CellState.OFF.value
        else CellState.WORK.value
    )
    edited = baseline.head(1).copy()
    edited.loc[0, column] = after
    assert components.diff_edits(edited, baseline) == {(0, column): after}


# --------------------------------------------------------------------------
# 色付け
# --------------------------------------------------------------------------


def test_heat_stylerは空DataFrameをそのまま返す():
    """空表に色付けしない（``Styler`` を組み立てると落ちる）こと。"""
    empty = pd.DataFrame()
    assert components.heat_styler(empty) is empty


def test_heat_stylerは数値列に背景色を付ける():
    """最小値を淡く最大値を濃くする線形補間が入ること。"""
    frame = pd.DataFrame({"A": [0, 1, 2], "B": [2, 1, 0]})
    styled = components.heat_styler(frame)
    html = styled.to_html()
    assert "background-color" in html


def test_heat_stylerはvminとvmaxを明示するとNaNを耐える():
    """``vmin`` / ``vmax`` を渡すと ``NaN`` セルが安全に色なしになること。

    ``heat_styler`` は既定で表全体の min/max を正規化に使うため、``NaN`` があると
    壊れる（下記の xfail が記録した既知のバグ）。範囲を明示すれば回避できる。
    """
    frame = pd.DataFrame({"A": [float("nan"), 1.0], "B": [0.0, 2.0]})
    html = components.heat_styler(frame, vmin=0.0, vmax=2.0).to_html()
    assert isinstance(html, str) and html
    assert "background-color" in html


def test_heat_stylerは既定引数でNaNがあっても落ちない():
    """``NaN`` を含む表を既定引数で色付けしても例外にならないこと（回帰防止）。

    修正前は正規化に使う min/max が ``NaN`` に汚染され
    ``ValueError: cannot convert float NaN to integer`` になっていた。
    詳細な組み合わせは ``tests/test_heat_styler.py`` を参照。
    """
    frame = pd.DataFrame({"A": [float("nan"), 1.0], "B": [0.0, 2.0]})
    assert "background-color" in components.heat_styler(frame).to_html()


def test_heat_stylerは文字列列があっても落ちない():
    """文字列列（例: 日付）つきの表が描画できること（回帰防止）。

    修正前は ``style.format("{:.0f}")`` が文字列列にも適用され
    ``ValueError: Unknown format code 'f' for object of type 'str'`` になっていた。
    ``tab_requirements._render_gap_preview`` が日付列つきの ``gap_matrix`` を
    ``subset`` 指定で渡しているため、优化後にタブを開き直すと必ず落ちていた。
    """
    frame = pd.DataFrame({"日付": ["2026-09-28"], "不足": [1]})
    assert components.heat_styler(frame, subset=["不足"]).to_html()


def test_heat_stylerはsubsetを指定できる():
    """``合計`` など数値でない列を除外してもレンダリングできること。"""
    frame = pd.DataFrame({"A": [0, 1], "B": [1, 0], "合計": [1, 1]})
    assert components.heat_styler(frame, subset=["A", "B"]) is not None


def test_heat_stylerは未知のscale名でも描画できる():
    """色見本がなくても既定の ``warm`` で描画されること。"""
    frame = pd.DataFrame({"A": [0, 1]})
    assert components.heat_styler(frame, scale="存在しないスケール") is not None


def test_heat_stylerはvmin_vmaxで固定できる():
    """過不足表のように -3〜3 で正規化したいケースを扱えること。"""
    frame = pd.DataFrame({"A": [-5, 5]})
    styled = components.heat_styler(frame, scale="shortfall", vmin=-3.0, vmax=3.0)
    assert "background-color" in styled.to_html()


def test_heat_stylerは数値が全て同じでも落ちない():
    """``span`` が 0 になる＝分母が 0 になるケースで例外にしないこと。"""
    frame = pd.DataFrame({"A": [1, 1, 1]})
    assert components.heat_styler(frame) is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (CellState.WORK.value, "COLOR_WORK"),
        (CellState.BREAK.value, "COLOR_BREAK"),
        (CellState.OFF.value, "COLOR_OFF"),
        (f"{components.LOCK} {CellState.OFF.value}", "inset"),
    ],
)
def test_cell_styleは状態ごとに違うCSSを返す(text, expected):
    """勤務/休憩/オフ/確定済みで見た目が変わること。"""
    from shiftai.config import COLOR_BREAK, COLOR_OFF, COLOR_WORK

    table = {
        "COLOR_WORK": COLOR_WORK,
        "COLOR_BREAK": COLOR_BREAK,
        "COLOR_OFF": COLOR_OFF,
    }
    style = components._cell_style(text)
    assert expected in table or expected in style
    assert table.get(expected, expected) in style


def test_style_shift_gridは空DataFrameをそのまま返す():
    """空グリッドに ``Styler`` を作らないこと。"""
    empty = pd.DataFrame()
    assert components.style_shift_grid(empty) is empty


def test_style_shift_gridは全セルを色付けする(day_shift, slots_list, small_staff):
    """実際のグリッドに ``background-color`` が並ぶこと。"""
    frame = components.shift_grid_frame(day_shift, slots_list, small_staff)
    assert "background-color" in components.style_shift_grid(frame).to_html()


# --------------------------------------------------------------------------
# サマリ表
# --------------------------------------------------------------------------


def test_staff_day_summary_frameは勤務の開始終了を計算する(
    day_shift, slots_list, small_staff
):
    """勤務の最初/最後の時間帯が HH:MM で、実働時間が時間単位になること。"""
    frame = components.staff_day_summary_frame(day_shift, slots_list, small_staff)
    assert list(frame.columns) == [
        "職員ID",
        "氏名",
        "資格",
        "勤務開始",
        "勤務終了",
        "勤務分数",
        "休憩分数",
        "実働時間",
    ]
    assert len(frame) == len(small_staff)
    worked = frame[frame["勤務分数"] > 0]
    assert len(worked) >= 1
    assert (worked["勤務開始"] != "—").all()
    assert (worked["勤務終了"] != "—").all()
    assert (frame["実働時間"] == (frame["勤務分数"] / 60.0).round(2)).all()


def test_staff_day_summary_frameは未勤務をダッシュで埋める(
    slots_list, small_staff
):
    """1 勤務もない職員は ``—`` のまま例外にならないこと。"""
    frame = components.staff_day_summary_frame(None, slots_list, small_staff)
    assert (frame["勤務開始"] == "—").all()
    assert (frame["勤務分数"] == 0).all()
    assert len(frame) == len(small_staff)


def test_staff_day_summary_frameは職員0名でも空表を返す(slots_list):
    """0 名でも列定義が壊れないこと。"""
    assert components.staff_day_summary_frame(None, slots_list, []).empty


def test_weekday_pivot_frameは月火水木金土日順で合計を持つ(solved_day, slots_list):
    """曜日の並びが崩れていないこと（回帰防止）。"""
    frame = components.weekday_pivot_frame(solved_day, slots_list)
    if frame.empty:
        pytest.skip("今回の解に割当がない")
    order = [c for c in ("月(h)", "火(h)", "水(h)", "木(h)", "金(h)", "土(h)", "日(h)") if c in frame.columns]
    assert list(frame.columns) == [*order, "合計(h)"]
    assert frame["合計(h)"].equals(frame[order].sum(axis=1).round(2))


def test_weekday_pivot_frameは解が無いとき空を返す(slots_list):
    """``None`` と「割当ゼロの解」の両方で空表を返すこと。"""
    assert components.weekday_pivot_frame(None, slots_list).empty
    empty = _make_result("OPTIMAL")
    assert components.weekday_pivot_frame(empty, slots_list).empty


def test_staff_hours_frameは職員別の勤務時間を返す(solved_day, small_staff):
    """bar_chart 用の 4 列が，并能で 0 埋めされていること。"""
    frame = components.staff_hours_frame(solved_day, small_staff)
    assert list(frame.columns) == ["職員ID", "氏名", "勤務時間", "出勤日数"]
    assert len(frame) == len(small_staff)
    assert (frame["勤務時間"] >= 0).all()


def test_staff_hours_frameは引数が足りないとき空を返す(solved_day):
    """``None`` / 職員 0 人のどちらでも空表を返すこと。"""
    assert components.staff_hours_frame(None, []).empty
    assert components.staff_hours_frame(solved_day, []).empty


def test_staffing_curve_frameは必要人員と配置人員を並べる(
    small_requirements, solved_day, small_staff, one_day
):
    """人員配置曲線の系列名が揃い、過不足が定義どおりになること。"""
    day = one_day[0]
    frame = components.staffing_curve_frame(small_requirements, solved_day, day, small_staff)
    assert list(frame.columns) == [
        "時間帯",
        "必要人員",
        "必要保育士数",
        "配置人員",
        "過不足",
    ]
    assert len(frame) == len(small_requirements.slots)
    assert (frame["過不足"] == frame["必要人員"] - frame["配置人員"]).all()


def test_staffing_curve_frameは未計算なら空を返す(small_requirements, one_day):
    """必要人員も解も無いときは空表（UI は info を出す）にすること。"""
    assert components.staffing_curve_frame(None, None, one_day[0], []).empty
    assert components.staffing_curve_frame(small_requirements, None, one_day[0], []).empty


def test_requirement_heatmapは時間帯と年齢クラスの表を作る(
    small_requirements, one_day
):
    """行が年齢クラス順、列が時間帯順で「合計」行/列があること。"""
    frame = components.requirement_heatmap(small_requirements, one_day[0])
    assert not frame.empty
    slot_labels = [s.label for s in small_requirements.slots if s.label in frame.columns]
    assert list(frame.columns) == [*slot_labels, "合計"]
    assert "合計" in frame.index
    assert frame.loc["合計", "合計"] == int(frame.loc["合計", slot_labels].sum())
    for age_class in frame.index.drop("合計"):
        assert frame.loc[age_class, "合計"] == int(frame.loc[age_class, slot_labels].sum())


def test_requirement_heatmapはNoneで空を返す():
    """必要人員が未計算なら空表を返すこと。"""
    assert components.requirement_heatmap(None).empty


def test_requirement_heatmapは開園日がないとき空を返す():
    """在園児 0 人の ``RequirementTable``（休日設定で全滅）でも空表になること。"""
    from shiftai.domain import RequirementTable

    empty_table = RequirementTable(
        day_open=time(9, 0), day_close=time(11, 0), granularity_min=30, slots=(), rows={}
    )
    assert components.requirement_heatmap(empty_table).empty


def test_requirement_heatmapは休園日で空を返す(small_requirements, small_children):
    """在園児が 0 人の日は「表示できない」空表になること。"""
    holiday = date(2026, 12, 31)
    assert components.requirement_heatmap(small_requirements, holiday).empty


# --------------------------------------------------------------------------
# 違反・ギャップ
# --------------------------------------------------------------------------


def test_gap_table_frameはレポートをDataFrameにする(day_report):
    """過不足レポートの表が「不足」「過剰」列を持つこと。"""
    frame = components.gap_table_frame(day_report)
    assert not frame.empty
    assert "不足" in frame.columns


def test_gap_table_frameはNoneなら空():
    """レポート未生成なら空表を返すこと。"""
    assert components.gap_table_frame(None).empty


def test_style_gap_tableは空DataFrameをそのまま返す():
    """空表に ``Styler`` を作らないこと。"""
    empty = pd.DataFrame()
    assert components.style_gap_table(empty) is empty


def test_style_gap_tableは不足と過剰で色分けする():
    """不足（正の不足量）は赤、過剰（正の過剰量）は青で塗られること。

    最適解では過不足が 0 になりうるため、配色は決定論的な小さい表で固定する。
    """
    frame = pd.DataFrame(
        {
            "時間帯": ["09:00-09:30", "10:00-10:30"],
            "不足": [2, -1],
            "過剰": [0, 3],
        }
    )
    html = components.style_gap_table(frame).to_html()
    assert html.count("background-color") >= 2
    assert "#8e0000" in html
    assert "#0d47a1" in html


def test_style_gap_tableは過剰列が無ても描画できる():
    """``過剰`` 列を持たないレポートでも落ちないこと。"""
    frame = pd.DataFrame({"時間帯": ["09:00-09:30"], "不足": [1]})
    assert "background-color" in components.style_gap_table(frame).to_html()


def test_style_gap_tableは0だけだと色が付かない():
    """過不足 0 の行には色を付けない（不要な背景色を避ける）こと。"""
    frame = pd.DataFrame({"時間帯": ["09:00-09:30"], "不足": [0], "過剰": [0]})
    html = components.style_gap_table(frame).to_html()
    assert "#8e0000" not in html


def test_style_gap_tableは数値でないセルを無視する():
    """``—`` や空文字が混ざっていても例外にせず色なしとして扱うこと。"""
    frame = pd.DataFrame({"時間帯": ["09:00-09:30"], "不足": ["—"], "過剰": [None]})
    html = components.style_gap_table(frame).to_html()
    assert "#8e0000" not in html


def test_violation_dataframeは空なら列定義だけ返す():
    """違反 0 件でも列 exists っていること（UI が壊れないため）。"""
    frame = components.violation_dataframe([])
    assert list(frame.columns) == ["深刻度", "区分", "内容", "日付", "時間帯", "職員ID"]


def test_violation_dataframeは全項目を日本語列で作る(violations, small_staff, slots_list):
    """深刻度・区分・コード・曜日などの列が揃い、日本語が化けていないこと。"""
    if not violations:
        pytest.skip("この解には違反がない")
    frame = components.violation_dataframe(violations)
    for column in ("深刻度", "区分", "コード", "内容", "日付", "曜日", "時間帯", "職員ID"):
        assert column in frame.columns
    assert len(frame) == len(violations)
    assert frame["深刻度"].map(lambda v: isinstance(v, str)).all()
    assert set(frame["曜日"]) <= {"月", "火", "水", "木", "金", "土", "日", ""}
    assert set(frame["深刻度"]) <= {s.value for s in ViolationSeverity}


def test_violation_dataframeはdayとslotが無い違反も扱う():
    """日付・時間帯が ``None`` の違反が ``—`` / 空文字 になること。"""
    violation = Violation(
        severity=ViolationSeverity.INFO,
        code="UNKNOWN_CODE",
        message="情報のみ",
        day=None,
        slot=None,
        staff_id=None,
    )
    frame = components.violation_dataframe([violation])
    assert frame.loc[0, "日付"] == "—"
    assert frame.loc[0, "時間帯"] == "—"
    assert frame.loc[0, "職員ID"] == "—"
    assert frame.loc[0, "曜日"] == ""
    assert frame.loc[0, "区分"] == "UNKNOWN_CODE"


def test_load_issue_dataframeは空でも列定義を返す():
    """エラー 0 件でも表の列が壊れないこと。"""
    frame = components.load_issue_dataframe([])
    assert list(frame.columns) == ["深刻度", "行", "列", "内容"]


def test_load_issue_dataframeは行番号を1始まりにする(load_issues):
    """Excel に合わせて ``row + 1`` を表示すること。"""
    frame = components.load_issue_dataframe(load_issues)
    assert list(frame["行"]) == [1, 4]
    assert list(frame["深刻度"]) == ["error", "warning"]
    assert frame.loc[0, "内容"] == "職員IDが空です"


def test_load_issue_dataframeは属性のないオブジェクトでも落ちない():
    """``LoadIssue`` でなくても既定値で埋められること。"""
    frame = components.load_issue_dataframe([object()])
    assert frame.loc[0, "深刻度"] == ""
    assert frame.loc[0, "行"] == 0
    assert frame.loc[0, "列"] == "—"


# --------------------------------------------------------------------------
# 根拠テキスト・日付ヘルパ
# --------------------------------------------------------------------------


def test_requirement_basis_rowsは時間帯ごとに説明を作る(
    small_requirements, one_day
):
    """「時間帯／年齢クラス」見出しと基準からの導出説明が揃うこと。"""
    st.session_state.clear()
    state.init_state()
    rows = components.requirement_basis_rows(small_requirements, one_day[0])
    st.session_state.clear()
    assert rows
    for heading, explanation in rows:
        assert "／" in heading
        assert isinstance(explanation, str) and explanation


def test_requirement_basis_rowsは未計算なら空():
    """必要人員か日付のどちらかが無ければ空リストになること。"""
    st.session_state.clear()
    state.init_state()
    assert components.requirement_basis_rows(None, date(2026, 9, 28)) == []
    assert components.requirement_basis_rows(object(), None) == []
    st.session_state.clear()


def test_days_ofとday_optionsは降順の日付を返す(solved_day, one_day):
    """selectbox は最新日を先頭にすること。"""
    days = components.days_of(solved_day)
    assert days
    options = components.day_options(solved_day)
    assert options == sorted(days, reverse=True)


def test_days_ofはNoneなら空():
    """未実行なら空リストを返すこと。"""
    assert components.days_of(None) == []
    assert components.day_options(None) == []


# --------------------------------------------------------------------------
# ウィジェット描画（AppTest 経由）
# --------------------------------------------------------------------------


def test_metric_rowは空でも描画できる(tmp_path):
    """項目 0 個でも「何も描かない」で終わること。"""
    at = _run_widget_script(tmp_path, "components.metric_row([])")
    assert at.markdown == []


def test_metric_rowは警告色と情報色を出し分ける(tmp_path):
    """先頭 ``!`` は警告、``~`` は情報になること。"""
    at = _run_widget_script(
        tmp_path,
        "components.metric_row([('不足', '!3 件', 'メモ'), ('充足', '~92%', None), ('普通', '5', None)])",
    )
    body = "\n".join(m.value for m in at.markdown)
    assert "shiftai-metric warn" in body
    assert "shiftai-metric info" in body
    assert "!3" not in body
    assert "メモ" in body


def test_metric_rowは1行のカード数で折り返す(tmp_path):
    """``per_row`` を超える項目は複数の行に分けて描画されること。"""
    at = _run_widget_script(
        tmp_path,
        "components.metric_row([(str(i), str(i), None) for i in range(7)], per_row=3)",
    )
    assert len(at.markdown) == 7
    assert len(at.columns) == 9  # 3 行 × 3 列（末尾の空欄も列として数えられる）


def test_metric_rowはper_rowが0以下でも1列に整える(tmp_path):
    """``per_row=0`` でも無限ループせず描画できること。"""
    at = _run_widget_script(
        tmp_path, "components.metric_row([('a', '1', None)], per_row=0)"
    )
    assert len(at.markdown) == 1


def test_cell_legendは3状態と確定の説明を出す(tmp_path):
    """凡例に勤務・休憩・オフ・確定済みが出ること。"""
    at = _run_widget_script(tmp_path, "components.cell_legend()")
    body = at.markdown[0].value
    for label in (CellState.WORK.value, CellState.BREAK.value, CellState.OFF.value):
        assert label in body
    assert components.LOCK in body


def test_status_bannerはステータスごとに色分けする(tmp_path):
    """``SolveStatus`` ごとに success/warning/error へ振り分けられること。

    5 つのステータスを 1 つのスクリプトにまとめて描画し（AppTest の起動コスト対策）、
    成功/警告/エラーの並び順と中身のラベルを突き合わせる。
    """
    names = ["OPTIMAL", "FEASIBLE", "PARTIAL", "INFEASIBLE", "ERROR"]
    body = "\n".join(f"components.status_banner(PAYLOAD['r{i}'])" for i in range(len(names)))
    at = _run_widget_script(
        tmp_path, body, payload={f"r{i}": _make_result(n) for i, n in enumerate(names)}
    )
    rendered = [e.value for e in (*at.success, *at.warning, *at.error)]
    assert len(rendered) == len(names)
    for name, text in zip(names, rendered, strict=True):
        assert _make_result(name).status.value in text
    assert [len(at.success), len(at.warning), len(at.error)] == [2, 1, 2]
    assert all("—" in text for text in rendered), "説明文が空だと利用者が迷う"


def test_status_bannerは所要時間を注記する(tmp_path):
    """``stats['elapsed_sec']`` があるとき所要時間・変数数が出ること。"""
    result = _make_result("OPTIMAL")
    result.stats.update(
        {
            "elapsed_sec": 12.34,
            "solver": "CBC",
            "num_variables": 700,
            "num_constraints": 900,
        }
    )
    at = _run_widget_script(
        tmp_path, "components.status_banner(PAYLOAD['result'])", payload={"result": result}
    )
    body = at.success[0].value
    assert "12.3" in body
    assert "CBC" in body
    assert "700" in body
    assert "900" in body


def test_status_bannerはNoneなら何も出さない(tmp_path):
    """未実行ならバナーを出さずそのまま返すこと。"""
    at = _run_widget_script(tmp_path, "components.status_banner(None)")
    assert at.success == [] and at.warning == [] and at.error == []


def test_render_violationsは0件なら成功メッセージを出す(tmp_path):
    """違反ゼロは「検出されていません」と伝えること。"""
    at = _run_widget_script(tmp_path, "components.render_violations([])")
    assert at.success
    assert at.expander == []


def test_render_violationsは深刻度ごとにまとめる(tmp_path, slots_list, one_day):
    """BLOCKER/WARNING/INFO が別々の ``expander`` になり、コード別に内訳が出ること。"""
    day = one_day[0]
    slot = slots_list[0]
    sample = [
        _make_violation(ViolationSeverity.BLOCKER, "COVERAGE_SHORTFALL", "不足", day, slot, "S001"),
        _make_violation(ViolationSeverity.BLOCKER, "COVERAGE_SHORTFALL", "不足2", day, slot, "S002"),
        _make_violation(
            ViolationSeverity.WARNING, "PREFERENCE_MISSED", "希望未達", day, slot, "S001"
        ),
    ]
    at = _run_widget_script(
        tmp_path,
        "components.render_violations(PAYLOAD['violations'])",
        payload={"violations": sample},
    )
    assert len(at.expander) == 2
    labels = [e.label for e in at.expander]
    blocker = ViolationSeverity.BLOCKER.value
    warning = ViolationSeverity.WARNING.value
    assert any(blocker in label and "2 件" in label for label in labels)
    assert any(warning in label and "1 件" in label for label in labels)
    bodies = "\n".join(m.value for m in at.markdown)
    assert "COVERAGE_SHORTFALL" in bodies
    assert "希望を叶えられていません" in bodies


def test_render_violationsは実データでも描画できる(tmp_path, violations):
    """実際の最適化結果の違反一覧でクラッシュしないこと（表のキーが重複しないこと）。"""
    at = _run_widget_script(
        tmp_path,
        "components.render_violations(PAYLOAD['violations'])",
        payload={"violations": violations},
    )
    assert not at.exception
    if violations:
        assert at.expander
