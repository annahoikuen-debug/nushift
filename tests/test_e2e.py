"""ヘッドレス E2E（``slow``）。

サンプル生成 → 必要人員計算 → シフト最適化 → 過不足分析 → 出力、
という一連の業務フローが例外なく最後まで通ることを保証する。
**1 週間分の最適化が 1 回だけ**走るよう ``solved_week`` フィクスチャを共有する。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from openpyxl import load_workbook

from shiftai import local_rules, sample_data
from shiftai.data_loader import load_bundle
from shiftai.domain import CellState, FacilitySettings
from shiftai.exporter import (
    export_bundle_zip,
    payroll_dataframe,
    requirements_dataframe,
    shift_matrices,
    shift_to_dataframe,
    summary_markdown,
    to_ics,
)
from shiftai.gap_analysis import analyze_gap, check_violations, compute_cost, summarize
from shiftai.solver import staff_shift_count, staff_work_hours
from shiftai.standards import peak_requirement, total_required_hours

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

FQ = "福岡市"


def test_サンプルから必要人員まで(small_requirements):
    """サンプルデータから必要人員表ができること。"""
    assert small_requirements.all_days()
    assert small_requirements.total_needed_hours() > 0
    assert peak_requirement(small_requirements) >= 1


def test_一連の業務フロー(solved_week, week_inputs):
    """サンプル → 必要人員 → 最適化 → 過不足 → 出力まで例外なく通ること。"""
    children, staff, prefs, table = week_inputs

    assert len(children) > 0
    assert len(staff) == 28
    assert total_required_hours(table) == pytest.approx(521.0)

    result = solved_week
    assert result.ok is True
    assert result.stats["num_slots"] == 25
    assert result.stats["elapsed_sec"] >= 0.0

    report = analyze_gap(table, result, staff, standard=local_rules.get_standard(FQ))
    assert report.total_shortfall_slots == 0
    assert report.coverage_ratio == pytest.approx(1.0, abs=0.01)

    violations = check_violations(
        table, result, staff, prefs, standard=local_rules.get_standard(FQ)
    )
    assert all(v.code and v.message for v in violations)
    assert len(result.blockers()) == 0

    summary = summarize(result, report, staff)
    assert summary["職員数"] == 28.0
    assert summary["法令違反件数"] == 0.0
    assert compute_cost(result, staff) > 0

    slots = tuple(table.slots)
    assert not shift_to_dataframe(result, slots, staff).empty
    assert not shift_matrices(result, slots, staff).empty
    assert len(payroll_dataframe(result, slots, staff)) == 28
    assert not requirements_dataframe(table).empty
    assert to_ics(result, slots, staff).startswith("BEGIN:VCALENDAR")
    assert "# シフトサマリー" in summary_markdown(
        result, slots, staff, facility_name="あさひ保育園"
    )


def test_勤務表が全日全職員を埋める(solved_week, week_inputs):
    """1 週間分の全日・全職員・全時間帯のセルが定義されていること。"""
    children, staff, prefs, table = week_inputs
    assert len(solved_week.shift_days) == 7
    for shift_day in solved_week.shift_days:
        assert len(shift_day.assignments) == 28
        for member in staff:
            for slot in table.slots:
                assert shift_day.get(member.staff_id, slot) in set(CellState)
    hours = staff_work_hours(solved_week, staff)
    counts = staff_shift_count(solved_week)
    assert set(hours) == {m.staff_id for m in staff}
    assert set(counts) == {m.staff_id for m in staff}
    assert sum(hours.values()) > 0


def test_成果物ZIPが開ける(solved_week, week_inputs):
    """``export_bundle_zip`` の成果物が実際に開けること。"""
    children, staff, prefs, table = week_inputs
    settings = FacilitySettings(facility_name="あさひ保育園")
    data = export_bundle_zip(solved_week, table, tuple(table.slots), staff, settings)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "shift.csv",
            "payroll.csv",
            "shift_matrix.csv",
            "requirements.csv",
            "shift.ics",
            "summary.md",
            "shift.xlsx",
        }
        workbook = load_workbook(io.BytesIO(archive.read("shift.xlsx")))
        assert {"シフト", "職員別勤務", "給与計算", "配置基準"} <= set(workbook.sheetnames)
        assert archive.read("summary.md").decode("utf-8").startswith("# シフトサマリー")
        assert archive.read("shift.ics").decode("utf-8").rstrip().endswith("END:VCALENDAR")


def test_データ入出力のラウンドトリップ(solved_week, week_inputs):
    """サンプルを CSV 経由で出し入れしても同じ結果が得られること。"""
    children, staff, prefs, table = week_inputs
    weeks = sorted({c.day for c in children})
    frames = sample_data.sample_dataframes(days=weeks, seed=42)
    result = load_bundle(frames["children"], frames["staff"], frames["preferences"])
    assert result.ok is True
    assert len(result.children) == len(children)
    assert len(result.staff) == len(staff)


def test_一週間の必要人時が市政基準の想定内(week_inputs):
    """1 週間分の必要人時とピーク配置が市政基準の想定内であること。"""
    children, staff, prefs, table = week_inputs
    assert total_required_hours(table) == pytest.approx(521.0)
    assert peak_requirement(table) == 9
    assert len(table.all_days()) == 7
    assert len(table.day_slots()) == 25
