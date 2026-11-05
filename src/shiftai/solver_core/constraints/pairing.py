"""Staff pairing rules (must work together or must not work together)."""

from __future__ import annotations

from collections.abc import Sequence

import pulp

from shiftai.domain import PairingRule, RequirementTable, StaffMember
from shiftai.solver_core.context import (
    _is_zero,
    _ModelCtx,
)


def _add_pairing_rules(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    rules: Sequence[PairingRule],
    obj: list[object],
) -> None:
    """職員ペアリング制約（相性・OJT指導）をMILPモデルに追加する。

    - require_together: Aが出勤しているスロットはBも必ず出勤 (work_A <= work_B)
    - avoid_together: AとBは同一スロットに同時出勤しない (work_A + work_B <= 1)
    """
    if not rules:
        return

    days = requirements.all_days()
    slots = tuple(requirements.slots)
    staff_ids = {st.staff_id for st in staff}

    for r_idx, rule in enumerate(rules):
        sid_a = rule.staff_a_id
        sid_b = rule.staff_b_id
        if sid_a not in staff_ids or sid_b not in staff_ids:
            continue

        for day in days:
            tag = day.strftime("%m%d")
            for s_idx, _slot in enumerate(slots):
                # 在勤（勤務 + 休憩）ブロック全体を考慮
                w_a = ctx.work.get((sid_a, day, s_idx), 0)
                b_a = ctx.brk.get((sid_a, day, s_idx), 0)
                w_b = ctx.work.get((sid_b, day, s_idx), 0)
                b_b = ctx.brk.get((sid_b, day, s_idx), 0)

                duty_a = w_a if _is_zero(b_a) else (b_a if _is_zero(w_a) else w_a + b_a)
                duty_b = w_b if _is_zero(b_b) else (b_b if _is_zero(w_b) else w_b + b_b)

                if _is_zero(duty_a) and _is_zero(duty_b):
                    continue

                c_name = f"pair_{r_idx}_{tag}_{s_idx}"
                if rule.kind == "require_together":
                    # staff_a が出勤なら staff_b も出勤 (在勤ブロックで一致): duty_a <= duty_b
                    if rule.is_hard:
                        ctx.prob += duty_a <= duty_b, c_name
                    else:
                        slack = pulp.LpVariable(f"slk_{c_name}", 0, 1)
                        ctx.prob += duty_a <= duty_b + slack, c_name
                        obj.append(rule.penalty * slack)

                elif rule.kind == "avoid_together":
                    # staff_a と staff_b は同一時間帯に同席不可 (在勤ブロック排他): duty_a + duty_b <= 1
                    if rule.is_hard:
                        ctx.prob += duty_a + duty_b <= 1, c_name
                    else:
                        slack = pulp.LpVariable(f"slk_{c_name}", 0, 1)
                        ctx.prob += duty_a + duty_b <= 1 + slack, c_name
                        obj.append(rule.penalty * slack)


