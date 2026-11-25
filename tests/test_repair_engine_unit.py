"""Unit tests for NuShift Tier 0 Rule-based Absence Repair Engine."""

from __future__ import annotations

import time
from datetime import date
from datetime import time as dt_time

from shiftai import local_rules, sample_data
from shiftai.domain import (
    AgeClass,
    CellState,
    Contract,
    EmploymentType,
    FacilitySettings,
    PairingRule,
    Requirement,
    RequirementTable,
    Role,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Unavailability,
)
from shiftai.repair_engine import repair_shift_absence_fast
from shiftai.repair_engine.candidates import ActionType, CandidateEvaluator
from shiftai.repair_engine.gap_scanner import SlotGapScanner
from shiftai.repair_engine.greedy import GreedyAssigner
from shiftai.repair_engine.snapshot import SnapshotIndex
from shiftai.solver import solve_shift
from shiftai.standards import build_requirements


def test_repair_engine_microsecond_speed_and_determinism():
    """ルールベースリペアがミリ秒級で完了し、決定論的に同一結果を返すこと"""
    start_d = date(2026, 11, 2)
    days = [start_d]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard("全国基準（厚労省）")
    table = build_requirements(
        children,
        days,
        std,
        day_open=dt_time(7, 0),
        day_close=dt_time(19, 0),
        granularity_min=30,
    )

    # 1日分の初期解を作成
    res = solve_shift(
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )
    assert res.ok

    # 欠勤者を指定（staff[1]）
    absent_id = staff[1].staff_id

    # 1. 速度計測
    start_t = time.perf_counter()
    repaired_1 = repair_shift_absence_fast(
        existing_result=res,
        absent_staff_id=absent_id,
        target_date=start_d,
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
    )
    elapsed_ms = (time.perf_counter() - start_t) * 1000

    # 50ミリ秒未満で完了することを検証
    assert elapsed_ms < 50.0, f"Repair took too long: {elapsed_ms:.2f}ms"
    assert repaired_1.ok
    assert repaired_1.stats.get("repair_tier") == 0

    # 2. 決定論性テスト（10回実行して assignments の全キー・値が完全一致）
    for _ in range(10):
        repaired_n = repair_shift_absence_fast(
            existing_result=res,
            absent_staff_id=absent_id,
            target_date=start_d,
            children=children,
            staff=staff,
            requirements=table,
            preferences=prefs,
            standard=std,
        )
        assert repaired_n.assignments == repaired_1.assignments


def test_gap_scanner_reads_real_requirement_fields():
    """SlotGapScanner が Requirement の needed_staff / needed_qualified を正確に参照すること"""
    d = date(2026, 11, 2)
    slot = Slot(dt_time(9, 0), dt_time(9, 30))
    req_table = RequirementTable(
        day_open=dt_time(9, 0),
        day_close=dt_time(9, 30),
        granularity_min=30,
        slots=(slot,),
        rows={
            d: [
                Requirement(
                    day=d,
                    slot=slot,
                    age_class=AgeClass.INFANT,
                    child_count=6,
                    needed_staff=2,
                    needed_qualified=2,
                ),
                Requirement(
                    day=d,
                    slot=slot,
                    age_class=AgeClass.AGE_1,
                    child_count=12,
                    needed_staff=2,
                    needed_qualified=1,
                ),
            ]
        },
    )
    c = Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI)
    s1 = StaffMember("S1", "A", (Role.HOIKUSHI,), c)
    s2 = StaffMember("S2", "B", (Role.SHIENSHIIN,), c)
    snapshot = SnapshotIndex.build(
        existing_assignments={("S1", d, slot.label): CellState.WORK},
        target_date=d,
        staff=[s1, s2],
        absent_staff_ids=[],
        contracts={"S1": c, "S2": c},
    )
    gaps = SlotGapScanner.scan(snapshot, req_table)
    assert len(gaps) == 1
    assert gaps[0].need_total == 4
    assert gaps[0].need_qualified == 3
    assert gaps[0].gap_total == 3
    assert gaps[0].gap_qualified == 2


def test_greedy_assigner_handles_multi_shortfall_hours_cap_unavail_and_pairing():
    """複数不足・累積1日上限・希望休・禁止ペアを貪欲リペアが正確に守ること"""
    d = date(2026, 11, 2)
    s1 = Slot(dt_time(9, 0), dt_time(10, 0))
    s2 = Slot(dt_time(10, 0), dt_time(11, 0))
    req_table = RequirementTable(
        day_open=dt_time(9, 0),
        day_close=dt_time(11, 0),
        granularity_min=60,
        slots=(s1, s2),
        rows={
            d: [
                Requirement(d, s1, AgeClass.AGE_1, 10, needed_staff=2, needed_qualified=1),
                Requirement(d, s2, AgeClass.AGE_1, 10, needed_staff=1, needed_qualified=1),
            ]
        },
    )
    c_tight = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        max_daily_hours=2.0,
        employment_type=EmploymentType.SEI,
        earliest_start=dt_time(8, 0),
        latest_end=dt_time(18, 0),
    )
    c_off = Contract(
        weekly_hours=40.0,
        daily_hours=8.0,
        employment_type=EmploymentType.SEI,
        earliest_start=dt_time(8, 0),
        latest_end=dt_time(18, 0),
    )
    # Aはs1既に1h勤務済(上限2hなのでs2のみ追加可)、Bはs2が希望休、Cは公休だがAとavoidペア、DがC抜きまたは両方充足可能か
    staff = [
        StaffMember("A", "保育士A", (Role.HOIKUSHI,), c_tight),
        StaffMember("B", "保育士B", (Role.HOIKUSHI,), c_off),
        StaffMember("C", "支援員C", (Role.SHIENSHIIN,), c_off),
        StaffMember("D", "保育士D", (Role.HOIKUSHI,), c_off),
    ]
    prefs = {
        "B": StaffPreferences(
            unavailable=[Unavailability(day=d, start=dt_time(10, 0), end=dt_time(11, 0))]
        ),
    }
    rules = [PairingRule(staff_a_id="A", staff_b_id="C", kind="avoid_together")]
    initial = SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[
            ShiftDay(
                day=d,
                assignments={
                    "A": {s1.label: CellState.WORK, s2.label: CellState.OFF},
                    "B": {s1.label: CellState.OFF, s2.label: CellState.OFF},
                    "C": {s1.label: CellState.OFF, s2.label: CellState.OFF},
                    "D": {s1.label: CellState.OFF, s2.label: CellState.OFF},
                },
            )
        ],
        assignments={
            ("A", d, s1.label): CellState.WORK,
            ("A", d, s2.label): CellState.WORK,
        },
    )
    # snapshot 上は A がすでに s1, s2 の両方に1hずつ = 2.0h (max_daily_hours=2.0) に到達するケース
    snap = SnapshotIndex.build(
        existing_assignments={("A", d, s1.label): CellState.WORK},
        target_date=d,
        staff=staff,
        absent_staff_ids=[],
        preferences=prefs,
        granularity_min=60,
    )
    # 1h ずつスロットを足す
    gaps = SlotGapScanner.scan(snap, req_table)
    assigned, unfilled, _logs = GreedyAssigner.assign(
        snapshot=snap,
        gaps=gaps,
        pairing_rules=rules,
        prefer_on_duty_extension=True,
    )
    assert not unfilled
    # C は s1 で A と同時勤務になるため割り当てられてはならない
    assert s1.label not in assigned.get("C", [])
    # B は希望休の s2 に割り当てられてはならない
    assert s2.label not in assigned.get("B", [])
    # CandidateEvaluator 単体でも B の希望休と D/A の時間超過が弾かれること
    assert (
        CandidateEvaluator.evaluate(snap, gaps[0], "B", [s2.label], ActionType.HELP_OFF_DUTY)
        is None
    )
    # clean execution through repair_shift_absence_fast
    res2 = repair_shift_absence_fast(
        existing_result=initial,
        absent_staff_id="B",
        target_date=d,
        children=[],
        staff=staff,
        requirements=req_table,
        preferences=prefs,
        settings=FacilitySettings(granularity_min=60),
        pairing_rules=rules,
    )
    assert res2.ok
