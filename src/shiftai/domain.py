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

from shiftai import config

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
    granularity_min: int = config.DEFAULT_GRANULARITY_MIN,
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
    min_rest_hours: float = float(config.STATUTORY_MIN_REST_HOURS)
    """勤務間の最低休息時間（労働基準法第9条に対応）。"""
    granularity_min: int = config.DEFAULT_GRANULARITY_MIN
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
    def is_nurse(self) -> bool:
        """看護師・准看護師の資格を持つか（みなし保育士の上限を数えるために使う）。"""
        return self.has_role(Role.KANGSHI)

    def is_qualified_under(self, standard: StaffingStandard | None) -> bool:
        """この基準で「必要保育士数」を満たす資格を持つか。

        看護師・准看護師が「必要保育士数」の分子に入る基準は2つあり、
        別々の制度に由来するため区別する。

        * ``nurse_as_qualified_cap`` が 1 以上（企業主導型保育事業）:
          実施要綱 第3の2(4)② の「みなし保育士」。**1人に限り**数えられる。
          上限の人数は :func:`qualified_count` で数える。
        * ``qualified_extra_roles`` に看護師を含む（認可外保育施設指導監督基準）:
          第1(2) の「3分の1以上は保育士、准看護師又は看護師」で、
          人数の上限なく比率の分子に入る。
        """
        if self.has_role(Role.HOIKUSHI):
            return True
        if standard is None:
            return False
        if set(self.roles) & standard.qualified_extra_roles:
            return True
        return standard.nurse_as_qualified_cap > 0 and self.is_nurse

    def counts_toward_headcount(self) -> bool:
        """保育基準の配置人数に数えてよい職種か（``is_placeable`` と同じ判定）。"""
        return self.is_placeable

    @property
    def is_placeable(self) -> bool:
        """保育室への配置対象か。

        次の職種は**保育基準の配置人数に計上しない**。

        * 調理員・栄養教諭・薬剤師: 保育に従事しないため。
          「企業主導型保育事業費補助金実施要綱」第3の2(4)① は
          「保育従事者**、**嘱託医**及び**調理員を置かなければならない」と定め、
          調理員は保育従事者と**別枠**の必置職員である
          （調理業務の全部委託、または他施設から食事の搬入をする場合は置かなくてよい）。
          認可保育所でも調理員は「保育を担任する職員」には数えない。
        * 園長・主任（``Role.ENJOGAKUIN``）: 副資格に保育士がある場合は
          配置**可能**とみなす（保育室の補助ができるため）。

        副資格に保育士を持てば、調理員・園長・主任であっても配置対象とする
        （保育室で補助ができるため）。
        """
        if self.has_role(Role.HOIKUSHI):
            return True
        if set(self.roles) & NON_PLACING_ROLES:
            return False
        return not self.has_role(Role.ENJOGAKUIN)


def qualified_count(members: Sequence[StaffMember], standard: StaffingStandard | None) -> int:
    """「必要保育士数」を満たす人数を数える。

    * ``nurse_as_qualified_cap`` が 0 かつ ``qualified_extra_roles`` が空
      （認可保育所）: 保育士だけ数える。
    * ``nurse_as_qualified_cap`` が 1 以上（企業主導型保育事業）:
      看護師・准看護師を**上限まで**保育士とみなす（みなし保育士）。
    * ``qualified_extra_roles`` に看護師を含む（認可外保育施設指導監督基準）:
      看護師・准看護師を人数の上限なく数える（第1(2) の3分の1以上）。
    """
    hoikushi = sum(1 for m in members if m.has_role(Role.HOIKUSHI))
    if standard is None:
        return hoikushi
    extra = sum(1 for m in members if set(m.roles) & standard.qualified_extra_roles)
    if extra:
        return hoikushi + extra
    if standard.nurse_as_qualified_cap <= 0:
        return hoikushi
    nurses = sum(1 for m in members if m.is_nurse)
    return hoikushi + min(nurses, standard.nurse_as_qualified_cap)


def qualified_ids(members: Sequence[StaffMember], standard: StaffingStandard | None) -> set[str]:
    """:func:`qualified_count` に数えられる職員の ID 集合を返す。

    看護師が上限を超える場合は職員IDの昇順で上限まで採用する
    （どの職員を数えるかが一意に決まるようにするため）。
    """
    if standard is None or (
        standard.nurse_as_qualified_cap <= 0 and not standard.qualified_extra_roles
    ):
        return {m.staff_id for m in members if m.has_role(Role.HOIKUSHI)}
    ids = {m.staff_id for m in members if m.has_role(Role.HOIKUSHI)}
    nurses = sorted(m.staff_id for m in members if m.is_nurse)
    if standard.qualified_extra_roles:
        ids.update(nurses)
    else:
        ids.update(nurses[: standard.nurse_as_qualified_cap])
    return ids


@dataclass(frozen=True)
class Unavailability:
    """希望休・不在時間帯。"""

    day: date
    start: time = time(0, 0)
    end: time = config.DAY_END
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
        return any(u.day == day and slot.overlaps(u.start, u.end) for u in self.unavailable)

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
    """'ceil' | 'floor' | 'round' | 'trunc1'。未満にならないよう原則 'ceil'。

    ``'trunc1'`` は小数第2位以下を切り捨てる（認可外保育施設の算出手法）。
    認可外では年齢区分ごとに切り捨てて**合算してから**丸めるため、
    単一年齢クラスに適用する意味はない。``StaffingStandard`` 側の
    ``headcount_mode`` が用它をまとめる。
    """


# ---------------------------------------------------------------------------
# 配置基準の算出手法・資格判定モード
# ---------------------------------------------------------------------------

HEADCOUNT_PER_CLASS = "per_class"
"""認可保育所（既定）。年齢クラスごとに定員比で割り上げる。"""

HEADCOUNT_FACILITY_FORMULA = "facility_formula"
"""認可外保育施設。年齢区分ごとに小数第2位以下を切り捨て→合算→+1→小数第1位で四捨五入。"""

HEADCOUNT_MODES: tuple[str, ...] = (HEADCOUNT_PER_CLASS, HEADCOUNT_FACILITY_FORMULA)

QUALIFIED_PER_CLASS = "per_class"
"""認可保育所（既定）。必要保育士数＝必要人員数（延長緩和以外）。"""

QUALIFIED_RATIO = "ratio"
"""認可外保育施設。必要人員に対する保育士比率で決める。"""

QUALIFIED_MODES: tuple[str, ...] = (QUALIFIED_PER_CLASS, QUALIFIED_RATIO)


def trunc1(value: float) -> float:
    """小数第2位以下を切り捨てる（認可外保育施設の算出手法で使う）。"""
    return math.floor(value * 10) / 10


def largest_remainder(total: int, weights: Sequence[float]) -> list[int]:
    """整数 ``total`` を ``weights`` に比例配分する（最大剰余法）。

    戻り値の合計は常に ``total`` になる。在園児がいる年齢クラスに 0 名しか
    配らないのは表示と実感がズレるため、重みの大きい順に 1 名ずつ先に配る。
    """
    count = len(weights)
    if count == 0 or total <= 0:
        return [0] * count
    order = sorted(range(count), key=lambda i: (-weights[i], i))
    out = [0] * count
    take = min(total, count)
    for i in order[:take]:
        out[i] = 1
    rest = total - take
    if rest <= 0:
        return out
    weight_sum = sum(weights)
    if weight_sum <= 0:
        for i in order[take:]:
            if rest <= 0:
                break
            out[i] += 1
            rest -= 1
        return out
    exact = [rest * w / weight_sum for w in weights]
    floors = [int(math.floor(e)) for e in exact]
    leftover = rest - sum(floors)
    ranked = sorted(range(count), key=lambda i: (-(exact[i] - floors[i]), i))
    for i in ranked[:leftover]:
        floors[i] += 1
    return [out[i] + floors[i] for i in range(count)]


#: 保育室への配置対象から外す職種（保育基準の配置人数に計上しない）。
#: 調理員は保育従事者と**別枠**の必置職員で、保健師・看護師・准看護師は
#: 保育従事者だが、基準によっては保育士とみなせる（みなし保育士）。
NON_PLACING_ROLES: frozenset[Role] = frozenset({Role.CHUUBOU, Role.EIYOU, Role.YAKUARIN})


@dataclass(frozen=True)
class StaffingStandard:
    """一園の配置基準。自治体プリセットに園ごとの上乗せを重ねる。"""

    name: str
    ratios: Mapping[AgeClass, AgeRatio]
    min_staff_per_room: int = 2
    """在園児がいる保育室の最低配置人数（2名ルール）。"""
    min_qualified_ratio: float = 0.5
    """配置人員に占める保育士の最低割合。"""
    break_minutes: int = config.DEFAULT_BREAK_MINUTES
    """職員1人あたりの休憩時間（労働基準法第9条）。"""
    break_stagger_minutes: int = config.DEFAULT_BREAK_STAGGER_MINUTES
    """休憩者の重複を避けるための時間ずれ。"""
    work_start_base: time = config.STANDARD_TIME_START
    standard_time: tuple[time, time] = (config.STANDARD_TIME_START, config.STANDARD_TIME_END)
    """保育標準時間。"""
    early_care_window: tuple[time, time] = (
        config.DEFAULT_EARLY_CARE_START,
        config.DEFAULT_EARLY_CARE_END,
    )
    """早朝保育の時間帯。"""
    late_care_window: tuple[time, time] = (
        config.DEFAULT_LATE_CARE_START,
        config.DEFAULT_LATE_CARE_END,
    )
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
    headcount_mode: str = HEADCOUNT_PER_CLASS
    """必要人員の算出手法。``per_class`` / ``facility_formula``。"""
    headcount_extra: int = 0
    """合計に加える定数。認可外保育施設は +1（実施要綱 第3の2(4)②）。"""
    age_groups: tuple[tuple[AgeClass, ...], ...] | None = None
    """定員比をまとめる年齢グループ。``None`` は年齢クラス毎に単独。

    認可外保育施設は 1・2歳児を合算、4歳以上児を合算する。
    例: ``((INFANT,), (AGE_1, AGE_2), (AGE_3,), (AGE_4, AGE_5))``
    """
    qualified_mode: str = QUALIFIED_PER_CLASS
    """必要保育士数の決め方。``per_class`` / ``ratio``。"""
    min_qualified_floor: int = 0
    """必要保育士数の下限。認可外保育施設は「最低2名のうち1名以上保育士」で 1。"""
    nurse_as_qualified_cap: int = 0
    """看護師・准看護師を保育士として数えられる上限（人/時間帯）。

    0 は数えられない。企業主導型保育事業（実施要綱 第3の2(4)②）は 1 人に限り
    保育士とみなすことができる（みなし保育士）。
    """
    qualified_extra_roles: frozenset[Role] = frozenset()
    """保育士に加えて「必要保育士数」を満たすとみなす職種（人数の上限なし）。

    認可外保育施設指導監督基準 第1(2) は
    「保育に従事する者の**おおむね3分の1以上**が保育士、准看護師又は
    看護師である」ことを求めるため、**看護師は比率の分子にそのまま入る**。
    企業主導型保育事業（実施要綱）の「みなし保育士（1人に限り）」とは別物なので、
    両方を同時に設定することはできない。
    """
    non_qualifying_roles: frozenset[Role] = frozenset()
    """この基準で「必要保育士数」を満たさないとみなす職種（認定外の範囲外）。"""

    def __post_init__(self) -> None:
        if self.headcount_mode not in HEADCOUNT_MODES:
            raise ValueError(
                f"headcount_mode が不正です: {self.headcount_mode!r}"
                f"（選択肢: {', '.join(HEADCOUNT_MODES)}）"
            )
        if self.qualified_mode not in QUALIFIED_MODES:
            raise ValueError(
                f"qualified_mode が不正です: {self.qualified_mode!r}"
                f"（選択肢: {', '.join(QUALIFIED_MODES)}）"
            )
        if self.headcount_extra < 0:
            raise ValueError("headcount_extra は 0 以上にしてください")
        if not 0.0 <= self.min_qualified_ratio <= 1.0:
            raise ValueError("min_qualified_ratio は 0.0〜1.0 の範囲で指定してください")
        if self.nurse_as_qualified_cap < 0:
            raise ValueError("nurse_as_qualified_cap は 0 以上にしてください")
        if self.nurse_as_qualified_cap > 0 and self.qualified_extra_roles:
            raise ValueError(
                "nurse_as_qualified_cap（みなし保育士の上限）と "
                "qualified_extra_roles（比率の分子に含める職種）は同時に指定できません"
            )

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
        if ratio.rounding == "trunc1":
            return int(trunc1(q))
        return int(math.ceil(q))

    # -- 認可外保育施設（施設単位の算出手法） -------------------------------

    def effective_age_groups(self) -> tuple[tuple[AgeClass, ...], ...]:
        """定員比をまとめる年齢グループを返す（未指定なら年齢クラス毎に単独）。"""
        if self.age_groups is not None:
            return tuple(tuple(group) for group in self.age_groups)
        return tuple((age_class,) for age_class in sorted(self.ratios, key=lambda a: a.sort_key))

    def headcount_breakdown(
        self, counts: Mapping[AgeClass, int]
    ) -> list[tuple[tuple[AgeClass, ...], int, float, float]]:
        """施設単位算出手法の内訳を返す。

        各要素は ``(年齢グループ, 園児数, 切り捨て後の配分, 定員比)``。
        園児が 0 人のグループは含めない。
        """
        out: list[tuple[tuple[AgeClass, ...], int, float, float]] = []
        for group in self.effective_age_groups():
            present = [ac for ac in group if counts.get(ac, 0) > 0]
            if not present:
                continue
            total = sum(counts[ac] for ac in present)
            ratio = self.ratio_for(min(present, key=lambda a: a.sort_key)).children_per_staff
            out.append(
                (
                    tuple(sorted(present, key=lambda a: a.sort_key)),
                    total,
                    trunc1(total / ratio),
                    ratio,
                )
            )
        return out

    def headcount_for_slot(self, counts: Mapping[AgeClass, int]) -> int:
        """1 時間帯の合計必要人員を返す。

        ``per_class`` は年齢クラス毎の ``headcount_for`` を合計する（従来どおり）。
        ``facility_formula`` は「区分ごとに小数第2位以下切捨 → 合算 → +headcount_extra
        → 小数第1位で四捨五入」で1つの整数にする。
        """
        if self.headcount_mode == HEADCOUNT_PER_CLASS:
            return sum(self.headcount_for(age_class, n) for age_class, n in counts.items() if n > 0)
        if sum(n for n in counts.values() if n > 0) <= 0:
            return 0
        raw = float(self.headcount_extra)
        for _group, _n, part, _ratio in self.headcount_breakdown(counts):
            raw += part
        return int(math.floor(raw + 0.5))

    def allocate_staff(self, counts: Mapping[AgeClass, int]) -> dict[AgeClass, int]:
        """合計必要人員を年齢クラスへ割り当てる（合計は ``headcount_for_slot`` と一致）。

        施設単位の算出手法では1つの整数が基準の答えだが、本ツールの
        :class:`Requirement` は年齢クラス毎の行なので表示用に分配する。
        まずグループ間 sufrir最大剰余法（在園児がいる区分に 1 名ずつ優先）で配り、
        次にグループ内の年齢クラスへ園児数に比例して切り分ける。
        合計は厳密に一致する。
        """
        if self.headcount_mode == HEADCOUNT_PER_CLASS:
            return {ac: self.headcount_for(ac, n) for ac, n in counts.items() if n > 0}
        breakdown = self.headcount_breakdown(counts)
        total = self.headcount_for_slot(counts)
        shares = largest_remainder(total, [part for _g, _n, part, _r in breakdown])
        out: dict[AgeClass, int] = {}
        for (group, _n, _part, _ratio), share in zip(breakdown, shares, strict=True):
            members = [ac for ac in group if counts.get(ac, 0) > 0]
            pieces = largest_remainder(share, [float(counts[ac]) for ac in members])
            for age_class, value in zip(members, pieces, strict=True):
                out[age_class] = value
        return out

    def slot_qualified_for(self, needed_total: int) -> int:
        """合計必要人員から 1 時間帯の必要保育士数を決める。

        ``per_class`` は「必要人員＝必要保育士数」（従来どおり）。
        ``ratio`` は ``min_qualified_ratio`` で割り上げ、``min_qualified_floor``
        を下限にする。どちらも ``needed_total`` を超えない。
        """
        if needed_total <= 0:
            return 0
        if self.qualified_mode == QUALIFIED_PER_CLASS:
            return needed_total
        floor_q = max(0, min(self.min_qualified_floor, needed_total))
        ratio_q = int(math.ceil(needed_total * self.min_qualified_ratio))
        return min(needed_total, max(floor_q, ratio_q))

    def allocate_qualified(
        self, staff_alloc: Mapping[AgeClass, int], qualified_total: int
    ) -> dict[AgeClass, int]:
        """必要保育士数を年齢クラスへ割り当てる（合計は ``qualified_total`` と一致）。

        ``staff_alloc`` のすべての年齢クラスをキーとして返す（0 名のものも含む）。
        """
        if self.qualified_mode == QUALIFIED_PER_CLASS:
            return {ac: n for ac, n in staff_alloc.items() if n > 0}
        keys = list(staff_alloc)
        positive = [ac for ac in keys if staff_alloc[ac] > 0]
        shares = dict(
            zip(
                positive,
                largest_remainder(qualified_total, [float(staff_alloc[ac]) for ac in positive]),
                strict=True,
            )
        )
        return {ac: shares.get(ac, 0) for ac in keys}

    def counts_qualify(self, member: StaffMember, slot: Slot) -> bool:  # noqa: F821
        """この基準で職員が「必要保育士数」を満たす資格を持つか。

        ``per_class`` では保育士のみ。``nurse_as_qualified_cap`` が 1 以上のときは
        看護師・准看護師も資格者とする（上限の判定は呼び出し側で行う）。
        ``qualified_extra_roles`` に看護師を入れていれば、それも資格者になる
        （認可外保育施設指導監督基準 第1(2) の「3分の1以上」）。
        """
        if member.has_role(Role.HOIKUSHI):
            return True
        if set(member.roles) & self.qualified_extra_roles:
            return True
        if self.nurse_as_qualified_cap > 0 and member.is_nurse:
            return True
        return False

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
            "headcount_mode": self.headcount_mode,
            "headcount_extra": self.headcount_extra,
            "age_groups": (
                [[ac.value for ac in group] for group in self.age_groups]
                if self.age_groups is not None
                else None
            ),
            "qualified_mode": self.qualified_mode,
            "min_qualified_floor": self.min_qualified_floor,
            "nurse_as_qualified_cap": self.nurse_as_qualified_cap,
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

    facility_name: str = config.DEFAULT_FACILITY_NAME
    day_open: time = config.DEFAULT_DAY_OPEN
    day_close: time = config.DEFAULT_DAY_CLOSE
    granularity_min: int = config.DEFAULT_GRANULARITY_MIN
    capacity: int = 60
    """利用定員。園ごとに変わるため UI から渡す。"""
    closed_days: frozenset[date] = frozenset()
    holiday_dates: frozenset[date] = frozenset()
    rooms: tuple[str, ...] = ("0・1歳児室", "2・3歳児室", "4・5歳児室")
    labor_cost_per_hour: float = config.DEFAULT_LABOR_COST_PER_HOUR
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


WEEKLY_WINDOW_DAYS = config.WEEKLY_WINDOW_DAYS
"""週の判定に使う窓の日数。``config.WEEKLY_WINDOW_DAYS`` の再輸出。"""


def weekly_windows(days: Sequence[date], size: int = WEEKLY_WINDOW_DAYS) -> list[list[date]]:
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


def weekly_periods(days: Sequence[date], size: int = WEEKLY_WINDOW_DAYS) -> list[list[date]]:
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
