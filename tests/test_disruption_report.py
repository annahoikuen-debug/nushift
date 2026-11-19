"""Timefold 思想に基づくディスラプション（変更破壊量）レポートの単体テスト。"""

from __future__ import annotations

from datetime import date, time

from shiftai.domain import (
    CellState,
    DisruptionReport,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
)
from shiftai.solver import _compute_disruption_report


def test_compute_disruption_report():
    """リペア前後の差分から、公休出勤・時間延長・スライドが正しく判定されるか検証。"""
    d = date(2026, 4, 1)
    slots = [
        Slot(start=time(7, 0), end=time(8, 0)),
        Slot(start=time(8, 0), end=time(9, 0)),
        Slot(start=time(9, 0), end=time(10, 0)),
        Slot(start=time(10, 0), end=time(11, 0)),
    ]
    s0, s1, s2, s3 = [s.label for s in slots]

    # リペア前:
    # absent: s1-s2 (勤務)
    # staff_A: 全OFF (公休)
    # staff_B: s1 (勤務)
    # staff_C: s1 (勤務)
    before_sd = ShiftDay(
        day=d,
        assignments={
            "absent": {
                s0: CellState.OFF,
                s1: CellState.WORK,
                s2: CellState.WORK,
                s3: CellState.OFF,
            },
            "staff_A": {s0: CellState.OFF, s1: CellState.OFF, s2: CellState.OFF, s3: CellState.OFF},
            "staff_B": {
                s0: CellState.OFF,
                s1: CellState.WORK,
                s2: CellState.OFF,
                s3: CellState.OFF,
            },
            "staff_C": {
                s0: CellState.OFF,
                s1: CellState.WORK,
                s2: CellState.OFF,
                s3: CellState.OFF,
            },
        },
    )
    before_res = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[before_sd])

    # リペア後:
    # absent: 全OFF (欠勤)
    # staff_A: s1 (公休から出勤!)
    # staff_B: s1-s2 (時間延長!)
    # staff_C: s2 (時間帯スライド変更!)
    after_sd = ShiftDay(
        day=d,
        assignments={
            "absent": {s0: CellState.OFF, s1: CellState.OFF, s2: CellState.OFF, s3: CellState.OFF},
            "staff_A": {
                s0: CellState.OFF,
                s1: CellState.WORK,
                s2: CellState.OFF,
                s3: CellState.OFF,
            },
            "staff_B": {
                s0: CellState.OFF,
                s1: CellState.WORK,
                s2: CellState.WORK,
                s3: CellState.OFF,
            },
            "staff_C": {
                s0: CellState.OFF,
                s1: CellState.OFF,
                s2: CellState.WORK,
                s3: CellState.OFF,
            },
        },
    )
    after_res = SolveResult(status=SolveStatus.FEASIBLE, shift_days=[after_sd])

    rep = _compute_disruption_report(before_res, after_res, "absent", d, slots)

    assert isinstance(rep, DisruptionReport)
    assert rep.total_disrupted_staff == 3
    assert rep.called_in_from_off == ["staff_A"]
    assert rep.hours_extended_staff == ["staff_B"]
    assert rep.pattern_changed_staff == ["staff_C"]
    assert rep.disruption_score == 100.0 + 10.0 + 30.0

    summary_text = rep.summary()
    assert "公休出勤: 1名 (staff_A)" in summary_text
    assert "時間延長: 1名 (staff_B)" in summary_text
    assert "時間帯変更: 1名 (staff_C)" in summary_text
