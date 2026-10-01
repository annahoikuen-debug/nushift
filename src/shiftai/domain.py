"""shiftai の共通ドメインモデル（このファイルは全モジュールが依存する「契約」です）。

このファイルだけは各サブエージェントが変更しません。変更が必要な場合は
このファイルに型を追加する形で行い、他モジュール側をそれに合わせてください。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from enum import Enum

# ---------------------------------------------------------------------------
# 列挙型
# ---------------------------------------------------------------------------


class AgeClass(str, Enum):
    """園児の年齢クラス。"""

    INFANT = "0歳児"
    AGE_1 = "1歳児"
    AGE_2 = "2歳児"
    AGE_3 = "3歳児"
    AGE_4 = "4歳児"
    AGE_5 = "5歳児以上"

    @property
    def years(self) -> int:
        return {
            AgeClass.INFANT: 0,
            AgeClass.AGE_1: 1,
            AgeClass.AGE_2: 2,
            AgeClass.AGE_3: 3,
            AgeClass.AGE_4: 4,
            AgeClass.AGE_5: 5,
        }[self]

    @property
    def sort_key(self) -> int:
        return self.years

    @classmethod
    def from_years(cls, years: int) -> AgeClass:
        return {
            0: cls.INFANT,
            1: cls.AGE_1,
            2: cls.AGE_2,
            3: cls.AGE_3,
            4: cls.AGE_4,
        }.get(years, cls.AGE_5)


class Role(str, Enum):
    """職員の職種。基準ごとに換算の扱いが異なる。"""

    HOIKUSHI = "保育士"
    SHIENSHIIN = "子育て支援員"
    YOUCHUIN = "幼稚園教諭"
    KANGSHI = "看護師"
    EIYOU = "栄養教諭"
    CHUUBOU = "調理員"
    YAKUARIN = "薬剤師"
    ENJOGAKUIN = "園長・主任（配置対象外）"


class EmploymentType(str, Enum):
    SEI = "正職員"
    PART = "パート"
    UKEIYOU = "契約社員"
    BUNKIN = "アルバイト"


COST_COEFFICIENT: dict[str, float] = {
    EmploymentType.SEI.value: 1.25,
    EmploymentType.UKEIYOU.value: 1.15,
    EmploymentType.PART.value: 1.0,
    EmploymentType.BUNKIN.value: 1.0,
}
"""雇用形態ごとの人件費係数。

人件費の概算は UI 指標（``gap_analysis.compute_cost``）と給与 CSV
（``exporter.payroll_dataframe``）の双方から使う。定義を二重に持つと
同じ人件費が画面と CSV で食い違うため、ここに一箇所だけ置く。
"""


def cost_coefficient(employment: object) -> float:
    """雇用形態に対応する人件費係数を返す（未知の種別は 1.0）。"""
    value = getattr(employment, "value", employment)
    return COST_COEFFICIENT.get(str(value), 1.0)


class SlotKind(str, Enum):
    """時間帯の性質。延長緩和措置などの判定に使う。"""

    NORMAL = "通常保育"
    STANDARD_TIME = "保育標準時間"
    EARLY = "早朝保育"
    LATE = "延長保育"
    LATE_STRICT = "延長保育（緩和措置なし）"


class CellState(str, Enum):
    WORK = "勤務"
    BREAK = "休憩"
    OFF = "オフ"


class ViolationSeverity(str, Enum):
    BLOCKER = "法令違反"
    WARNING = "要調整"
    INFO = "参考"


class SolveStatus(str, Enum):
    OPTIMAL = "最適解"
    FEASIBLE = "実行可能解"
    INFEASIBLE = "解なし"
    PARTIAL = "部分的なシフト"
    ERROR = "エラー"


# ---------------------------------------------------------------------------
# 時間帯
# ---------------------------------------------------------------------------


def to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def to_time(minutes: int) -> time:
    minutes %= 24 * 60
    return time(hour=minutes // 60, minute=minutes % 60)


@dataclass(frozen=True, order=True)
class Slot:
    """一つの作業時間帯（既定は30分）。

    終端の ``00:00`` は「24時」を意味する番兵として扱う。深夜に閉所する園で
    最後の時間帯が 23:30〜24:00 になる場合に対応するため。
    """

    start: time
    end: time

    def __post_init__(self) -> None:
        if self.end == time(0, 0) and to_minutes(self.start) > 0:
            return
        if self.end <= self.start:
            raise ValueError(f"Slot の end は start より後にしてください: {self!r}")

    @property
    def minutes(self) -> int:
        return self.end_minutes - self.start_minutes

    @property
    def hours(self) -> float:
        return self.minutes / 60.0

    @property
    def start_minutes(self) -> int:
        return to_minutes(self.start)

    @property
    def end_minutes(self) -> int:
        """終端の分。終日（00:00 表記）は 1440 とする。"""
        minutes = to_minutes(self.end)
        if minutes == 0 and self.start_minutes > 0:
            return 24 * 60
        return minutes

    @property
    def is_midnight_end(self) -> bool:
        """終端が 00:00 表記（24時）かどうか。"""
        return to_minutes(self.end) == 0

    @property
    def label(self) -> str:
        if self.is_midnight_end:
            return f"{self.start.strftime('%H:%M')}-24:00"
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"

    def overlaps(self, start: time, end: time) -> bool:
        """[start, end) と重なるか（境界は重なりとみなさない）。"""
        if self.is_midnight_end:
            # [start_minutes, 1440) として比較する
            return to_minutes(end) > self.start_minutes and to_minutes(start) < 24 * 60
        return self.start < end and start < self.end

    def contains(self, start: time, end: time) -> bool:
        """時間帯が [start, end) を完全に覆うか。"""
        if self.is_midnight_end:
            stop = to_minutes(end) or 24 * 60
            return self.start_minutes <= to_minutes(start) and stop <= 24 * 60
        return self.start <= start and end <= self.end

    def shifted(self, delta_minutes: int) -> Slot:
        base = self.start_minutes + delta_minutes
        return Slot(to_time(base), to_time(base + self.minutes))


def build_slots(
    day_open: time,
    day_close: time,
    granularity_min: int = 30,
    *,
    strict: bool = False,
) -> tuple[Slot, ...]:
    """開所〜閉所の間の時間帯を等分割して生成する。

    開所・閉所が粒度に合わない場合（例: 7:15 開所、19:30 閉所）は、
    strict=False のときは開始は粒度境界まで切り下げ、終了は切り上げる。
    strict=True では ValueError を送出する。
    """
    if granularity_min <= 0:
        raise ValueError("granularity_min は正の数である必要があります")
    start, end = to_minutes(day_open), to_minutes(day_close)
    if end <= start:
        raise ValueError("day_close は day_open より後にしてください")
    if (end - start) % granularity_min != 0:
        if strict:
            raise ValueError(
                f"利用時間 {end - start} 分は granularity_min={granularity_min} で割り切れません"
            )
        start = (start // granularity_min) * granularity_min
        end = math.ceil(end / granularity_min) * granularity_min
        if end <= start:
            end = start + granularity_min
    slots = []
    cur = start
    while cur < end:
        slots.append(Slot(to_time(cur), to_time(cur + granularity_min)))
        cur += granularity_min
    return tuple(slots)


def overlapping_slots(slots: Sequence[Slot], start: time, end: time) -> list[int]:
    """[start, end) と重なる時間帯のインデックス一覧を返す。"""
    return [i for i, s in enumerate(slots) if s.overlaps(start, end)]


# ---------------------------------------------------------------------------
# 園児（登降園予定）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildPlan:
    """保護者からの登降園予定1件（1日分）。実績取り込みも同じ型で表現する。"""

    child_id: str
    name: str
    day: date
    age_class: AgeClass
    arrive: time
    depart: time
    is_short_time: bool = False
    """短時間保育（保育標準時間のみ）園児か。"""
    absent: bool = False
    absent_reason: str = ""
    uses_early_care: bool = False
    """早朝保育（登園前の預かり）を利用するか。"""
    uses_late_care: bool = False
    """延長保育（降園後の預かり）を利用するか。"""
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.absent and to_minutes(self.depart) <= to_minutes(self.arrive):
            raise ValueError(f"在園時間が不正です: {self.child_id} {self.arrive}-{self.depart}")

    @property
    def stay_minutes(self) -> int:
        if self.absent:
            return 0
        return to_minutes(self.depart) - to_minutes(self.arrive)

    @property
    def stay_hours(self) -> float:
        return self.stay_minutes / 60.0

    def effective_arrive(self, settings_early: tuple[time, time] | None = None) -> time:
        """早朝保育利用時のみ実登園時刻を早める。"""
        if self.uses_early_care and settings_early is not None:
            return min(self.arrive, settings_early[0])
        return self.arrive

    def effective_depart(self, settings_late: tuple[time, time] | None = None) -> time:
        """延長保育利用時のみ実降園時刻を遅らせる。"""
        if self.uses_late_care and settings_late is not None:
            return max(self.depart, settings_late[1])
        return self.depart


# ---------------------------------------------------------------------------
# 職員
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    """勤務条件（契約）。"""

    weekly_hours: float
    daily_hours: float
    employment_type: EmploymentType = EmploymentType.PART
    min_monthly_hours: float = 0.0
    max_monthly_hours: float = 200.0
    max_weekly_days: int = 5
    """週あたりの最大出勤日数。``0`` は「週あたりの上限なし」を意味する。"""
    max_consecutive_days: int = 5
    """最大連続勤務日数。``0`` は「上限なし」を意味する。"""
    min_rest_hours: float = 11.0
    """勤務間の最低休息時間（労働基準法第9条に対応）。"""
    granularity_min: int = 30
    earliest_start: time = time(6, 0)
    latest_end: time = time(22, 0)
    can_work_holiday: bool = True
    overtime_allowed: bool = True

    def __post_init__(self) -> None:
        if self.daily_hours <= 0 or self.weekly_hours <= 0:
            raise ValueError("契約時間は正である必要があります")
        if self.max_monthly_hours < self.min_monthly_hours:
            raise ValueError("max_monthly_hours は min_monthly_hours 以上にしてください")
        if self.max_weekly_days < 0 or self.max_consecutive_days < 0:
            raise ValueError(
                "max_weekly_days と max_consecutive_days は 0 以上にしてください"
                "（0 は上限なしを意味します）"
            )


@dataclass(frozen=True)
class StaffMember:
    """職員1名。"""

    staff_id: str
    name: str
    roles: tuple[Role, ...]
    contract: Contract
    skills: frozenset[str] = frozenset()
    """例: '乳幼児研修修了', '応急処置資格', 'ピアノ指導可'"""
    home_ward: str = ""
    memo: str = ""

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError(f"{self.staff_id}: roles が空です")

    @property
    def primary_role(self) -> Role:
        return self.roles[0]

    def has_role(self, role: Role) -> bool:
        return role in self.roles

    @property
    def is_qualified(self) -> bool:
        """保育士資格を持つか（限定的な時間帯の可否判定に使う）。"""
        return self.has_role(Role.HOIKUSHI)

    @property
    def is_placeable(self) -> bool:
        """保育室への配置対象か。

        園長・主任（``Role.ENJOGAKUIN``）は保育基準の人数に計上しない。
        副資格に保育士がある場合は配置**可能**とみなす（保育室の補助ができるため）。
        """
        if not self.has_role(Role.ENJOGAKUIN):
            return True
        return self.has_role(Role.HOIKUSHI)


@dataclass(frozen=True)
class Unavailability:
    """希望休・不在時間帯。"""

    day: date
    start: time = time(0, 0)
    end: time = time(23, 59)
    reason: str = ""


@dataclass
class StaffPreferences:
    """個人希望（ソフト制約）。希望休のみハード制約として扱う。"""

    unavailable: list[Unavailability] = field(default_factory=list)
    preferred_days: frozenset[date] = frozenset()
    preferred_off_days: frozenset[date] = frozenset()
    preferred_slots: dict[date, tuple[Slot, ...]] = field(default_factory=dict)
    avoid_early: bool = False
    avoid_late: bool = False
    max_early_shifts: int = 4
    max_late_shifts: int = 4
    notes: str = ""

    def is_unavailable(self, day: date, slot: Slot) -> bool:
        return any(
            u.day == day and slot.overlaps(u.start, u.end) for u in self.unavailable
        )

    def unavailable_days(self) -> set[date]:
        return {u.day for u in self.unavailable}

    def preferred_slots_for(self, day: date) -> tuple[Slot, ...]:
        return self.preferred_slots.get(day, ())


# ---------------------------------------------------------------------------
# 配置基準
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgeRatio:
    """年齢クラスごとの定員基準。"""

    age_class: AgeClass
    children_per_staff: float
    """例: 0歳児=3.0, 1・2歳児=6.0, 3歳児=8.0, 4・5歳児=20.0"""
    rounding: str = "ceil"
    """'ceil' | 'floor' | 'round'。未満にならないよう原則 'ceil'。"""


@dataclass(frozen=True)
class StaffingStandard:
    """一園の配置基準。自治体プリセットに園ごとの上乗せを重ねる。"""

    name: str
    ratios: Mapping[AgeClass, AgeRatio]
    min_staff_per_room: int = 2
    """在園児がいる保育室の最低配置人数（2名ルール）。"""
    min_qualified_ratio: float = 0.5
    """配置人員に占める保育士の最低割合。"""
    break_minutes: int = 60
    """職員1人あたりの休憩時間（労働基準法第9条）。"""
    break_stagger_minutes: int = 30
    """休憩者の重複を避けるための時間ずれ。"""
    work_start_base: time = time(8, 30)
    standard_time: tuple[time, time] = (time(8, 30), time(17, 15))
    """保育標準時間。"""
    early_care_window: tuple[time, time] = (time(7, 15), time(8, 30))
    """早朝保育の時間帯。"""
    late_care_window: tuple[time, time] = (time(17, 15), time(19, 30))
    """延長保育の時間帯。"""
    late_care_relaxed: bool = True
    """延長時に「保育士1名＋他資格者」で代替できるとする（Fukuoka型）。"""
    late_care_min_qualified: int = 1
    """延長時の最低保育士数。1なら支援員の併記が許容される。"""
    late_care_after_relax_time: time | None = time(18, 30)
    """この時刻以降は緩和措置なしで保育士のみとする境界。Noneで常に緩和。"""
    is_short_time_only: bool = False
    """短時間保育（保育時間11時間）園のみ。"""
    remarks: str = ""

    def ratio_for(self, age_class: AgeClass) -> AgeRatio:
        ratio = self.ratios.get(age_class)
        if ratio is None:
            raise KeyError(f"{self.name}: {age_class.value} の定員比が定義されていません")
        return ratio

    def headcount_for(self, age_class: AgeClass, child_count: int) -> int:
        """在園児数から必要な換算人員を返す。"""
        if child_count <= 0:
            return 0
        ratio = self.ratio_for(age_class)
        q = child_count / ratio.children_per_staff
        if ratio.rounding == "floor":
            return int(math.floor(q))
        if ratio.rounding == "round":
            return max(1, int(math.floor(q + 0.5)))
        return int(math.ceil(q))

    def is_standard_time(self, slot: Slot) -> bool:
        return self.standard_time[0] <= slot.start and slot.end <= self.standard_time[1]

    def slot_kind(self, slot: Slot) -> SlotKind:
        if slot.overlaps(*self.early_care_window):
            return SlotKind.EARLY
        if slot.overlaps(*self.late_care_window):
            if (
                self.late_care_after_relax_time is not None
                and slot.start >= self.late_care_after_relax_time
            ):
                return SlotKind.LATE_STRICT
            return SlotKind.LATE
        if self.is_standard_time(slot):
            return SlotKind.STANDARD_TIME
        return SlotKind.NORMAL

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ratios": {
                k.value: {
                    "children_per_staff": v.children_per_staff,
                    "rounding": v.rounding,
                }
                for k, v in self.ratios.items()
            },
            "min_staff_per_room": self.min_staff_per_room,
            "min_qualified_ratio": self.min_qualified_ratio,
            "break_minutes": self.break_minutes,
            "standard_time": [
                self.standard_time[0].strftime("%H:%M"),
                self.standard_time[1].strftime("%H:%M"),
            ],
            "early_care_window": [
                self.early_care_window[0].strftime("%H:%M"),
                self.early_care_window[1].strftime("%H:%M"),
            ],
            "late_care_window": [
                self.late_care_window[0].strftime("%H:%M"),
                self.late_care_window[1].strftime("%H:%M"),
            ],
            "late_care_relaxed": self.late_care_relaxed,
            "late_care_min_qualified": self.late_care_min_qualified,
            "is_short_time_only": self.is_short_time_only,
            "remarks": self.remarks,
        }


# ---------------------------------------------------------------------------
# 必要人員（配置基準エンジンの出力）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Requirement:
    """1日・1時間帯・1年齢クラスの必要人員。"""

    day: date
    slot: Slot
    age_class: AgeClass
    child_count: int
    needed_staff: int
    """必要な換算人員（支援員を含む）。"""
    needed_qualified: int
    """そのうち保育士でなければならない人数。"""
    slot_kind: SlotKind = SlotKind.NORMAL
    basis: str = ""
    """どの基準から導いたかの説明（UI で開示）。"""
    is_binding: bool = True
    """True なら必ず満たすべき必須要件。False は超過配置のみ許容。"""

    @property
    def is_shortfall_critical(self) -> bool:
        return self.is_binding and self.child_count > 0


@dataclass
class RequirementTable:
    """全日×全時間帯分の必要人員。"""

    day_open: time
    day_close: time
    granularity_min: int
    slots: tuple[Slot, ...]
    rows: dict[date, list[Requirement]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def for_day(self, day: date) -> list[Requirement]:
        return self.rows.get(day, [])

    def all_days(self) -> list[date]:
        return sorted(self.rows)

    def day_slots(self) -> tuple[Slot, ...]:
        return self.slots

    def needed_staff(self, day: date, slot: Slot) -> int:
        return sum(r.needed_staff for r in self.for_day(day) if r.slot == slot)

    def needed_qualified(self, day: date, slot: Slot) -> int:
        return sum(r.needed_qualified for r in self.for_day(day) if r.slot == slot)

    def total_needed_hours(self) -> float:
        return sum(r.needed_staff * r.slot.hours for rows in self.rows.values() for r in rows)

    def by_age_class(self, age_class: AgeClass) -> list[Requirement]:
        return [r for rows in self.rows.values() for r in rows if r.age_class == age_class]

    def all_requirements(self) -> list[Requirement]:
        return [r for rows in self.rows.values() for r in rows]

    def to_long_dataframe(self):
        import pandas as pd

        records = [
            {
                "日付": r.day.isoformat(),
                "時間帯": r.slot.label,
                "開始": r.slot.start.strftime("%H:%M"),
                "終了": r.slot.end.strftime("%H:%M"),
                "年齢クラス": r.age_class.value,
                "在園児数": r.child_count,
                "必要人員": r.needed_staff,
                "必要保育士数": r.needed_qualified,
                "時間帯区分": r.slot_kind.value,
                "根拠": r.basis,
                "必須": r.is_binding,
            }
            for r in sorted(
                self.all_requirements(),
                key=lambda x: (x.day, x.slot.start, x.age_class.sort_key),
            )
        ]
        return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# シフト
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShiftAssignment:
    """職員1名×1日×1時間帯の状態。"""

    staff_id: str
    day: date
    slot: Slot
    state: CellState

    @property
    def is_work(self) -> bool:
        return self.state is CellState.WORK

    @property
    def is_break(self) -> bool:
        return self.state is CellState.BREAK

    @property
    def key(self) -> tuple[str, date, str]:
        return (self.staff_id, self.day, self.slot.label)


@dataclass
class ShiftDay:
    """1日分のシフト（職員×時間帯のマトリクス）。"""

    day: date
    assignments: dict[str, dict[str, CellState]] = field(default_factory=dict)

    def get(self, staff_id: str, slot: Slot) -> CellState:
        return self.assignments.get(staff_id, {}).get(slot.label, CellState.OFF)

    def set(self, staff_id: str, slot: Slot, state: CellState) -> None:
        self.assignments.setdefault(staff_id, {})[slot.label] = state

    def working(self, slots: Sequence[Slot]) -> dict[str, list[Slot]]:
        """職員ID → 勤務中時間帯の連続ブロック。"""
        out: dict[str, list[Slot]] = {}
        for staff_id in self.assignments:
            worked = [s for s in slots if self.get(staff_id, s) is CellState.WORK]
            if worked:
                out[staff_id] = worked
        return out

    def working_slots_by_staff(self, slots: Sequence[Slot]) -> dict[str, set[Slot]]:
        out: dict[str, set[Slot]] = {}
        for staff_id in self.assignments:
            worked = {s for s in slots if self.get(staff_id, s) is CellState.WORK}
            if worked:
                out[staff_id] = worked
        return out

    def to_frame(self, slots: Sequence[Slot], staff_ids: Sequence[str]):
        import pandas as pd

        records = []
        for sid in staff_ids:
            row: dict[str, object] = {"職員ID": sid}
            for s in slots:
                row[s.label] = self.get(sid, s).value
            records.append(row)
        return pd.DataFrame.from_records(records)


@dataclass
class Violation:
    severity: ViolationSeverity
    code: str
    message: str
    day: date | None = None
    slot: Slot | None = None
    staff_id: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "深刻度": self.severity.value,
            "コード": self.code,
            "内容": self.message,
            "日付": self.day.isoformat() if self.day else "",
            "時間帯": self.slot.label if self.slot else "",
            "職員ID": self.staff_id or "",
        }


@dataclass
class SolveResult:
    """シフト最適化の結果。"""

    status: SolveStatus
    shift_days: list[ShiftDay] = field(default_factory=list)
    assignments: list[ShiftAssignment] = field(default_factory=list)
    objective_value: float | None = None
    violations: list[Violation] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    """所要時間・変数数・制約数など。"""

    @property
    def ok(self) -> bool:
        return self.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)

    def day(self, day: date) -> ShiftDay | None:
        for sd in self.shift_days:
            if sd.day == day:
                return sd
        return None

    def blockers(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is ViolationSeverity.BLOCKER]

    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is ViolationSeverity.WARNING]


@dataclass
class ObjectiveWeights:
    """目的関数の重み。大きいほど優先される。"""

    shortfall_penalty: float = 1000.0
    overstaff_penalty: float = 1.0
    preference_miss_penalty: float = 5.0
    preference_match_bonus: float = 3.0
    early_shift_penalty: float = 2.0
    late_shift_penalty: float = 2.0
    break_conflict_penalty: float = 50.0
    consecutive_day_penalty: float = 8.0
    hours_imbalance_penalty: float = 4.0
    fairness_early_penalty: float = 4.0
    """早番回数の最大と最小の差（週レンジ）を縮めるペナルティ。"""
    fairness_late_penalty: float = 4.0
    """遅番回数の上限と最小の差（週レンジ）を縮めるペナルティ。"""
    fairness_saturday_penalty: float = 4.0
    """土曜出勤日数の上限と最小の差（週レンジ）を縮めるペナルティ。"""
    unused_staff_penalty: float = 0.5
    monthly_hours_penalty: float = 3.0
    rest_violation_penalty: float = 40.0
    max_shift_length_penalty: float = 6.0
    """1勤務あたりの長時間超過（1日9時間・週44時間の内部目安など）。"""


# ---------------------------------------------------------------------------
# 園設定
# ---------------------------------------------------------------------------


@dataclass
class FacilitySettings:
    """園の開設条件。UI の初期値とプリセットに使う。"""

    facility_name: str = "あさひ保育園"
    day_open: time = time(7, 15)
    day_close: time = time(19, 30)
    granularity_min: int = 30
    closed_days: frozenset[date] = frozenset()
    holiday_dates: frozenset[date] = frozenset()
    rooms: tuple[str, ...] = ("0・1歳児室", "2・3歳児室", "4・5歳児室")
    labor_cost_per_hour: float = 1500.0
    """人件費目安（1時間・パート係数）。"""


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------


def daterange(start: date, end: date):
    """start と end を含む日次イテレータ。"""
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


WEEKLY_WINDOW_DAYS = 7
"""週の判定に使う窓の日数。"""


def weekly_windows(
    days: Sequence[date], size: int = WEEKLY_WINDOW_DAYS
) -> list[list[date]]:
    """連続する size 日ごとの窓を返す。末尾も重複する窓で覆う。

    週の上限は「任意の連続した 1 週間の勤務量」で判定するため、
    週の区切りで切らず 1 日ずつずらした窓を返す。計画期間が size 日未満の
    ときは全体を 1 つの窓として扱う。1 週間未満の計画は合計で判定する。
    """
    ordered = sorted(days)
    if not ordered:
        return []
    if len(ordered) <= size:
        return [ordered]
    return [ordered[i : i + size] for i in range(len(ordered) - size + 1)]


def weekly_periods(
    days: Sequence[date], size: int = WEEKLY_WINDOW_DAYS
) -> list[list[date]]:
    """``days`` を size 日ずつに区切った窓を返す（窓どうしは重複しない）。

    出勤可能日数のように「合計を数える」用途では、重ねた窓を足すと
    同じ日を重複して数えてしまうためこちらを使う。
    """
    ordered = sorted(days)
    if not ordered:
        return []
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def is_workday(day: date, settings: FacilitySettings) -> bool:
    return day not in settings.closed_days


def japanese_weekday(day: date) -> str:
    return "月火水木金土日"[day.weekday()]


def format_jp_date(day: date) -> str:
    return f"{day.month}/{day.day}({japanese_weekday(day)})"


def format_jp_date_full(day: date) -> str:
    return f"{day.year}年{day.month}月{day.day}日({japanese_weekday(day)})"
