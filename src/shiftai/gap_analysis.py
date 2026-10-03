"""過不足・法令違反の分析と UI 向けのレポート生成。

``analyze_gap`` は「配置基準が必要とする人数」と「実際に勤務した人数」を時間帯ごとに
突き合わせ、``check_violations`` は労働基準法と園の契約に照らして違反を列挙する。
どちらの関数も ``RequirementTable`` と ``SolveResult`` だけを受け取り、
このモジュール内でシフトを再計算することはない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean, pvariance

import pandas as pd

from shiftai import config
from shiftai.config import (
    STATUTORY_BREAK_MINUTES,
    STATUTORY_BREAK_THRESHOLDS,
    STATUTORY_MIN_REST_HOURS,
    STATUTORY_WEEKLY_WORK_HOURS,
)
from shiftai.domain import (
    CellState,
    Contract,
    FacilitySettings,
    RequirementTable,
    Slot,
    SlotKind,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
    cost_coefficient,
    qualified_ids,
    weekly_periods,
)
from shiftai.solver import staff_work_hours

_BREAK_CONCURRENT_SHARE = config.BREAK_CONCURRENT_SHARE
_STATUS_CODE: dict[SolveStatus, float] = {
    SolveStatus.OPTIMAL: 1.0,
    SolveStatus.FEASIBLE: 0.75,
    SolveStatus.PARTIAL: 0.5,
    SolveStatus.INFEASIBLE: 0.0,
    SolveStatus.ERROR: -1.0,
}


@dataclass
class SlotGap:
    """1日・1時間帯の配置過不足。"""

    day: date
    slot: Slot
    slot_kind: SlotKind
    needed_staff: int
    needed_qualified: int
    actual_staff: int
    actual_qualified: int
    overstaff: int

    @property
    def is_shortfall(self) -> bool:
        """必要人員に足りないか。"""
        return self.actual_staff < self.needed_staff

    @property
    def is_qualified_shortfall(self) -> bool:
        """必要保育士数に足りないか。"""
        return self.actual_qualified < self.needed_qualified

    @property
    def shortfall_staff(self) -> int:
        """不足人数（0 以上）。"""
        return max(0, self.needed_staff - self.actual_staff)

    @property
    def shortfall_qualified(self) -> int:
        """不足保育士数（0 以上）。"""
        return max(0, self.needed_qualified - self.actual_qualified)

    def to_dict(self) -> dict:
        return {
            "日付": self.day.isoformat(),
            "時間帯": self.slot.label,
            "開始": self.slot.start.strftime("%H:%M"),
            "終了": self.slot.end.strftime("%H:%M"),
            "時間帯区分": self.slot_kind.value,
            "必要人員": self.needed_staff,
            "必要保育士数": self.needed_qualified,
            "配置人員": self.actual_staff,
            "配置保育士数": self.actual_qualified,
            "不足": self.shortfall_staff,
            "不足保育士数": self.shortfall_qualified,
            "過剰": self.overstaff,
        }


@dataclass
class GapReport:
    """過不足の集計結果。"""

    gaps: list[SlotGap] = field(default_factory=list)
    total_shortfall_slots: int = 0
    total_shortfall_hours: float = 0.0
    total_overstaff_hours: float = 0.0
    coverage_ratio: float = 1.0
    worst_day: date | None = None
    daily_summary: dict[date, dict[str, float]] = field(default_factory=dict)
    total_qualified_shortfall_hours: float = 0.0
    total_required_hours: float = 0.0
    total_planned_hours: float = 0.0

    def to_dataframe(self) -> pd.DataFrame:
        """時間帯ごとの過不足を DataFrame にする。"""
        if not self.gaps:
            return pd.DataFrame(
                columns=[
                    "日付",
                    "時間帯",
                    "開始",
                    "終了",
                    "時間帯区分",
                    "必要人員",
                    "必要保育士数",
                    "配置人員",
                    "配置保育士数",
                    "不足",
                    "不足保育士数",
                    "過剰",
                ]
            )
        return pd.DataFrame.from_records([g.to_dict() for g in self.gaps])

    def to_html(self) -> str:
        """st.markdown(..., unsafe_allow_html=True) に渡せる HTML 文字列。"""
        return self.to_dataframe().to_html(
            index=False, escape=False, na_rep="", border=0, classes="shiftai-gap"
        )

    def daily_dataframe(self) -> pd.DataFrame:
        """日別サマリーを DataFrame にする。"""
        records = [
            {
                "日付": d.isoformat(),
                **{k: v for k, v in summary.items()},
            }
            for d, summary in sorted(self.daily_summary.items())
        ]
        return pd.DataFrame.from_records(records)


def _state_lookup(result: SolveResult) -> dict[tuple[str, date, str], CellState]:
    lookup: dict[tuple[str, date, str], CellState] = {}
    for a in result.assignments:
        lookup[(a.staff_id, a.day, a.slot.label)] = a.state
    if lookup:
        return lookup
    for sd in result.shift_days:
        for sid, row in sd.assignments.items():
            for label, state in row.items():
                lookup[(sid, sd.day, label)] = state
    return lookup


def _qualified_ids(
    staff: Sequence[StaffMember], standard: StaffingStandard | None = None
) -> set[str]:
    """この基準で「必要保育士数」を満たす職員 ID 集合。

    看護師のみなしが許される基準（企業主導型保育事業など）では看護師を
    ``nurse_as_qualified_cap`` まで数える。
    """
    return qualified_ids(staff, standard)


def _binding_rows(requirements: RequirementTable, day: date, slot: Slot) -> list:
    return [r for r in requirements.for_day(day) if r.slot == slot and r.is_binding]


def _slot_kind_of(rows: Sequence, slot: Slot, standard: StaffingStandard | None) -> SlotKind:
    for r in rows:
        if r.slot_kind is not SlotKind.NORMAL:
            return r.slot_kind
    if standard is not None:
        return standard.slot_kind(slot)
    return SlotKind.NORMAL


def _used_slots(requirements: RequirementTable) -> list[Slot]:
    used: list[Slot] = []
    for slot in requirements.slots:
        if any(any(r.slot == slot for r in rows) for rows in requirements.rows.values()):
            used.append(slot)
    return used or list(requirements.slots)


def analyze_gap(
    requirements: RequirementTable,
    result: SolveResult,
    staff: Sequence[StaffMember],
    *,
    standard: StaffingStandard | None = None,
) -> GapReport:
    """必要人員と実際の配置を時間帯ごとに比較して GapReport を作る。"""
    lookup = _state_lookup(result)
    qualified = _qualified_ids(staff, standard)
    gaps: list[SlotGap] = []
    shortfall_slots = 0
    shortfall_hours = 0.0
    qualified_shortfall_hours = 0.0
    overstaff_hours = 0.0
    required_hours = 0.0
    planned_hours = 0.0
    covered = 0.0
    daily: dict[date, dict[str, float]] = {}

    for day in requirements.all_days():
        rows_all = requirements.for_day(day)
        if not rows_all:
            continue
        acc = {
            "必要人員時間": 0.0,
            "配置人員時間": 0.0,
            "必要保育士数時間": 0.0,
            "配置保育士数時間": 0.0,
            "不足時間": 0.0,
            "過不足時間": 0.0,
            "不足時間帯数": 0.0,
            "不足保育士時間帯数": 0.0,
        }
        seen: set[Slot] = set()
        for row in rows_all:
            if row.slot in seen:
                continue
            seen.add(row.slot)
            slot = row.slot
            rows = _binding_rows(requirements, day, slot)
            if not rows:
                rows = [r for r in rows_all if r.slot == slot]
            need_all = sum(r.needed_staff for r in rows)
            need_q = sum(r.needed_qualified for r in rows)
            actual = 0
            actual_q = 0
            for s in staff:
                if not s.is_placeable:
                    # 園長・主任は保育基準の人数に計上しない
                    continue
                state = lookup.get((s.staff_id, day, slot.label), CellState.OFF)
                if state is CellState.WORK:
                    actual += 1
                    if s.staff_id in qualified:
                        actual_q += 1
            over = max(0, actual - need_all)
            gap = SlotGap(
                day=day,
                slot=slot,
                slot_kind=_slot_kind_of(rows, slot, standard),
                needed_staff=need_all,
                needed_qualified=need_q,
                actual_staff=actual,
                actual_qualified=actual_q,
                overstaff=over,
            )
            gaps.append(gap)
            hours = slot.hours
            required_hours += need_all * hours
            planned_hours += actual * hours
            covered += min(actual, need_all) * hours
            if gap.is_shortfall:
                shortfall_slots += 1
                shortfall_hours += gap.shortfall_staff * hours
            if gap.is_qualified_shortfall:
                qualified_shortfall_hours += gap.shortfall_qualified * hours
            overstaff_hours += over * hours
            acc["必要人員時間"] += need_all * hours
            acc["配置人員時間"] += actual * hours
            acc["必要保育士数時間"] += need_q * hours
            acc["配置保育士数時間"] += actual_q * hours
            if gap.is_shortfall:
                acc["不足時間帯数"] += 1
                acc["不足時間"] += gap.shortfall_staff * hours
            if gap.is_qualified_shortfall:
                acc["不足保育士時間帯数"] += 1
            acc["過不足時間"] += abs(actual - need_all) * hours
        daily[day] = acc

    worst_day: date | None = None
    worst_key = (0.0, 0.0)
    for d, acc in daily.items():
        key = (acc["不足時間"], acc["不足時間帯数"])
        if key > worst_key:
            worst_key = key
            worst_day = d

    return GapReport(
        gaps=gaps,
        total_shortfall_slots=shortfall_slots,
        total_shortfall_hours=round(shortfall_hours, 2),
        total_overstaff_hours=round(overstaff_hours, 2),
        coverage_ratio=round(covered / required_hours, 4) if required_hours > 0 else 1.0,
        worst_day=worst_day,
        daily_summary=daily,
        total_qualified_shortfall_hours=round(qualified_shortfall_hours, 2),
        total_required_hours=round(required_hours, 2),
        total_planned_hours=round(planned_hours, 2),
    )


def coverage_matrix(
    requirements: RequirementTable,
    result: SolveResult,
    staff: Sequence[StaffMember],
) -> pd.DataFrame:
    """index=日付 / columns=時間帯ラベルの「配置数/必要数」表を返す。"""
    lookup = _state_lookup(result)
    slots = _used_slots(requirements)
    records = []
    for day in requirements.all_days():
        row: dict[str, object] = {"日付": day.isoformat()}
        for slot in slots:
            rows = _binding_rows(requirements, day, slot)
            need = sum(r.needed_staff for r in rows)
            actual = sum(
                1
                for s in staff
                if lookup.get((s.staff_id, day, slot.label), CellState.OFF) is CellState.WORK
            )
            row[slot.label] = f"{actual}/{need}"
        records.append(row)
    return pd.DataFrame.from_records(records)


def gap_matrix(requirements: RequirementTable, result: SolveResult) -> pd.DataFrame:
    """index=日付 / columns=時間帯ラベルの「必要数-配置数」数値表を返す。"""
    lookup = _state_lookup(result)
    slots = _used_slots(requirements)
    records = []
    for day in requirements.all_days():
        row: dict[str, object] = {"日付": day.isoformat()}
        for slot in slots:
            need = sum(r.needed_staff for r in _binding_rows(requirements, day, slot))
            actual = sum(
                1
                for (_, d, label), state in lookup.items()
                if d == day and label == slot.label and state is CellState.WORK
            )
            row[slot.label] = need - actual
        records.append(row)
    return pd.DataFrame.from_records(records)


def _required_break_minutes(work_minutes: int) -> int:
    """労働基準法上の休憩時間（6時間超45分、8時間超60分）。"""
    required = 0
    for threshold, minutes in STATUTORY_BREAK_THRESHOLDS:
        if work_minutes > threshold:
            # STATUTORY_BREAK_THRESHOLDS は閾値の降順で並ぶため、上書きすると
            # 常に下限側の 45 分が勝つ。厳格な方（60 分）を採る。
            required = max(required, minutes)
    return required


def _day_cells(
    lookup: Mapping[tuple[str, date, str], CellState],
    sid: str,
    day: date,
    slots: Sequence[Slot],
) -> tuple[list[int], list[int]]:
    work = [
        i
        for i, s in enumerate(slots)
        if lookup.get((sid, day, s.label), CellState.OFF) is CellState.WORK
    ]
    brk = [
        i
        for i, s in enumerate(slots)
        if lookup.get((sid, day, s.label), CellState.OFF) is CellState.BREAK
    ]
    return work, brk


def _is_contiguous(indices: Sequence[int]) -> bool:
    if not indices:
        return True
    ordered = sorted(indices)
    return ordered[-1] - ordered[0] + 1 == len(ordered)


def check_violations(
    requirements: RequirementTable,
    result: SolveResult,
    staff: Sequence[StaffMember],
    preferences: Mapping[str, StaffPreferences] | None = None,
    *,
    standard: StaffingStandard | None = None,
    settings: FacilitySettings | None = None,
    period_days: int | None = None,
) -> list[Violation]:
    """配置基準・労働基準法・契約への適合を検査して Violation の一覧を返す。

    BLOCKER は法令・契約に反する確定的な不足、WARNING は調整が必要な超過や不足、
    INFO は判断材料の参考情報として扱う。

    :param period_days: 契約勤務時間を判定する際の対象日数。``None`` のときは
        ``RequirementTable`` に含まれる日数を使う。1週間だけのシフトを
        「月換算」で判定しないために必須の正規化パラメータ。
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    slots = tuple(requirements.slots)
    days = requirements.all_days()
    period_days = int(period_days) if period_days else len(days)
    lookup = _state_lookup(result)
    out: list[Violation] = []
    report = analyze_gap(requirements, result, staff, standard=standard)
    n = len(slots)

    for gap in report.gaps:
        if gap.is_shortfall:
            out.append(
                Violation(
                    severity=ViolationSeverity.BLOCKER,
                    code="SHORTFALL_STAFF",
                    message=(
                        f"{gap.day.isoformat()} {gap.slot.label} の配置が {gap.shortfall_staff} 名不足です"
                        f"（必要 {gap.needed_staff} 名 / 配置 {gap.actual_staff} 名）。"
                    ),
                    day=gap.day,
                    slot=gap.slot,
                    detail={
                        "needed": gap.needed_staff,
                        "actual": gap.actual_staff,
                        "slot_kind": gap.slot_kind.value,
                    },
                )
            )
        if gap.is_qualified_shortfall:
            out.append(
                Violation(
                    severity=ViolationSeverity.BLOCKER,
                    code="SHORTFALL_QUALIFIED",
                    message=(
                        f"{gap.day.isoformat()} {gap.slot.label} の保育士が "
                        f"{gap.shortfall_qualified} 名不足です"
                        f"（必要 {gap.needed_qualified} 名 / 配置 {gap.actual_qualified} 名）。"
                    ),
                    day=gap.day,
                    slot=gap.slot,
                    detail={
                        "needed_qualified": gap.needed_qualified,
                        "actual_qualified": gap.actual_qualified,
                        "slot_kind": gap.slot_kind.value,
                    },
                )
            )

    worked_days: dict[str, set[date]] = {}
    for a in result.assignments:
        if a.state is CellState.WORK:
            worked_days.setdefault(a.staff_id, set()).add(a.day)

    for s in staff:
        sid = s.staff_id
        p = prefs.get(sid)
        c: Contract = s.contract
        daily_hours: dict[date, float] = {}
        for day in days:
            work_idx, brk_idx = _day_cells(lookup, sid, day, slots)
            work_minutes = sum(slots[i].minutes for i in work_idx)
            if not work_idx:
                continue
            worked_days.setdefault(sid, set()).add(day)
            daily_hours[day] = work_minutes / 60.0
            cap = (
                c.daily_hours
                if not c.overtime_allowed
                else min(
                    c.daily_hours * config.STATUTORY_OVERTIME_MULTIPLIER, _DAILY_LEGAL_CAP_HOURS
                )
            )
            if work_minutes / 60.0 > cap + config.FLOAT_TOLERANCE:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="DAILY_HOURS_EXCEEDED",
                        message=(
                            f"{sid} は {day.isoformat()} に {work_minutes / 60.0:.2f} 時間勤務で、"
                            f"契約上限 {cap:.2f} 時間を超えています。"
                        ),
                        day=day,
                        staff_id=sid,
                        detail={"hours": round(work_minutes / 60.0, 2), "cap": round(cap, 2)},
                    )
                )
            if work_minutes / 60.0 > _STATUTORY_DAILY_LIMIT_HOURS + config.FLOAT_TOLERANCE:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="STATUTORY_DAILY_HOURS",
                        message=(
                            f"{sid} は {day.isoformat()} に法定の日勤上限"
                            f"（8時間+45分）を超えて勤務しています"
                            f"（{work_minutes / 60.0:.2f} 時間）。"
                        ),
                        day=day,
                        staff_id=sid,
                        detail={"hours": round(work_minutes / 60.0, 2)},
                    )
                )
            if work_idx and (work_idx[0] < 0 or work_idx[-1] >= n):
                continue
            for i in work_idx:
                slot = slots[i]
                if p is not None and p.is_unavailable(day, slot):
                    out.append(
                        Violation(
                            severity=ViolationSeverity.BLOCKER,
                            code="WORK_ON_UNAVAILABLE",
                            message=(
                                f"{sid} は {day.isoformat()} {slot.label} が不在（希望休）"
                                "にもかかわらず勤務しています。"
                            ),
                            day=day,
                            slot=slot,
                            staff_id=sid,
                            detail={"reason": "希望休・不在時間帯"},
                        )
                    )
                if day in fac.closed_days:
                    out.append(
                        Violation(
                            severity=ViolationSeverity.BLOCKER,
                            code="WORK_ON_CLOSED_DAY",
                            message=(
                                f"{sid} は休園日 {day.isoformat()} に {slot.label} 勤務しています。"
                            ),
                            day=day,
                            slot=slot,
                            staff_id=sid,
                            detail={"facility": fac.facility_name},
                        )
                    )
                if slot.start_minutes < _minutes(c.earliest_start) or slot.end_minutes > _minutes(
                    c.latest_end
                ):
                    out.append(
                        Violation(
                            severity=ViolationSeverity.WARNING,
                            code="OUTSIDE_CONTRACT_HOURS",
                            message=(
                                f"{sid} は契約時間帯（{c.earliest_start.strftime('%H:%M')}-"
                                f"{c.latest_end.strftime('%H:%M')}）外の {slot.label} に勤務しています。"
                            ),
                            day=day,
                            slot=slot,
                            staff_id=sid,
                            detail={},
                        )
                    )
            break_minutes = sum(slots[i].minutes for i in brk_idx)
            required = _required_break_minutes(work_minutes)
            if standard is not None and work_minutes > config.STATUTORY_LONG_SHIFT_MINUTES:
                required = max(required, int(standard.break_minutes))
            if required and break_minutes < required:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="BREAK_INSUFFICIENT",
                        message=(
                            f"{sid} は {day.isoformat()} の休憩が {break_minutes} 分しかなく、"
                            f"必要な {required} 分に届いていません（基準 {STATUTORY_BREAK_MINUTES} 分）。"
                        ),
                        day=day,
                        staff_id=sid,
                        detail={"break_minutes": break_minutes, "required": required},
                    )
                )
            if not _is_contiguous(brk_idx) and brk_idx:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="BREAK_FRAGMENTED",
                        message=(
                            f"{sid} は {day.isoformat()} の休憩が"
                            f"{len(brk_idx)} 時間に分かれており、連続した休憩になっていません。"
                        ),
                        day=day,
                        staff_id=sid,
                        detail={"slots": [slots[i].label for i in brk_idx]},
                    )
                )
            if not _is_contiguous(work_idx) and len(work_idx) > 1:
                runs = sum(1 for k, i in enumerate(work_idx) if k == 0 or i != work_idx[k - 1] + 1)
                if runs > 2:
                    out.append(
                        Violation(
                            severity=ViolationSeverity.WARNING,
                            code="SPLIT_SHIFT",
                            message=(
                                f"{sid} は {day.isoformat()} の勤務が {runs} ブロックに分かれています。"
                            ),
                            day=day,
                            staff_id=sid,
                            detail={"blocks": runs},
                        )
                    )

        total_hours = sum(daily_hours.values())
        band = _contract_hours_band(c, period_days)
        severity = (
            ViolationSeverity.WARNING
            if band["days"] >= _MONTHLY_WARNING_MIN_DAYS
            else ViolationSeverity.INFO
        )
        if band["max"] is not None and total_hours > band["max"] + config.FLOAT_TOLERANCE:
            out.append(
                Violation(
                    severity=severity,
                    code="MONTHLY_HOURS_EXCEEDED",
                    message=(
                        f"{sid} の勤務時間が {total_hours:.2f} 時間（{band['days']}日間）で、"
                        f"契約の年間基準から求めた上限 {band['max']:.1f} 時間を超えています。"
                        f"（週契約 {c.weekly_hours:.1f} 時間／"
                        f"月契約 {c.min_monthly_hours:.0f}〜{c.max_monthly_hours:.0f} 時間）"
                    ),
                    staff_id=sid,
                    detail={
                        "hours": round(total_hours, 2),
                        "cap": round(band["max"], 2),
                        "period_days": band["days"],
                        "weekly_hours": c.weekly_hours,
                    },
                )
            )
        if band["min"] is not None and total_hours < band["min"] - config.FLOAT_TOLERANCE:
            out.append(
                Violation(
                    severity=severity,
                    code="MONTHLY_HOURS_SHORT",
                    message=(
                        f"{sid} の勤務時間が {total_hours:.2f} 時間（{band['days']}日間）で、"
                        f"契約の年間基準から求めた下限 {band['min']:.1f} 時間に届いていません。"
                        f"（週契約 {c.weekly_hours:.1f} 時間／"
                        f"月契約 {c.min_monthly_hours:.0f}〜{c.max_monthly_hours:.0f} 時間）"
                    ),
                    staff_id=sid,
                    detail={
                        "hours": round(total_hours, 2),
                        "floor": round(band["min"], 2),
                        "period_days": band["days"],
                        "weekly_hours": c.weekly_hours,
                    },
                )
            )
        sid_worked = worked_days.get(sid, set())
        for window in weekly_periods(days):
            window_hours = sum(daily_hours.get(d, 0.0) for d in window)
            if window_hours > STATUTORY_WEEKLY_WORK_HOURS + config.FLOAT_TOLERANCE:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="WEEKLY_HOURS_EXCEEDED",
                        message=(
                            f"{sid} は {window[0].isoformat()} から {window[-1].isoformat()} の"
                            f"1 週間で {window_hours:.2f} 時間勤務し、"
                            f"週{STATUTORY_WEEKLY_WORK_HOURS:.0f}時間を超えています。"
                        ),
                        day=window[0],
                        staff_id=sid,
                        detail={
                            "hours": round(window_hours, 2),
                            "window_start": window[0].isoformat(),
                            "window_end": window[-1].isoformat(),
                            "window_days": len(window),
                        },
                    )
                )

        streak = 0
        worst_streak = 0
        streak_start: date | None = None
        worst_start: date | None = None
        for day in days:
            if day in worked_days.get(sid, set()):
                if streak == 0:
                    streak_start = day
                streak += 1
                if streak > worst_streak:
                    worst_streak = streak
                    worst_start = streak_start
            else:
                streak = 0
                streak_start = None
        # ``max_consecutive_days == 0`` は「上限なし」なので判定しない。
        # 修正前: 1 日勤務するたびに「契約上限（0 日）を超えています」と
        # 誤った違反が出ていた（``max_weekly_days`` には同種のガードがある）。
        if c.max_consecutive_days > 0 and worst_streak > c.max_consecutive_days:
            out.append(
                Violation(
                    severity=ViolationSeverity.WARNING,
                    code="CONSECUTIVE_DAYS",
                    message=(
                        f"{sid} は {worst_start.isoformat()} から {worst_streak} 日連続勤務しており、"
                        f"契約上限（{c.max_consecutive_days} 日）を超えています。"
                    ),
                    day=worst_start,
                    staff_id=sid,
                    detail={"streak": worst_streak, "cap": c.max_consecutive_days},
                )
            )
        if c.max_weekly_days > 0:
            for window in weekly_periods(days):
                worked_in_window = len(sid_worked & set(window))
                if worked_in_window > c.max_weekly_days:
                    out.append(
                        Violation(
                            severity=ViolationSeverity.WARNING,
                            code="WEEKLY_DAYS_EXCEEDED",
                            message=(
                                f"{sid} は {window[0].isoformat()} から {window[-1].isoformat()} の"
                                f"1 週間で {worked_in_window} 日出勤し、"
                                f"契約上限（週 {c.max_weekly_days} 日）を超えています。"
                            ),
                            day=window[0],
                            staff_id=sid,
                            detail={
                                "days": worked_in_window,
                                "cap": c.max_weekly_days,
                                "window_start": window[0].isoformat(),
                                "window_end": window[-1].isoformat(),
                                "window_days": len(window),
                            },
                        )
                    )

        for a, b in zip(days, days[1:], strict=False):
            prev = [i for i in range(n) if lookup.get((sid, a, slots[i].label)) is CellState.WORK]
            cur = [i for i in range(n) if lookup.get((sid, b, slots[i].label)) is CellState.WORK]
            if not prev or not cur:
                continue
            rest = (
                (b - a).days * 24 * 60 - slots[prev[-1]].end_minutes + slots[cur[0]].start_minutes
            )
            if rest < c.min_rest_hours * 60 - config.FLOAT_TOLERANCE:
                out.append(
                    Violation(
                        severity=ViolationSeverity.WARNING,
                        code="REST_HOURS_SHORT",
                        message=(
                            f"{sid} は {a.isoformat()} と {b.isoformat()} の勤務間休息が"
                            f" {rest / 60.0:.1f} 時間で、法定{c.min_rest_hours:.0f}時間"
                            f"（既定{STATUTORY_MIN_REST_HOURS:.0f}時間）に届いていません。"
                        ),
                        day=b,
                        staff_id=sid,
                        detail={"rest_hours": round(rest / 60.0, 2)},
                    )
                )

        if p is not None:
            early_days = {
                d
                for d in days
                if any(
                    lookup.get((sid, d, slots[i].label)) is CellState.WORK
                    for i, s in enumerate(slots)
                    if (standard.slot_kind(s) if standard else SlotKind.NORMAL) is SlotKind.EARLY
                )
            }
            late_days = {
                d
                for d in days
                if any(
                    lookup.get((sid, d, slots[i].label)) is CellState.WORK
                    for i, s in enumerate(slots)
                    if (standard.slot_kind(s) if standard else SlotKind.NORMAL)
                    in (SlotKind.LATE, SlotKind.LATE_STRICT)
                )
            }
            if p.avoid_early and len(early_days) > p.max_early_shifts:
                out.append(
                    Violation(
                        severity=ViolationSeverity.INFO,
                        code="AVOID_EARLY_CONFLICT",
                        message=(
                            f"{sid} は早朝保育を希望しない設定ですが {len(early_days)} 日"
                            f"（上限 {p.max_early_shifts} 日）配置されています。"
                        ),
                        staff_id=sid,
                        detail={"days": len(early_days), "cap": p.max_early_shifts},
                    )
                )
            if p.avoid_late and len(late_days) > p.max_late_shifts:
                out.append(
                    Violation(
                        severity=ViolationSeverity.INFO,
                        code="AVOID_LATE_CONFLICT",
                        message=(
                            f"{sid} は延長保育を希望しない設定ですが {len(late_days)} 日"
                            f"（上限 {p.max_late_shifts} 日）配置されています。"
                        ),
                        staff_id=sid,
                        detail={"days": len(late_days), "cap": p.max_late_shifts},
                    )
                )

    # 「同時刻の休憩者数」を時間帯ごとに数える（全職員_pool に対する1回の検査）。
    # 以前は職員ループの中で 1 日全体の休憩セル総数と在勤セル総数を比べていたため、
    # (1) 同時かどうかとは無関係に INFO が出ており、
    # (2) 職員数ぶんの重複した違反が stacked されていた。
    worst_day: date | None = None
    worst_at: Slot | None = None
    worst_count = 0
    for d in days:
        for i in range(n):
            on_duty = sum(
                1
                for member in staff
                if lookup.get((member.staff_id, d, slots[i].label), CellState.OFF)
                in (CellState.WORK, CellState.BREAK)
            )
            breaks = sum(
                1
                for member in staff
                if lookup.get((member.staff_id, d, slots[i].label), CellState.OFF)
                is CellState.BREAK
            )
            if on_duty == 0 or breaks <= 1:
                continue
            if breaks > max(1, int(_BREAK_CONCURRENT_SHARE * on_duty)) and breaks > worst_count:
                worst_count = breaks
                worst_day = d
                worst_at = slots[i]
    if worst_day is not None and worst_at is not None:
        out.append(
            Violation(
                severity=ViolationSeverity.INFO,
                code="BREAK_OVERLAP",
                message=(
                    f"{worst_day.isoformat()} {worst_at.label} は同時に休憩する職員が"
                    f" {worst_count} 名います（目安は在勤者数の25%）。"
                ),
                day=worst_day,
                slot=worst_at,
                detail={"concurrent_breaks": worst_count, "slot_label": worst_at.label},
            )
        )

    hours_map = staff_work_hours(result, staff)
    if hours_map:
        top = max(hours_map.values())
        bottom = min(hours_map.values())
        if top - bottom > max(8.0, fmean(hours_map.values()) * 0.4):
            out.append(
                Violation(
                    severity=ViolationSeverity.INFO,
                    code="HOURS_IMBALANCE",
                    message=(
                        f"勤務時間の偏りが大きいです（最大 {top:.1f} 時間 / 最小 {bottom:.1f} 時間）。"
                    ),
                    detail={"max": round(top, 2), "min": round(bottom, 2)},
                )
            )

    out.sort(
        key=lambda v: (
            0 if v.severity is ViolationSeverity.BLOCKER else 1,
            1 if v.severity is ViolationSeverity.INFO else 0,
            v.day or date.min,
            v.code,
        )
    )
    return out


_DAILY_LEGAL_CAP_HOURS = config.STATUTORY_MAX_DAILY_WORK_HOURS
_STATUTORY_DAILY_LIMIT_HOURS = config.STATUTORY_DAILY_LIMIT_HOURS
_MONTHLY_WARNING_MIN_DAYS = 5
"""契約勤務時間の警告を WARNING として出すのに必要な最小日数。"""


VIOLATION_CODE_LABELS: dict[str, str] = {
    "CHECK_UNAVAILABLE": "法令違反の検査が完了せず、適合性を判定できていません。",
    "SHORTFALL_STAFF": "配置基準の必要人員に足りない時間帯があります（法令違反）。",
    "SHORTFALL_QUALIFIED": "配置基準の必要保育士数に足りない時間帯があります（法令違反）。",
    "WORK_ON_UNAVAILABLE": "希望休・不在時間帯に勤務しています（ハード制約違反）。",
    "WORK_ON_CLOSED_DAY": "休園日に勤務しています。",
    "DAILY_HOURS_EXCEEDED": "1日の勤務時間が契約上限を超えています。",
    "STATUTORY_DAILY_HOURS": "1日の勤務時間が法定上限（8時間+45分）を超えています。",
    "OUTSIDE_CONTRACT_HOURS": "契約の勤務時間帯外で勤務しています。",
    "MONTHLY_HOURS_EXCEEDED": "対象期間の勤務時間が契約基準の上限を超えています。",
    "MONTHLY_HOURS_SHORT": "対象期間の勤務時間が契約基準の下限に届いていません。",
    "WEEKLY_HOURS_EXCEEDED": "週の勤務時間が内部目安の週44時間を超えている週があります。",
    "WEEKLY_DAYS_EXCEEDED": "週の勤務日数が契約の週最大出勤日数を超えている週があります。",
    "CONSECUTIVE_DAYS": "連続勤務日数が契約上限を超えています。",
    "REST_HOURS_SHORT": "勤務間の休息時間が11時間を下回っています。",
    "BREAK_INSUFFICIENT": "勤務に対する休憩時間が不足しています（45分/60分）。",
    "BREAK_FRAGMENTED": "休憩が複数の時間帯に分かれており、連続した休憩になっていません。",
    "SPLIT_SHIFT": "勤務が複数のブロックに分かれています（分割勤務）。",
    "BREAK_OVERLAP": "同時刻の休憩者が在勤者数の目安（25%）を超えています。",
    "AVOID_EARLY_CONFLICT": "早朝保育を希望しない職員が早朝保育に配置されています。",
    "AVOID_LATE_CONFLICT": "延長保育を希望しない職員が延長保育に配置されています。",
    "HOURS_IMBALANCE": "職員間の勤務時間の偏りが大きいです。",
}


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def _contract_hours_band(contract: Contract, period_days: int) -> dict:
    """対象期間に対する契約勤務時間の許容幅を時間単位で求める。

    労働契約は「週○○時間」が第一の基準で、「月○○〜○○時間」はその補足である。
    1週間だけのシフトを「月換算」で判定すると 26 時間と 110 時間を比べるような
    無意味な比較になるため、以下の順に幅を決める。

    1. 週契約から期待値を求める: ``weekly_hours * 日数 / 7``
    2. 期待値の上下15%を緩衝幅とし、月契約（1か月=4.345週換算）から求めた
       幅と合わせ、緩衝幅のほうを採用する（緩衝幅があれば十分広い）
    3. ``min_monthly_hours`` が 0 の職員は下限判定しない
    """
    days = max(1, int(period_days))
    expected = contract.weekly_hours * days / 7.0
    lo = expected * 0.85
    hi = expected * 1.15
    if contract.min_monthly_hours > 0:
        lo = min(lo, contract.min_monthly_hours / 4.345 * days / 7.0)
    if contract.max_monthly_hours > 0:
        hi = max(hi, contract.max_monthly_hours / 4.345 * days / 7.0)
    return {
        "days": days,
        "expected": round(expected, 2),
        "min": round(lo, 2),
        "max": round(hi, 2),
    }


def compute_cost(
    result: SolveResult,
    staff: Sequence[StaffMember],
    settings: FacilitySettings | None = None,
) -> float:
    """人件費（円相当）の概算を返す。

    **実働基準**（勤務した時間帯のみ。休憩 ``CellState.BREAK`` は除く）で計算する。
    給与 CSV（``exporter.payroll_dataframe``）と同じ式・同じ係数を使うため、
    画面と ``payroll.csv`` の合計が一致する。旧実装は休憩を在勤時間として
    扱っていたため CSV とはずれており、係数も 1.6 / 1.25 と異なっていた。
    """
    fac = settings or FacilitySettings()
    weight_by_staff = {s.staff_id: cost_coefficient(s.contract.employment_type) for s in staff}
    total = 0.0
    for a in result.assignments:
        if a.state is not CellState.WORK:
            continue
        total += a.slot.hours * weight_by_staff.get(a.staff_id, 1.0) * fac.labor_cost_per_hour
    return round(total, 1)


def summarize(
    result: SolveResult,
    report: GapReport,
    staff: Sequence[StaffMember],
    settings: FacilitySettings | None = None,
) -> dict[str, float]:
    """UI のサマリーカード用に、状態と主要指標を数値の辞書で返す。

    :param settings: 人件費の単価に用いる園設定。省略時は既定の単価になる。
    """
    hours = staff_work_hours(result, staff)
    values = list(hours.values())
    days = {a.day for a in result.assignments}
    worked = sum(1 for a in result.assignments if a.state is CellState.WORK)
    summary: dict[str, float] = {
        "ステータスコード": _STATUS_CODE.get(result.status, 0.0),
        "職員数": float(len(staff)),
        "対象日数": float(len(days)),
        "総勤務時間": round(sum(values), 2),
        "平均勤務時間": round(fmean(values), 2) if values else 0.0,
        "最大勤務時間": round(max(values), 2) if values else 0.0,
        "最小勤務時間": round(min(values), 2) if values else 0.0,
        "時間方差": round(pvariance(values), 3) if len(values) > 1 else 0.0,
        "必要人員時間": report.total_required_hours,
        "配置人員時間": report.total_planned_hours,
        "不足時間帯数": float(report.total_shortfall_slots),
        "不足時間": report.total_shortfall_hours,
        "不足保育士時間": report.total_qualified_shortfall_hours,
        "過剰時間": report.total_overstaff_hours,
        "充足率": report.coverage_ratio,
        "人件費": compute_cost(result, staff, settings),
        "勤務セル数": float(worked),
        "目的関数": float(result.objective_value or 0.0),
        "所要秒数": float(result.stats.get("elapsed_sec", 0.0) or 0.0),
        "法令違反件数": float(
            sum(1 for v in result.violations if v.severity is ViolationSeverity.BLOCKER)
        ),
        "要調整件数": float(
            sum(1 for v in result.violations if v.severity is ViolationSeverity.WARNING)
        ),
    }
    if report.worst_day is not None:
        summary["最大不足日"] = float(report.worst_day.toordinal())
    return summary
