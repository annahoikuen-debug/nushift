"""Skills, leadership, and classroom-specific staffing constraints.

``shiftai.solver`` から遅延 import される専用モジュール。
solver.py は 3,200 行を超えているため、クラス配置・開所/閉所スキルの
制約だけここに分離している（``solver._build_problem`` から呼ばれる）。
"""

from __future__ import annotations

from collections.abc import Sequence

from shiftai.domain import (
    ClassRoom,
    ObjectiveWeights,
    RequirementTable,
    SlotKind,
    StaffingStandard,
    StaffMember,
)
from shiftai.solver_core.constraints_util import _ModelCtx, _hard_ge, _is_zero, _linear


def _add_classroom_and_skill_constraints(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
    standard: StaffingStandard | None = None,
    classrooms: Sequence[ClassRoom] | None = None,
    require_leader: bool = False,
    require_opening_closing_skill: bool = False,
) -> None:
    """開所・閉所スキル要件、日次当番リーダー、クラス別常駐要件をモデルに追加する。

    :param classrooms: 保育室・クラス定義一覧（0歳児クラス等、指定クラスの担任最低常駐数を課す）。
    :param require_leader: 日次で最低1名のリーダー適格者（can_lead=True）が勤務していることを要求する。
    :param require_opening_closing_skill: 開所スロット（SlotKind.EARLY）に can_open=True の職員、
        閉所スロット（SlotKind.LATE / LATE_STRICT）に can_close=True の職員の常駐を要求する。
    """
    if not classrooms and not require_leader and not require_opening_closing_skill:
        return

    slots = tuple(requirements.slots)
    kinds = [standard.slot_kind(s) if standard else SlotKind.NORMAL for s in slots]
    days = requirements.all_days()

    for day in days:
        tag = day.strftime("%m%d")

        # 1. 開所・閉所スキル要件
        if require_opening_closing_skill:
            for idx, kind in enumerate(kinds):
                if kind is SlotKind.EARLY:
                    openers = [
                        ctx.work[(s.staff_id, day, idx)]
                        for s in staff
                        if s.can_open
                        and (s.staff_id, day, idx) in ctx.work
                        and not _is_zero(ctx.work[(s.staff_id, day, idx)])
                    ]
                    if openers:
                        _hard_ge(ctx, _linear([(v, 1) for v in openers]), 1.0, f"skill_open_{tag}_{idx}")
                    else:
                        # 候補変数が無い（全員確定OFF等）でも要件は破られているので
                        # 無警告で無視せず conflicts に記録する。
                        ctx.conflicts.append(
                            f"{day.isoformat()} {slots[idx].label} の開所スキル（can_open）職員を配置できません"
                        )
                elif kind in (SlotKind.LATE, SlotKind.LATE_STRICT):
                    closers = [
                        ctx.work[(s.staff_id, day, idx)]
                        for s in staff
                        if s.can_close
                        and (s.staff_id, day, idx) in ctx.work
                        and not _is_zero(ctx.work[(s.staff_id, day, idx)])
                    ]
                    if closers:
                        _hard_ge(ctx, _linear([(v, 1) for v in closers]), 1.0, f"skill_close_{tag}_{idx}")
                    else:
                        ctx.conflicts.append(
                            f"{day.isoformat()} {slots[idx].label} の閉所スキル（can_close）職員を配置できません"
                        )

        # 2. 日次リーダー要件
        if require_leader:
            leader_day_vars = []
            for s in staff:
                if not s.can_lead:
                    continue
                dvar = ctx.day_var.get((s.staff_id, day))
                if dvar is not None and not _is_zero(dvar):
                    leader_day_vars.append(dvar)
            if leader_day_vars:
                _hard_ge(ctx, _linear([(v, 1) for v in leader_day_vars]), 1.0, f"leader_present_{tag}")
            else:
                ctx.conflicts.append(
                    f"{day.isoformat()} にリーダー適格者（can_lead）を配置できません"
                )

        # 3. クラス別担任・最低常駐要件
        if classrooms:
            for room in classrooms:
                if room.min_staff <= 0:
                    continue
                class_staff = [s for s in staff if s.assigned_class_id == room.class_id]
                if not class_staff:
                    ctx.conflicts.append(
                        f"{room.class_id} に配置できる職員がいないため {room.min_staff} 名の常駐要件を満たせません"
                    )
                    continue
                for idx, _slot in enumerate(slots):
                    if kinds[idx] in (SlotKind.STANDARD_TIME, SlotKind.NORMAL):
                        c_workers = [
                            ctx.work[(s.staff_id, day, idx)]
                            for s in class_staff
                            if (s.staff_id, day, idx) in ctx.work
                            and not _is_zero(ctx.work[(s.staff_id, day, idx)])
                        ]
                        if c_workers:
                            _hard_ge(
                                ctx,
                                _linear([(v, 1) for v in c_workers]),
                                float(room.min_staff),
                                f"room_{room.class_id}_{tag}_{idx}",
                            )
                        else:
                            ctx.conflicts.append(
                                f"{day.isoformat()} {slots[idx].label} に {room.class_id} の職員を"
                                f"{room.min_staff} 名配置できません"
                            )
