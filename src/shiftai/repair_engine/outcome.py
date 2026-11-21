"""RepairOutcome: Result container and converter to SolveResult."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional, Sequence

from shiftai.domain import CellState
from shiftai.monthly import SolveResult, SolveStatus
from .gap_scanner import SlotGap


@dataclass
class RepairOutcome:
    """ルールベースリペアの実行結果"""

    status: str  # SUCCESS / PARTIAL / UNREPAIRED
    assigned_slots: dict[str, list[str]]  # staff_id -> 追加割当されたスロット一覧
    unfilled_gaps: list[SlotGap]
    action_logs: list[str]
    tier: int = 0  # 0: Rule, 1: LAHC, 2: MILP
    elapsed_sec: float = 0.0

    @property
    def is_fully_repaired(self) -> bool:
        """不足が1件もなく完全修復されたか"""
        return len(self.unfilled_gaps) == 0

    def to_solve_result(
        self,
        original_result: SolveResult,
        target_date: date,
        absent_staff_ids: Sequence[str],
        slots: Optional[Sequence[Any]] = None,
    ) -> SolveResult:
        """元の SolveResult をベースに、欠勤者OFF化および代行割当を反映した SolveResult を生成する。"""
        from shiftai.domain import ShiftAssignment, ShiftDay

        absent_set = set(str(sid) for sid in absent_staff_ids)
        is_list_type = isinstance(original_result.assignments, (list, tuple))

        # 1. assignments の更新
        if is_list_type:
            new_assignments_list: list[ShiftAssignment] = []
            for a in original_result.assignments:
                sid = str(a.staff_id)
                d = a.day
                sl_label = a.slot.label if hasattr(a.slot, "label") else str(a.slot)

                if d == target_date and sid in absent_set:
                    # 欠勤者を OFF に
                    new_assignments_list.append(
                        ShiftAssignment(staff_id=sid, day=d, slot=a.slot, state=CellState.OFF)
                    )
                elif d == target_date and sid in self.assigned_slots and sl_label in self.assigned_slots[sid]:
                    # 追加割当されたスロットを WORK に
                    new_assignments_list.append(
                        ShiftAssignment(staff_id=sid, day=d, slot=a.slot, state=CellState.WORK)
                    )
                else:
                    new_assignments_list.append(a)
            new_assignments = new_assignments_list
        else:
            new_assignments_dict = dict(original_result.assignments)
            for sid in absent_set:
                keys_to_off = [
                    k for k in new_assignments_dict
                    if isinstance(k, tuple) and len(k) >= 3 and str(k[0]) == sid and k[1] == target_date
                ]
                for k in keys_to_off:
                    new_assignments_dict[k] = CellState.OFF

            for sid, slot_list in self.assigned_slots.items():
                for sl in slot_list:
                    new_assignments_dict[(sid, target_date, sl)] = CellState.WORK
            new_assignments = new_assignments_dict

        # 2. shift_days の更新
        new_shift_days: list[ShiftDay] = []
        for sd in (original_result.shift_days or []):
            if sd.day == target_date:
                updated_day_assigns = {
                    sid: dict(slot_dict) for sid, slot_dict in sd.assignments.items()
                }
                # 欠勤者を OFF に
                for sid in absent_set:
                    if sid in updated_day_assigns:
                        for sl_k in updated_day_assigns[sid]:
                            updated_day_assigns[sid][sl_k] = CellState.OFF
                # 追加割当を WORK に
                for sid, s_list in self.assigned_slots.items():
                    updated_day_assigns.setdefault(sid, {})
                    for sl_k in s_list:
                        updated_day_assigns[sid][sl_k] = CellState.WORK
                new_shift_days.append(ShiftDay(day=target_date, assignments=updated_day_assigns))
            else:
                new_shift_days.append(sd)

        # 3. ステータス判定
        final_status = (
            SolveStatus.OPTIMAL
            if self.is_fully_repaired
            else SolveStatus.FEASIBLE
        )

        # 4. メッセージ・stats統合
        tier_label = f"Tier {self.tier} (Rule Engine)" if self.tier == 0 else f"Tier {self.tier}"
        new_messages = list(original_result.messages or [])
        new_messages.append(f"Repaired via {tier_label} in {self.elapsed_sec * 1000:.2f}ms")
        new_messages.extend(self.action_logs)

        stats = dict(original_result.stats or {})
        stats["repaired"] = True
        stats["repair_tier"] = self.tier
        stats["repair_elapsed_sec"] = self.elapsed_sec
        stats["unfilled_gap_count"] = len(self.unfilled_gaps)

        res = SolveResult(
            status=final_status,
            shift_days=new_shift_days,
            assignments=new_assignments,
            objective_value=original_result.objective_value,
            violations=original_result.violations,
            messages=new_messages,
            stats=stats,
            score=original_result.score,
            overconstrained_shortfalls=original_result.overconstrained_shortfalls,
        )
        return res
