"""MILP 解のデコード部品 (decoder)。

``shiftai/solver.py`` のモノリスから切り出した解デコード関数をここに集約する。

責務:
- 変数値から ShiftDay / ShiftAssignment を復元 (``decode_solution``)
- 勤務パターンの整列（スナップ） (``snap_to_patterns`` 等)
- 突発欠勤リペア (``repair_shift_absence``)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from shiftai import config
from shiftai.domain import (
    CellState,
    FacilitySettings,
    RequirementTable,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SolveResult,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.model_builder import (
    _day_is_workable,
    _is_unavailable,
    _slot_is_contractible,
    _var_value,
)
from shiftai.shift_patterns import (
    ShiftPattern,
    describe_pattern,
    match_pattern,
    normalize_patterns,
)
from shiftai.statutory_rules import daily_cap_minutes


def _extract_vars(problem_vars: object) -> tuple[dict, dict]:
    """内部モデルでも dict でも受け取れるように変数表を取り出す。"""
    if isinstance(problem_vars, Mapping):
        work = problem_vars.get("work", problem_vars.get("w", {}))
        brk = problem_vars.get("break", problem_vars.get("brk", problem_vars.get("b", {})))
        return dict(work), dict(brk)
    return dict(getattr(problem_vars, "work", {})), dict(
        getattr(problem_vars, "brk", getattr(problem_vars, "break", {}))
    )


def decode_solution(
    problem_vars: object,
    staff: Sequence[StaffMember],
    days: Sequence[date],
    slots: Sequence[Slot],
) -> tuple[list[ShiftDay], list[ShiftAssignment]]:
    """変数値から 1日分のシフト表（ShiftDay）と平坦な代入リストを復元する。"""
    work, brk = _extract_vars(problem_vars)
    staff_ids = [s.staff_id for s in staff]
    day_map = {d: ShiftDay(day=d, assignments={}) for d in days}
    assignments: list[ShiftAssignment] = []
    for sid in staff_ids:
        for day in days:
            row: dict[str, CellState] = {}
            for i, slot in enumerate(slots):
                if _var_value(work.get((sid, day, i), 0)) >= 0.5:
                    state = CellState.WORK
                elif _var_value(brk.get((sid, day, i), 0)) >= 0.5:
                    state = CellState.BREAK
                else:
                    state = CellState.OFF
                row[slot.label] = state
                assignments.append(ShiftAssignment(sid, day, slot, state))
            day_map[day].assignments[sid] = row
    return [day_map[d] for d in days], assignments


# ---------------------------------------------------------------------------
# 勤務パターンの整列（スナップ）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapChange:
    """スナップ 1 件の結果（＝1 人の 1 日分）。"""

    staff_id: str
    day: date
    before: str
    after: str
    pattern_label: str
    applied: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "職員ID": self.staff_id,
            "日付": self.day.isoformat(),
            "変更前": self.before,
            "変更後": self.after,
            "パターン": self.pattern_label,
            "適用": "済" if self.applied else "-",
            "理由": self.reason,
        }


def _target_indices(slots: Sequence[Slot], pattern: ShiftPattern) -> list[int]:
    """パターンの内側に収まる時間帯の添字（連続していなければ空）。"""
    picked = [
        i
        for i, slot in enumerate(slots)
        if slot.start_minutes >= pattern.start_minutes and slot.end_minutes <= pattern.end_minutes
    ]
    if not picked or picked[-1] - picked[0] + 1 != len(picked):
        return []
    return picked


def _snap_coverage_ok(
    cells: Mapping[tuple[str, date], Sequence[CellState]],
    staff_index: Mapping[str, StaffMember],
    need_all: Mapping[tuple[date, int], int],
    need_q: Mapping[tuple[date, int], int],
    sid: str,
    day: date,
    before: Sequence[CellState],
    after: Sequence[CellState],
    standard: StaffingStandard | None = None,
) -> bool:
    """差し替えで配置基準が壊れないかを確認する。

    「勤務 → オフ」になる時間帯だけを見る。充足から引かれる可能性があるので、
        その時間帯の現配置数から 1 を引いた時点で必要人員を下回るなら差し替えない。
    """
    member = staff_index.get(sid)
    for idx, (was, now) in enumerate(zip(before, after, strict=True)):
        had_work = was is CellState.WORK
        has_work = now is CellState.WORK
        if had_work == has_work:
            continue
        need = need_all.get((day, idx), 0)
        if need <= 0:
            continue
        if not (had_work and not has_work):
            continue
        working = sum(
            1
            for (other_sid, other_day), states in cells.items()
            if other_day == day and states[idx] is CellState.WORK
        )
        if working - 1 < need:
            return False
        needq = need_q.get((day, idx), 0)
        if needq > 0 and member is not None and member.is_qualified_under(standard):
            working_ids = [
                other_sid
                for (other_sid, other_day), states in cells.items()
                if other_day == day and states[idx] is CellState.WORK
            ]
            # `working` のときと同じ数え方（本人を含む）で、
            # 差し替え後は 1 人減るため -1 する。
            members = [m for m in (staff_index.get(o) for o in working_ids) if m]
            from shiftai.domain import qualified_count

            if qualified_count(members, standard) - 1 < needq:
                return False
    return True


def snap_to_patterns(
    result: SolveResult,
    patterns: Sequence[ShiftPattern] | None,
    *,
    requirements: RequirementTable | None = None,
    preferences: Mapping[str, StaffPreferences] | None = None,
    staff: Sequence[StaffMember] | None = None,
    standard: StaffingStandard | None = None,
    settings: FacilitySettings | None = None,
) -> tuple[SolveResult, tuple[SnapChange, ...]]:
    """勤務ブロックの境界をパターン（早番・日勤・遅番）へ寄せる後処理。

    ソルバの整列ペナルティ（``_add_pattern_alignment``）で通常は到達しない。
    人手不足などを理由に境界が揃わないときの「仕上げ」段である。

    **配置基準を破る差し替えは行わない。** 候補パターンは次のすべてを満たすもの
    だけを採用する。

    * 契約時間帯（最早始業〜最遅終業）の内側
    * 希望休・不在時間帯と重ならない
    * 休園日・休日勤務不可の日ではない
    * 1日の上限時間内に収まる
    * 外した時間帯で配置基準が満たされたまま

    :param result: 整列対象の :class:`~shiftai.domain.SolveResult`
    :param patterns: パターン定義（空なら何もしない）
    :param requirements: 配置基準（配置を破らないかの判定に使う）
    :param preferences: 職員IDごとの個人希望
    :param staff: 職員一覧
    :param standard: 休憩時間を決めるための基準
    :param settings: 園設定
    :returns: ``(整列後の結果, 1 日分の記録)``。元の結果は破壊しない。
    """
    pats = normalize_patterns(patterns)
    if not pats or not result.assignments:
        return result, ()

    slots = tuple(sorted({a.slot for a in result.assignments}))
    n = len(slots)
    if n == 0:
        return result, ()
    index_of = {slot: i for i, slot in enumerate(slots)}
    days = tuple(sd.day for sd in result.shift_days) or tuple(
        sorted({a.day for a in result.assignments})
    )
    staff_index = {m.staff_id: m for m in (staff or ())}
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    gran = config.DEFAULT_GRANULARITY_MIN
    if requirements is not None and requirements.granularity_min:
        gran = max(1, int(requirements.granularity_min))
    elif slots[0].minutes:
        gran = slots[0].minutes
    break_slots = max(1, -(-int(standard.break_minutes) // gran)) if standard is not None else 2

    base: dict[tuple[str, date], list[CellState]] = {}
    for sd in result.shift_days:
        for sid, row in sd.assignments.items():
            base[(sid, sd.day)] = [row.get(slot.label, CellState.OFF) for slot in slots]
    if not base:
        return result, ()

    need_all: dict[tuple[date, int], int] = {}
    need_q: dict[tuple[date, int], int] = {}
    if requirements is not None:
        for day in days:
            for req in requirements.for_day(day):
                idx = index_of.get(req.slot)
                if idx is None or not req.is_binding:
                    continue
                need_all[(day, idx)] = need_all.get((day, idx), 0) + req.needed_staff
                need_q[(day, idx)] = need_q.get((day, idx), 0) + req.needed_qualified

    working = {key: list(value) for key, value in base.items()}
    changes: list[SnapChange] = []
    for key in sorted(working, key=lambda k: (k[1], k[0])):
        sid, day = key
        states = working[key]
        duty = [i for i in range(n) if states[i] in (CellState.WORK, CellState.BREAK)]
        if not duty:
            continue
        before_text = (
            f"{slots[duty[0]].start.strftime('%H:%M')}-{slots[duty[-1]].end.strftime('%H:%M')}"
        )
        start_min = slots[duty[0]].start_minutes
        end_min = slots[duty[-1]].end_minutes
        exact = match_pattern(start_min, end_min, pats)
        if exact is not None:
            changes.append(
                SnapChange(
                    sid,
                    day,
                    before_text,
                    before_text,
                    exact.label,
                    False,
                    "パターンと一致",
                )
            )
            continue
        member = staff_index.get(sid)
        if member is None:
            changes.append(
                SnapChange(sid, day, before_text, before_text, "", False, "職員情報が不明")
            )
            continue
        breaks = [i for i in duty if states[i] is CellState.BREAK]
        brk_len = len(breaks) or min(break_slots, max(1, len(duty) - 1))
        offset = (breaks[0] - duty[0]) if breaks else max(0, len(duty) - brk_len)
        ordered_pats = sorted(
            pats,
            key=lambda p: abs(p.start_minutes - start_min) + abs(p.end_minutes - end_min),
        )
        applied_label = ""
        applied_after = before_text
        reason = "候補パターンが契約・配置基準に合わなかった"
        for pat in ordered_pats:
            target = _target_indices(slots, pat)
            if not target or len(target) < brk_len + 1:
                reason = "パターン境界が時間帯の区切りに合わない"
                continue
            if not _day_is_workable(member, day, fac):
                reason = "休園日・休日の勤務不可"
                continue
            if any(not _slot_is_contractible(member, slots[i]) for i in target):
                reason = "契約時間帯の外"
                continue
            if any(_is_unavailable(prefs.get(sid), day, slots[i]) for i in target):
                reason = "希望休と重なる"
                continue
            if sum(slots[i].minutes for i in target) > daily_cap_minutes(member.contract):
                reason = "1日の上限時間を超える"
                continue
            new_states = [CellState.OFF] * n
            for i in target:
                new_states[i] = CellState.WORK
            brk_at = min(max(0, offset), len(target) - brk_len)
            for k in range(brk_at, brk_at + brk_len):
                new_states[target[k]] = CellState.BREAK
            if not _snap_coverage_ok(
                working,
                staff_index,
                need_all,
                need_q,
                sid,
                day,
                states,
                new_states,
                standard,
            ):
                reason = "差し替えると配置基準が満たされなくなる"
                continue
            working[key] = new_states
            applied_label = pat.label
            applied_after = pat.span()
            reason = "パターン境界へ整列"
            break
        changes.append(
            SnapChange(
                sid,
                day,
                before_text,
                applied_after,
                applied_label,
                bool(applied_label),
                reason,
            )
        )

    moved = [c for c in changes if c.applied]
    if not moved:
        return result, tuple(changes)

    order = sorted({key[0] for key in working})
    assignments: list[ShiftAssignment] = []
    shift_days: list[ShiftDay] = []
    for day in days:
        sd = ShiftDay(day=day, assignments={})
        for sid in order:
            states = working[(sid, day)]
            sd.assignments[sid] = {slots[i].label: states[i] for i in range(n)}
            for i in range(n):
                assignments.append(ShiftAssignment(sid, day, slots[i], states[i]))
        shift_days.append(sd)

    snapped = SolveResult(
        status=result.status,
        shift_days=shift_days,
        assignments=assignments,
        objective_value=result.objective_value,
        messages=list(result.messages),
        stats=dict(result.stats),
    )
    snapped.stats["pattern_snap_applied"] = len(moved)
    snapped.stats["pattern_snap_total"] = len(changes)
    snapped.messages = [
        *result.messages,
        f"勤務パターンの整列を {len(moved)} 日分"
        f"（{len(changes) - len(moved)} 日分は据え置き）適用しました。",
    ]
    if requirements is not None and staff is not None:
        from shiftai.solver import _attach_violations

        _attach_violations(
            snapped,
            requirements,
            staff,
            prefs,
            standard,
            fac,
        )
    return snapped, tuple(changes)


def pattern_breakdown(
    result: SolveResult,
    patterns: Sequence[ShiftPattern] | None,
) -> dict[str, int]:
    """勤務日数をパターンラベルごとに数える（一致しないものは「その他」）。

    :returns: ``{"早番": 8, "日勤": 11, "遅番": 6, "その他": 3}`` のような辞書
    """
    pats = normalize_patterns(patterns)
    if not pats:
        return {}
    slots = tuple(sorted({a.slot for a in result.assignments}))
    index_of = {slot: i for i, slot in enumerate(slots)}
    per_day: dict[tuple[str, date], list[int]] = {}
    for a in result.assignments:
        if a.state in (CellState.WORK, CellState.BREAK):
            per_day.setdefault((a.staff_id, a.day), []).append(index_of[a.slot])
    counts: dict[str, int] = {}
    for key in sorted(per_day, key=lambda k: (k[1], k[0])):
        indices = sorted(per_day[key])
        if not indices:
            continue
        found = match_pattern(slots[indices[0]].start_minutes, slots[indices[-1]].end_minutes, pats)
        label = "その他" if found is None else found.label
        counts[label] = counts.get(label, 0) + 1
    return counts


def describe_shift_pattern(
    result: SolveResult,
    staff_id: str,
    day: date,
    patterns: Sequence[ShiftPattern] | None,
) -> str:
    """職員 1 人の 1 日分の勤務枠を、パターン付きで説明する。"""
    pats = normalize_patterns(patterns)
    slots = sorted({a.slot for a in result.assignments})
    index_of = {slot: i for i, slot in enumerate(slots)}
    duty = sorted(
        index_of[a.slot]
        for a in result.assignments
        if a.staff_id == staff_id and a.day == day and a.state in (CellState.WORK, CellState.BREAK)
    )
    if not duty:
        return "オフ"
    return describe_pattern(slots[duty[0]].start_minutes, slots[duty[-1]].end_minutes, pats)


# ---------------------------------------------------------------------------
# 突発欠勤リペア（repair_shift_absence）は solver.py に残す。
# decoder.py には含めず、solver.py から import することで circular import を回避する。
# ---------------------------------------------------------------------------
