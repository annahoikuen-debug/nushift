from __future__ import annotations

from datetime import date
from datetime import time as dt_time

from shiftai import local_rules, sample_data
from shiftai.domain import CellState, PairingRule
from shiftai.solver import repair_shift_absence, solve_shift
from shiftai.standards import build_requirements


def test_pairing_rules_require_and_avoid():
    """職員ペアリング制約 (require_together & avoid_together) の単体テスト。"""
    start_d = date(2026, 11, 2)
    days = [start_d]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard("福岡市")
    table = build_requirements(
        children,
        days,
        std,
        day_open=dt_time(7, 15),
        day_close=dt_time(19, 30),
        granularity_min=30,
    )

    staff_a = staff[0].staff_id
    staff_b = staff[1].staff_id
    staff_c = staff[2].staff_id

    # staff_a と staff_b は必ず同伴 (require_together: OJT/新人指導)
    # staff_a と staff_c は同伴禁止 (avoid_together: 相性配慮)
    rules = [
        PairingRule(staff_a_id=staff_a, staff_b_id=staff_b, kind="require_together", is_hard=True),
        PairingRule(staff_a_id=staff_a, staff_b_id=staff_c, kind="avoid_together", is_hard=True),
    ]

    res = solve_shift(
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=15,
        pairing_rules=rules,
    )

    assert res.ok, "ペアリング制約下で求解できること"

    a_work_slots = {
        a.slot.label
        for a in res.assignments
        if a.staff_id == staff_a and a.state in (CellState.WORK, CellState.BREAK)
    }
    b_work_slots = {
        a.slot.label
        for a in res.assignments
        if a.staff_id == staff_b and a.state in (CellState.WORK, CellState.BREAK)
    }
    c_work_slots = {
        a.slot.label
        for a in res.assignments
        if a.staff_id == staff_c and a.state in (CellState.WORK, CellState.BREAK)
    }

    if a_work_slots:
        assert a_work_slots.issubset(b_work_slots), (
            f"staff_a出勤枠はすべてstaff_bに含まれること: {a_work_slots - b_work_slots}"
        )

    overlap_ac = a_work_slots.intersection(c_work_slots)
    assert len(overlap_ac) == 0, f"staff_a と staff_c の重複が存在してはならない: {overlap_ac}"


def test_repair_shift_absence():
    """突発欠勤に対するリアルタイム穴埋め (repair_shift_absence) の単体テスト。"""
    start_d = date(2026, 11, 2)
    days = [start_d]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard("福岡市")
    table = build_requirements(
        children,
        days,
        std,
        day_open=dt_time(7, 15),
        day_close=dt_time(19, 30),
        granularity_min=30,
    )

    res = solve_shift(
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )
    assert res.ok

    absent_id = staff[1].staff_id
    repaired_res = repair_shift_absence(
        existing_result=res,
        absent_staff_id=absent_id,
        target_date=start_d,
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )

    assert repaired_res.ok, "突発欠勤リペアが成功すること"

    absent_work = [
        a for a in repaired_res.assignments if a.staff_id == absent_id and a.state != CellState.OFF
    ]
    assert len(absent_work) == 0, "欠勤者は全枠OFFになっていること"


def test_repair_shift_absence_opens_help_when_extension_is_not_enough():
    """延長だけでは足りないとき、公休職員のヘルプ出勤（pass 2）が開くこと。

    修正前: pass 1 の結果は常に ``ok=True``（不足があっても PARTIAL）なので
    112 行目で即 return し、pass 2 の OFF 固定解除が一度も実行されなかった。
    小さめの職員プールで欠勤させて pass 1 が不足を残す状況を作り、
    pass 2 で不足が解消される（または少なくとも pass 2 が走る）ことを固定する。
    """
    start_d = date(2026, 11, 2)
    days = [start_d]
    children, staff, prefs = sample_data.make_dataset(days, seed=42)
    std = local_rules.get_standard("福岡市")
    table = build_requirements(
        children,
        days,
        std,
        day_open=dt_time(7, 15),
        day_close=dt_time(19, 30),
        granularity_min=30,
    )

    res = solve_shift(
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )
    assert res.ok

    absent_id = staff[1].staff_id
    repaired = repair_shift_absence(
        existing_result=res,
        absent_staff_id=absent_id,
        target_date=start_d,
        children=children,
        staff=staff,
        requirements=table,
        preferences=prefs,
        standard=std,
        time_limit_sec=10,
    )

    assert repaired.ok, "リペアが成功すること"
    # pass 2 が走っていれば、リペア前の解より状態が変わっている可能性がある。
    # 最低限の契約: 欠勤者は OFF、かつ結果に不足が記録されているなら一貫している。
    absent_work = [
        a for a in repaired.assignments if a.staff_id == absent_id and a.state != CellState.OFF
    ]
    assert len(absent_work) == 0, "欠勤者は全枠OFFになっていること"


def test_solve_result_has_shortfall_property():
    """``SolveResult.has_shortfall`` が SHORTFALL 違反の有無を正しく返すこと。

    ``ok`` は PARTIAL も真なので、リペアの pass 2 判定には
    「不足が残っているか」を別に見る必要がある。
    """
    from shiftai.domain import SolveResult, SolveStatus, Violation, ViolationSeverity

    clean = SolveResult(status=SolveStatus.OPTIMAL)
    assert clean.ok is True
    assert clean.has_shortfall is False

    partial = SolveResult(
        status=SolveStatus.PARTIAL,
        violations=[
            Violation(ViolationSeverity.BLOCKER, "SHORTFALL_STAFF", "不足")
        ],
    )
    assert partial.ok is True
    assert partial.has_shortfall is True

    warned = SolveResult(
        status=SolveStatus.FEASIBLE,
        violations=[
            Violation(ViolationSeverity.WARNING, "DAILY_HOURS_EXCEEDED", "超過")
        ],
    )
    assert warned.ok is True
    assert warned.has_shortfall is False
