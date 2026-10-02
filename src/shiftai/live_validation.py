"""シフト表の手動編集を「確定前」に検証する（即時バリデーション）。

**このモジュールが解決する問題**

タブ4 の ``st.data_editor`` で 1 セル動かしても、判定は「変更を確定して再最適化」
を押すまで行われない。急な休み対応では

1. シフトを確定する
2. 1 セルだけ動かす
3. 「あれ、これ基準割ってないかも」と気づく
4. 確定を取り消して、最初から基準を満たすまで調整し直す

という往復が発生する。ここで言う **その場（確定前）** の判定を、
副作用のない純粋関数として提供する。

**判定区分**

* ``error`` … 配置基準・保育士数・契約時間帯・希望休に抵触。再最適化しても入らない。
* ``warning`` … 港湾基準・分割勤務など、確定はできるが悪い状態。

**依存関係**

``gap_analysis`` の内部ヘルパー（:_func:`gap_analysis._required_break_minutes` /
:func:`gap_analysis._is_contiguous` / :func:`gap_analysis._state_lookup`）を参照する。
逆向きに ``gap_analysis`` 側は本モジュールを import しない（循環依存を避ける）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from shiftai.domain import (
    CellState,
    FacilitySettings,
    RequirementTable,
    Slot,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    qualified_count,
    to_minutes,
)
from shiftai.fairness import block_indices
from shiftai.gap_analysis import _is_contiguous, _required_break_minutes

__all__ = [
    "ERROR",
    "LEVELS",
    "WARNING",
    "LiveIssue",
    "LiveReport",
    "states_from_grid",
    "to_dataframe",
    "validate_day",
]

ERROR = "error"
WARNING = "warning"
LEVELS: tuple[str, str] = (ERROR, WARNING)

_LEVEL_LABELS: dict[str, str] = {
    ERROR: "配置基準・契約違反",
    WARNING: "運用上の注意",
}


@dataclass(frozen=True)
class LiveIssue:
    """指摘 1 件。

    :param staff_id: 該当職員（時間帯全体の指摘なら空文字）
    :param slot_label: 該当時間帯（職員全体の指摘なら空文字）
    """

    level: str
    code: str
    message: str
    staff_id: str = ""
    slot_label: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == ERROR

    @property
    def level_label(self) -> str:
        return _LEVEL_LABELS.get(self.level, self.level)


@dataclass(frozen=True)
class LiveReport:
    """1 日分の検証結果。"""

    day: date
    issues: tuple[LiveIssue, ...] = ()

    @property
    def errors(self) -> tuple[LiveIssue, ...]:
        return tuple(i for i in self.issues if i.is_error)

    @property
    def warnings(self) -> tuple[LiveIssue, ...]:
        return tuple(i for i in self.issues if not i.is_error)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def summary(self) -> str:
        if not self.issues:
            return "指摘はありません。"
        return f"エラー {len(self.errors)} 件・注意 {len(self.warnings)} 件"

    def error_cells(self) -> set[tuple[str, str]]:
        """赤枠を付ける ``(職員ID, 時間帯ラベル)`` の集合。"""
        return {(i.staff_id, i.slot_label) for i in self.errors if i.staff_id and i.slot_label}

    def warning_cells(self) -> set[tuple[str, str]]:
        """橙枠を付ける ``(職員ID, 時間帯ラベル)`` の集合。"""
        return {(i.staff_id, i.slot_label) for i in self.warnings if i.staff_id and i.slot_label}

    def error_columns(self) -> set[str]:
        """列側（時間帯全体）の指摘で赤くする時間帯ラベル。"""
        return {i.slot_label for i in self.errors if not i.staff_id and i.slot_label}

    def warning_columns(self) -> set[str]:
        return {i.slot_label for i in self.warnings if not i.staff_id and i.slot_label}


def states_from_grid(
    grid: Mapping[str, Any] | pd.DataFrame,
    staff: Sequence[StaffMember],
    slots: Sequence[Slot],
) -> dict[str, list[CellState]]:
    """編集済みグリッドを ``職員ID -> 時間帯ごとの CellState`` に変換する。

    受け付ける形は 3 つ。

    * ``{職員ID: [CellState, ...]}``（時間帯順に並べた列）
    * ``{職員ID: {"09:00-09:30": CellState, ...}}``（時間帯ラベルをキーにした列）
    * ``DataFrame``（index が職員ID、または「職員ID 氏名」）

    解釈できない値は ``CellState.OFF`` として扱う（=「勤務しない」）。
    """
    out: dict[str, list[CellState]] = {}
    for member in staff:
        sid = member.staff_id
        row: Any = None
        if isinstance(grid, pd.DataFrame):
            if sid in grid.index:
                row = grid.loc[sid]
            elif f"{sid} {member.name}" in grid.index:
                row = grid.loc[f"{sid} {member.name}"]
        else:
            row = grid.get(sid)
        out[sid] = _row_states(row, slots)
    return out


def _row_states(row: Any, slots: Sequence[Slot]) -> list[CellState]:
    """1 行（Series / Mapping / 時間帯の列）を CellState の列にする。"""
    if row is None:
        return [CellState.OFF] * len(slots)
    if isinstance(row, pd.Series):
        row = row.to_dict()
    if isinstance(row, Mapping):
        return [_coerce_state(row.get(slot.label)) for slot in slots]
    values = list(row)
    return [
        _coerce_state(values[i]) if i < len(values) else CellState.OFF for i in range(len(slots))
    ]


def _coerce_state(value: Any) -> CellState:
    if isinstance(value, CellState):
        return value
    try:
        return CellState(str(value))
    except ValueError:
        return CellState.OFF


def to_dataframe(report: LiveReport) -> pd.DataFrame:
    """指摘を表として返す（深刻度順・職員順）。"""
    columns = ["深刻度", "種別", "職員ID", "時間帯", "内容"]
    if not report.issues:
        return pd.DataFrame(columns=columns)
    order = {ERROR: 0, WARNING: 1}
    rows = [
        {
            "深刻度": i.level_label,
            "種別": i.code,
            "職員ID": i.staff_id,
            "時間帯": i.slot_label,
            "内容": i.message,
        }
        for i in sorted(
            report.issues,
            key=lambda i: (order.get(i.level, 9), i.staff_id, i.slot_label, i.code),
        )
    ]
    return pd.DataFrame.from_records(rows, columns=columns)


def validate_day(
    day: date,
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    slots: Sequence[Slot],
    grid: Mapping[str, Sequence[CellState]] | pd.DataFrame,
    *,
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
    standard: StaffingStandard | None = None,
    staff_name_map: Mapping[str, str] | None = None,
) -> LiveReport:
    """編集途中の 1 日分を検証して :class:`LiveReport` を返す。

    :param grid: :func:`states_from_grid` が受け付ける形式
    :param staff_name_map: ``職員ID -> 氏名``（メッセージに使う）
    :returns: 深刻度順に並んだ指摘
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    names = dict(staff_name_map or {})
    states = states_from_grid(grid, staff, slots)
    out: list[LiveIssue] = []

    def who(sid: str) -> str:
        return f"{sid} {names.get(sid, '')}".strip()

    # --- 職員ごとの契約・希望・労働基準の判定 -------------------------------
    for member in staff:
        sid = member.staff_id
        row = states.get(sid, [CellState.OFF] * len(slots))
        contract = member.contract
        if day in fac.closed_days and any(s is not CellState.OFF for s in row):
            out.append(
                LiveIssue(
                    ERROR,
                    "CLOSED_DAY_WORK",
                    f"{who(sid)} は休園日に勤務しています。",
                    sid,
                    "",
                )
            )
        if not contract.can_work_holiday and day.weekday() >= 5:
            worked = [slots[i].label for i, s in enumerate(row) if s is CellState.WORK]
            if worked:
                out.append(
                    LiveIssue(
                        ERROR,
                        "HOLIDAY_NOT_ALLOWED",
                        f"{who(sid)} は休日勤務不可の契約です"
                        f"（{', '.join(worked)} が勤務になっています）。",
                        sid,
                        worked[0],
                    )
                )
        work_minutes = sum(slots[i].minutes for i, s in enumerate(row) if s is CellState.WORK)
        break_minutes = sum(slots[i].minutes for i, s in enumerate(row) if s is CellState.BREAK)
        cap = _daily_cap_minutes(contract)
        if work_minutes > cap:
            out.append(
                LiveIssue(
                    WARNING,
                    "DAILY_CAP_EXCEEDED",
                    f"{who(sid)} の1日の勤務が {work_minutes / 60:.1f} 時間に"
                    f"上限（{cap / 60:.1f} 時間）を超えています。",
                    sid,
                    "",
                )
            )
        elif work_minutes > int(round(contract.daily_hours * 60)):
            out.append(
                LiveIssue(
                    WARNING,
                    "DAILY_CONTRACT_EXCEEDED",
                    f"{who(sid)} の1日の勤務が {work_minutes / 60:.1f} 時間に"
                    f"上回りしています（契約 {contract.daily_hours:.1f} 時間）。",
                    sid,
                    "",
                )
            )
        required_break = _required_break_minutes(work_minutes)
        if work_minutes > 0 and break_minutes < required_break:
            out.append(
                LiveIssue(
                    WARNING,
                    "BREAK_INSUFFICIENT",
                    f"{who(sid)} は {work_minutes / 60:.1f} 時間勤務しますが"
                    f"休憩が {break_minutes} 分です（最低 {required_break} 分必要）。",
                    sid,
                    "",
                )
            )
        blocks = block_indices(row)
        if len(blocks) > 1:
            out.append(
                LiveIssue(
                    WARNING,
                    "SPLIT_SHIFT",
                    f"{who(sid)} の勤務が {len(blocks)} ブロックに分かれています"
                    "（勤務→休憩→勤務の形）。",
                    sid,
                    "",
                )
            )
        duty_indices = [i for i, s in enumerate(row) if s is not CellState.OFF]
        if duty_indices and not _is_contiguous(duty_indices):
            out.append(
                LiveIssue(
                    WARNING,
                    "DUTY_DISCONTIGUOUS",
                    f"{who(sid)} の在勤が連続していません（勤務の合間にオフが入っています）。",
                    sid,
                    "",
                )
            )
        entry = prefs.get(sid)
        if entry is not None:
            for i, slot in enumerate(slots):
                if row[i] is CellState.WORK and entry.is_unavailable(day, slot):
                    out.append(
                        LiveIssue(
                            ERROR,
                            "UNAVAILABLE_WORK",
                            f"{who(sid)} はこの時間帯に出勤不可です（希望休）。",
                            sid,
                            slot.label,
                        )
                    )
            if entry.avoid_early and any(
                i in _early_indices(slots, standard)
                for i, s in enumerate(row)
                if s is CellState.WORK
            ):
                out.append(
                    LiveIssue(
                        WARNING,
                        "AVOID_EARLY",
                        f"{who(sid)} は早番を希望しない設定です。",
                        sid,
                        "",
                    )
                )
        for i, slot in enumerate(slots):
            if row[i] is not CellState.WORK:
                continue
            if slot.start_minutes < to_minutes(contract.earliest_start):
                out.append(
                    LiveIssue(
                        ERROR,
                        "BEFORE_EARLIEST_START",
                        f"{who(sid)} は最早始業 "
                        f"{contract.earliest_start.strftime('%H:%M')} より前の "
                        f"{slot.label} に勤務しています。",
                        sid,
                        slot.label,
                    )
                )
            if slot.end_minutes > to_minutes(contract.latest_end):
                out.append(
                    LiveIssue(
                        ERROR,
                        "AFTER_LATEST_END",
                        f"{who(sid)} は最遅終業 "
                        f"{contract.latest_end.strftime('%H:%M')} より後の "
                        f"{slot.label} に勤務しています。",
                        sid,
                        slot.label,
                    )
                )

    # --- 時間帯ごとの配置基準の判定 ----------------------------------------
    for slot in slots:
        rows = [r for r in requirements.for_day(day) if r.slot == slot and r.is_binding]
        if not rows:
            continue
        need_all = sum(r.needed_staff for r in rows)
        need_q = sum(r.needed_qualified for r in rows)
        actual = sum(
            1
            for member in staff
            for i, s in enumerate(states.get(member.staff_id, []))
            if slots[i] == slot and s is CellState.WORK
        )
        if actual < need_all:
            out.append(
                LiveIssue(
                    ERROR,
                    "SHORTFALL_STAFF",
                    f"{slot.label} の配置が {need_all - actual} 名不足です"
                    f"（必要 {need_all} 名 / 配置 {actual} 名）。",
                    "",
                    slot.label,
                )
            )
        if need_q > 0:
            working_qualified = [
                member
                for member in staff
                if any(
                    slots[i] == slot and s is CellState.WORK
                    for i, s in enumerate(states.get(member.staff_id, []))
                )
            ]
            actual_q = qualified_count(working_qualified, standard)
            if actual_q < need_q:
                out.append(
                    LiveIssue(
                        ERROR,
                        "SHORTFALL_QUALIFIED",
                        f"{slot.label} の保育士が {need_q - actual_q} 名不足です"
                        f"（必要 {need_q} 名 / 配置 {actual_q} 名）。",
                        "",
                        slot.label,
                    )
                )
    return LiveReport(day=day, issues=tuple(out))


def _daily_cap_minutes(contract: Any) -> int:
    """その職員が1日に置ける勤務分の上限（法定10時間を超えない）。"""
    base = int(round(contract.daily_hours * 60))
    if not contract.overtime_allowed:
        return min(base, 600)
    return min(int(base * 1.25), 600)


def _early_indices(slots: Sequence[Slot], standard: StaffingStandard | None) -> set[int]:
    """早朝保育時間帯の添字集合（基準が無くても動くよう境界時刻で判定）。"""
    if standard is None:
        return set()
    boundary = to_minutes(standard.early_care_window[0])
    return {i for i, slot in enumerate(slots) if slot.start_minutes <= boundary}
