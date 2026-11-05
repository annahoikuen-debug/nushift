"""Localized shift repair and absence recovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from shiftai.domain import (
    CellState,
    ChildPlan,
    ClassRoom,
    FacilitySettings,
    ObjectiveWeights,
    PairingRule,
    RequirementTable,
    SolveResult,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.shift_patterns import ShiftPattern


def repair_shift_absence(
    existing_result: SolveResult,
    absent_staff_id: str,
    target_date: date,
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    settings: FacilitySettings | None = None,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    pairing_rules: Sequence[PairingRule] | None = None,
    classrooms: Sequence[ClassRoom] | None = None,
    require_leader: bool = False,
    require_opening_closing_skill: bool = False,
    time_limit_sec: int = 10,
    prefer_on_duty_extension: bool = True,
) -> SolveResult:
    """当日の急な突発欠勤（発熱・忌引等）に対する局所安定リアルタイム穴埋め（動的リペア）。

    確定済みシフト（``existing_result``）のうち、欠勤日以外のすべて、および
    当日に出勤予定のない職員の勤務状態を手動固定（fixed_assignments）として維持し、
    「欠勤者のみ OFF」に変更した上で、
    1. 当日すでに出勤している職員の勤務時間延長（残業・枠拡大）を優先
    2. それでも不足する場合に公休職員の応援出勤（ヘルプ）を開放
    3. 別日への波及再編（カスケード変動）を100%防止（局所ピン留め）
    して配置基準の不足を最小の変更手数で埋める。
    """
    from shiftai.solver import solve_shift

    fixed: dict[tuple[str, date, str], CellState] = {}

    # 当日に出勤予定だった職員と公休だった職員を把握
    on_duty_staff_ids = set()
    off_duty_staff_ids = set()

    for a in existing_result.assignments:
        if a.day == target_date:
            if a.state in (CellState.WORK, CellState.BREAK):
                on_duty_staff_ids.add(a.staff_id)
            else:
                off_duty_staff_ids.add(a.staff_id)

    # 1. 別日のシフトは完全に維持固定（局所ピン留め: 他の日には一切波及させない）
    for a in existing_result.assignments:
        sid = a.staff_id
        d = a.day
        slot_label = a.slot.label
        if d != target_date:
            fixed[(sid, d, slot_label)] = a.state

    # 2. 欠勤者は公休(OFF)に固定
    for slot in requirements.slots:
        fixed[(absent_staff_id, target_date, slot.label)] = CellState.OFF

    # 3. 当日の他職員の固定戦略
    if prefer_on_duty_extension:
        # パス1: 出勤中職員の勤務枠をベースに延長（公休職員はOFF固定）
        for a in existing_result.assignments:
            if a.day != target_date:
                continue
            sid = a.staff_id
            if sid == absent_staff_id:
                continue
            if a.state in (CellState.WORK, CellState.BREAK):
                fixed[(sid, target_date, a.slot.label)] = a.state
            elif sid in off_duty_staff_ids:
                fixed[(sid, target_date, a.slot.label)] = CellState.OFF

        # まず出勤中職員の延長のみで解けるか試行
        res = solve_shift(
            children=children,
            staff=staff,
            requirements=requirements,
            preferences=preferences,
            weights=weights,
            fixed_assignments=fixed,
            settings=settings,
            standard=standard,
            patterns=patterns,
            pattern_mode=True,
            pairing_rules=pairing_rules,
            classrooms=classrooms,
            require_leader=require_leader,
            require_opening_closing_skill=require_opening_closing_skill,
            time_limit_sec=max(3, time_limit_sec // 2),
        )
        if res.ok:
            return res

        # パス2: 出勤中職員の延長だけでは足りない場合、公休職員のOFF固定を解除してヘルプ出勤を開放
        for sid in off_duty_staff_ids:
            if sid == absent_staff_id:
                continue
            for slot in requirements.slots:
                fixed.pop((sid, target_date, slot.label), None)

    else:
        # 従来動作: 出勤中職員の勤務枠は維持し、公休職員をすべて調整弁として開放
        for a in existing_result.assignments:
            if a.day != target_date:
                continue
            sid = a.staff_id
            if sid == absent_staff_id:
                continue
            if a.state in (CellState.WORK, CellState.BREAK):
                fixed[(sid, target_date, a.slot.label)] = a.state

    return solve_shift(
        children=children,
        staff=staff,
        requirements=requirements,
        preferences=preferences,
        weights=weights,
        fixed_assignments=fixed,
        settings=settings,
        standard=standard,
        patterns=patterns,
        pattern_mode=True,
        pairing_rules=pairing_rules,
        classrooms=classrooms,
        require_leader=require_leader,
        require_opening_closing_skill=require_opening_closing_skill,
        time_limit_sec=time_limit_sec,
    )
