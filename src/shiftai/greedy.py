"""貪欲法（ヒューリスティック）によるシフト暫定作成エンジン。

MILP ソルバー（HiGHS / CBC）が利用できない環境、または制約が厳しすぎて
解が存在しない（Infeasible）場合のフェイルセーフ・高速初期解生成として動作する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from time import perf_counter

from shiftai import config
from shiftai.domain import (
    daily_cap_minutes,
    CellState,
    ChildPlan,
    Contract,
    FacilitySettings,
    RequirementTable,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
    qualified_count,
    to_minutes,
    weekly_windows,
)

_DEFAULT_BREAK_MINUTES = config.DEFAULT_BREAK_MINUTES
_DEFAULT_STAGGER_SLOTS = 1
_BREAK_CONCURRENT_SHARE = config.BREAK_CONCURRENT_SHARE


def _daily_cap_minutes(contract: Contract) -> int:
    """職員の1日あたり勤務時間の上限（domain.daily_cap_minutes に委譲）。"""
    return daily_cap_minutes(contract)


def _day_is_workable(st: StaffMember, day: date, settings: FacilitySettings) -> bool:
    """その職員が対象日に在勤できる日かを返す（休園日・休日勤務不可の判定）。"""
    if day in settings.closed_days:
        return False
    if not st.contract.can_work_holiday:
        if day.weekday() >= config.WEEKEND_START_WEEKDAY:
            return False
        if day in settings.holiday_dates:
            return False
    return True


def _slot_is_contractible(st: StaffMember, slot: Slot) -> bool:
    """その職員が契約上その時間帯に勤務できるかを返す。"""
    contract = st.contract
    return slot.start_minutes >= to_minutes(
        contract.earliest_start
    ) and slot.end_minutes <= to_minutes(contract.latest_end)


def _is_unavailable(prefs: StaffPreferences | None, day: date, slot: Slot) -> bool:
    if prefs is None:
        return False
    return prefs.is_unavailable(day, slot)


def _checker_unavailable(exc: BaseException) -> Violation:
    return Violation(
        severity=ViolationSeverity.BLOCKER,
        code="CHECK_UNAVAILABLE",
        message=(
            "法令違反の検査に失敗したため、適合可否を確認できません"
            f"（{type(exc).__name__}: {exc}）。"
        ),
        detail={"error": repr(exc)},
    )


def _slot_by_label(slots: Sequence[Slot], label: str) -> Slot:
    for s in slots:
        if s.label == label:
            return s
    raise KeyError(label)


def _consecutive_streak(worked: set[date], day: date) -> int:
    """day まで連続して勤務した日数。"""
    streak = 0
    cur = day
    while cur in worked:
        streak += 1
        cur = cur - timedelta(days=1)
    return streak


def solve_shift_greedy(
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    *,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    standard: StaffingStandard | None = None,
    settings: FacilitySettings | None = None,
) -> SolveResult:
    """貪欲法で暫定シフトを作る（MILP が使えない/解なしのときの最終保険）。

    1日ごとに「必要人員が最も多い時間帯」から順に、その時間帯に出勤できる職員のうち
    勤務時間が最も少ない者を選んで連続した在勤ブロックを1つ積む。契約時間帯・希望休・
    休園日・1日上限・連続勤務日数・週の勤務日数・最低休息時間を尊重し、勤務ブロック
    が十分長くなれば ``|職員ID| % stagger`` の位置に連続した休憩を1つ挟む。
    在勤ブロックの連続性は必ず保つ（勤務の分裂は作らない）。

    :returns: status は常に ``PARTIAL``（貪欲法のため最適ではない）
    """
    started = perf_counter()
    # 園長・主任（配置対象外）は MILP 側と同じく充填に使わない
    staff_list = [member for member in staff if member.is_placeable]
    prefs = dict(preferences or {})
    fixed = dict(fixed_assignments or {})
    # 園設定（休園日・祝日）は MILP 側と同じものを渡さないと挙動がずれる
    settings = settings or FacilitySettings()
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    n = len(slots)
    gran = max(1, int(requirements.granularity_min or slots[0].minutes))
    stats: dict = {
        "solver": "greedy",
        "solver_status": "Greedy",
        "pass": 0,
        "relaxed": True,
        "elapsed_sec": 0.0,
        "num_variables": 0,
        "num_constraints": 0,
        "num_staff": len(staff_list),
        "num_days": len(days),
        "num_slots": len(slots),
    }
    if not staff_list or not days or not slots:
        return SolveResult(
            status=SolveStatus.ERROR,
            messages=["職員・対象日・時間帯のいずれかが空です。"],
            stats=stats,
        )

    break_minutes = int(standard.break_minutes) if standard is not None else _DEFAULT_BREAK_MINUTES
    break_slots = max(1, -(-break_minutes // gran))
    stagger = (
        max(1, int(round(standard.break_stagger_minutes / gran)))
        if standard is not None
        else _DEFAULT_STAGGER_SLOTS
    )
    index_of = {slot: i for i, slot in enumerate(slots)}
    need_all: dict[tuple[date, int], int] = {}
    need_q: dict[tuple[date, int], int] = {}
    for day in days:
        for row in requirements.for_day(day):
            if not row.is_binding:
                continue
            idx = index_of.get(row.slot)
            if idx is None:
                continue
            need_all[(day, idx)] = need_all.get((day, idx), 0) + row.needed_staff
            need_q[(day, idx)] = need_q.get((day, idx), 0) + row.needed_qualified

    by_id = {s.staff_id: s for s in staff_list}
    order = [s.staff_id for s in staff_list]
    cells: dict[tuple[str, date], list[CellState]] = {
        (sid, day): [CellState.OFF] * n for sid in order for day in days
    }
    worked_minutes: dict[str, int] = {sid: 0 for sid in order}
    worked_days: dict[str, set[date]] = {sid: set() for sid in order}
    # 「週所定出勤日数」を判定するための週区画（7 日ずつの窓）。
    workable_windows = [set(w) for w in weekly_windows(days)]
    # 利用者が確認した OFF を持つセル。充填時はこの場所を一切見ない。
    pinned_off: set[tuple[str, date, int]] = set()

    def _cell_ok(sid: str, day: date, i: int) -> bool:
        if (sid, day, i) in pinned_off:
            return False
        if not _day_is_workable(by_id[sid], day, settings):
            return False
        if not _slot_is_contractible(by_id[sid], slots[i]):
            return False
        if _is_unavailable(prefs.get(sid), day, slots[i]):
            return False
        return True

    def _day_ok(sid: str, day: date) -> bool:
        contract = by_id[sid].contract
        if contract.max_weekly_days > 0:
            for window in workable_windows:
                if day in window and len(worked_days[sid] & window) >= contract.max_weekly_days:
                    return False
        prev = day - timedelta(days=1)
        if prev in worked_days[sid]:
            streak = _consecutive_streak(worked_days[sid], prev)
            if contract.max_consecutive_days > 0 and streak >= contract.max_consecutive_days:
                return False
            last = worked_days[sid] and max(d for d in worked_days[sid] if d < day)
            if last is not None:
                end_minutes = max(
                    slots[i].end_minutes
                    for i in range(n)
                    if cells[(sid, last)][i] is CellState.WORK
                )
                rest = (day - last).days * 24 * 60 - end_minutes + slots[0].start_minutes
                if rest < contract.min_rest_hours * 60:
                    return False
        return True

    def _put_block(sid: str, day: date, idx: int) -> int:
        row = cells[(sid, day)]
        cap = _daily_cap_minutes(by_id[sid].contract)
        used = sum(
            slots[i].minutes for i in range(n) if row[i] in (CellState.WORK, CellState.BREAK)
        )
        if not _cell_ok(sid, day, idx):
            return 0
        if used + slots[idx].minutes > cap:
            return 0
        row[idx] = CellState.WORK
        used += slots[idx].minutes
        added = slots[idx].minutes
        left = idx - 1
        right = idx + 1
        blocked: set[int] = set()
        while True:
            can_left = (
                left >= 0
                and left not in blocked
                and row[left] is CellState.OFF
                and _cell_ok(sid, day, left)
            )
            can_right = (
                right < n
                and right not in blocked
                and row[right] is CellState.OFF
                and _cell_ok(sid, day, right)
            )
            if not can_left and not can_right:
                break
            if can_left and can_right:
                wl = need_all.get((day, left), 0) + need_q.get((day, left), 0)
                wr = need_all.get((day, right), 0) + need_q.get((day, right), 0)
                take_left = wl >= wr
            else:
                take_left = can_left
            i = left if take_left else right
            if used + slots[i].minutes > cap:
                blocked.add(i)
                continue
            row[i] = CellState.WORK
            used += slots[i].minutes
            added += slots[i].minutes
            left -= 1
            right += 1
        if added:
            worked_minutes[sid] += added
            worked_days[sid].add(day)
        return added

    def _put_break(sid: str, day: date) -> None:
        row = cells[(sid, day)]
        duty = [i for i in range(n) if row[i] is CellState.WORK]
        if len(duty) <= break_slots + 1:
            return
        on_duty = sum(
            1
            for other in order
            for i in range(n)
            if cells[(other, day)][i] in (CellState.WORK, CellState.BREAK)
        )
        concurrent = sum(
            1 for other in order for i in range(n) if cells[(other, day)][i] is CellState.BREAK
        )
        cap = max(1, int(_BREAK_CONCURRENT_SHARE * on_duty)) + break_slots - 1
        if concurrent >= cap:
            return
        span = len(duty) - break_slots
        pos = 1 + (len(order) + order.index(sid) * stagger) % max(1, span)
        pos = min(max(0, pos), max(0, len(duty) - break_slots))
        for j in range(break_slots):
            k = duty[pos + j]
            if row[k] is CellState.WORK:
                row[k] = CellState.BREAK

    def _working(day: date, idx: int) -> list[StaffMember]:
        return [by_id[sid] for sid in order if cells[(sid, day)][idx] is CellState.WORK]

    fixed_notes: list[str] = []
    for (fid, fday, label), state in fixed.items():
        if (fid, fday) not in cells or fid not in by_id:
            continue
        try:
            idx = slots.index(_slot_by_label(slots, label))
        except KeyError:
            fixed_notes.append(
                f"{fid} {fday.isoformat()} の時間帯「{label}」が見つからないため反映できませんでした"
            )
            continue
        if state is CellState.OFF:
            cells[(fid, fday)][idx] = CellState.OFF
            pinned_off.add((fid, fday, idx))
            continue
        if not _day_is_workable(by_id[fid], fday, settings):
            fixed_notes.append(
                f"{fid} {fday.isoformat()} は勤務可能日ではないため固定セルを反映しませんでした"
            )
            continue
        if not _slot_is_contractible(by_id[fid], slots[idx]):
            fixed_notes.append(
                f"{fid} {fday.isoformat()} の「{label}」は契約時間帯外のため固定セルを反映しませんでした"
            )
            continue
        if _is_unavailable(prefs.get(fid), fday, slots[idx]):
            fixed_notes.append(
                f"{fid} {fday.isoformat()} の「{label}」は希望休と重なるため固定セルを反映しませんでした"
            )
            continue
        cap = _daily_cap_minutes(by_id[fid].contract)
        used = sum(
            slots[i].minutes
            for i in range(n)
            if cells[(fid, fday)][i] in (CellState.WORK, CellState.BREAK)
        )
        if used + slots[idx].minutes > cap:
            fixed_notes.append(
                f"{fid} {fday.isoformat()} の「{label}」は法定1日上限（{cap // 60}時間）を"
                "超えるため固定セルを反映しませんでした"
            )
            continue
        cells[(fid, fday)][idx] = state
        if state is CellState.WORK:
            worked_minutes[fid] += slots[idx].minutes
            worked_days[fid].add(fday)

    for day in days:
        keys = [i for i in range(n) if need_all.get((day, i), 0) > 0]
        keys.sort(key=lambda i: (-need_all[(day, i)], i))
        for idx in keys:
            for _ in range(need_all[(day, idx)] + need_q[(day, idx)]):
                working = _working(day, idx)
                cur_all = len(working)
                cur_q = qualified_count(working, standard)
                short_all = need_all[(day, idx)] - cur_all
                short_q = need_q[(day, idx)] - cur_q
                if short_all <= 0 and short_q <= 0:
                    break
                want_qualified = short_q > 0
                nurse_cap = standard.nurse_as_qualified_cap if standard is not None else 0
                nurses_used = sum(1 for m in working if m.is_nurse)
                cands = [
                    sid
                    for sid in order
                    if cells[(sid, day)][idx] is CellState.OFF
                    and _cell_ok(sid, day, idx)
                    and _day_ok(sid, day)
                    and (
                        by_id[sid].is_qualified_under(standard)
                        or (by_id[sid].is_nurse and nurses_used < nurse_cap)
                        or not want_qualified
                    )
                ]
                if not cands and want_qualified:
                    cands = [
                        sid
                        for sid in order
                        if cells[(sid, day)][idx] is CellState.OFF
                        and _cell_ok(sid, day, idx)
                        and _day_ok(sid, day)
                    ]
                if not cands:
                    break
                cands.sort(key=lambda sid: (worked_minutes[sid], sid))
                chosen = cands[0]
                if _put_block(chosen, day, idx) == 0:
                    break
                _put_break(chosen, day)

    shift_days: list[ShiftDay] = []
    assignments: list[ShiftAssignment] = []
    for day in days:
        sd = ShiftDay(day=day, assignments={})
        for sid in order:
            row = cells[(sid, day)]
            sd.assignments[sid] = {slots[i].label: row[i] for i in range(n)}
            for i in range(n):
                assignments.append(ShiftAssignment(sid, day, slots[i], row[i]))
        shift_days.append(sd)

    stats["elapsed_sec"] = round(perf_counter() - started, 3)
    result = SolveResult(
        status=SolveStatus.PARTIAL,
        shift_days=shift_days,
        assignments=assignments,
        objective_value=None,
        messages=[
            "貪欲法による暫定シフトを生成しました（最適解ではありません）。",
            *fixed_notes,
        ],
        stats=stats,
    )
    try:
        from shiftai import gap_analysis

        result.violations = gap_analysis.check_violations(
            requirements, result, staff_list, prefs, standard=standard, settings=settings
        )
    except Exception as exc:
        result.violations = [_checker_unavailable(exc)]
        result.status = SolveStatus.ERROR
        result.messages = [
            *result.messages,
            f"法令違反の検査中にエラーが発生しました（{exc}）。適合可否は判定できていません。",
        ]
    return result


def staff_work_hours(result: SolveResult, staff: Sequence[StaffMember]) -> dict[str, float]:
    """職員ごとの対象期間内の勤務時間（時間単位・休憩を除く）。"""
    totals: dict[str, float] = {s.staff_id: 0.0 for s in staff}
    if isinstance(result.assignments, dict):
        for (sid, _, _), state in result.assignments.items():
            if sid in totals and state is CellState.WORK:
                totals[sid] += 0.5
        return totals
    for a in result.assignments:
        if a.staff_id in totals and a.state is CellState.WORK:
            totals[a.staff_id] += a.slot.hours
    return totals


def staff_shift_count(result: SolveResult) -> dict[str, int]:
    """職員ごとの勤務日数。"""
    days_by_staff: dict[str, set[date]] = {}
    if isinstance(result.assignments, dict):
        for (sid, d, _), state in result.assignments.items():
            if state is CellState.WORK:
                days_by_staff.setdefault(sid, set()).add(d)
    else:
        for a in result.assignments:
            if a.state is CellState.WORK:
                days_by_staff.setdefault(a.staff_id, set()).add(a.day)
    counts: dict[str, int] = {}
    for sd in result.shift_days:
        for sid in sd.assignments:
            counts.setdefault(sid, 0)
    for a in result.assignments:
        counts.setdefault(a.staff_id, 0)
    for sid, days in days_by_staff.items():
        counts[sid] = len(days)
    return counts
