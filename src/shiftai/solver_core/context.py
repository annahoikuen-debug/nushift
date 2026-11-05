"""MILP model context, variable helpers, and constraint snapshot structures."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import pulp

from shiftai import config
from shiftai.domain import (
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    PairingRule,
    RequirementTable,
    Slot,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    daily_cap_minutes,
    to_minutes,
)
from shiftai.shift_patterns import ShiftPattern

_DAILY_LEGAL_CAP_MIN = int(config.STATUTORY_MAX_DAILY_WORK_HOURS * 60)
_OVERTIME_FACTOR = config.STATUTORY_OVERTIME_MULTIPLIER
_LONG_DAILY_HOURS = config.INTERNAL_DAILY_LONG_HOURS
_BREAK_CONCURRENT_SHARE = config.BREAK_CONCURRENT_SHARE
_UNUSED_HOURS_EPS = 0.25
_DEFAULT_BREAK_MINUTES = config.DEFAULT_BREAK_MINUTES
_MAX_INTERVAL_CANDIDATES = 150
_DEFAULT_STAGGER_SLOTS = 1
_EPS = config.FLOAT_TOLERANCE
_INFEASIBLE_FALLBACK_MESSAGE = "PuLP では解なし。貪欲法による暫定シフトを生成しました"
_SOFT_COVERAGE_MESSAGE = (
    "配置基準を罰変数に置く 2 パス目（ベストエフォート）で最適化します。"
)

def _daily_cap_minutes(contract: Contract) -> int:
    """その職員が1日に置ける勤務分の上限（後方互換・domain.daily_cap_minutes に委譲）。"""
    return daily_cap_minutes(contract)


def _week_fraction(n_days: int) -> float:
    """日数を週単位に換算する係数（後方互換のため残す）。"""
    return max(n_days, 1) / float(config.WEEKLY_WINDOW_DAYS)


def month_fraction(days: Sequence[date]) -> float:
    """対象期間が何月分かを返す。

    ``min_monthly_hours`` / ``max_monthly_hours`` は「1か月あたり」の値なので、
    1週間だけ計算するときはそのまま比較すると必ず未達判定になってしまう。対象期間に含まれる
    各月の「その月に占める日数 ÷ その月の日数」を合計することで、期間に比例した
    目標時間へ変換する（例: 9月28日〜10月4日 = 3/30 + 4/31 = 0.229）。
    """
    if not days:
        return 0.0
    per_month: dict[tuple[int, int], int] = {}
    for day in days:
        key = (day.year, day.month)
        per_month[key] = per_month.get(key, 0) + 1
    total = 0.0
    for (year, month), count in per_month.items():
        if month == 12:
            nxt = date(year + 1, 1, 1)
        else:
            nxt = date(year, month + 1, 1)
        length = (nxt - date(year, month, 1)).days
        total += count / max(1, length)
    return total


def _var_value(v: object) -> float:
    """変数でも定数でも未決定でも数値に変換する。"""
    if v is None:
        return 0.0
    if isinstance(v, int | float):
        return float(v)
    value = v.value()
    return 0.0 if value is None else float(value)


def _is_zero(v: object) -> bool:
    """LpVariable と 0 の比較は LpConstraint を返し常に真になるため、必ず型で判定する。

    PuLP の ``LpVariable.__eq__`` は線形制約オブジェクトを返すので、
    ``if var == 0:`` は常に真になって「変数セルを定数0扱い」になってしまう。
    """
    return isinstance(v, int | float) and v == 0


def _is_one(v: object) -> bool:
    """``_is_zero`` と同じ理由で型判定する「定数1」判定。

    ``_is_zero`` と対で、``seq.get(i, 0) == 1`` と直接書くと
    ``LpVariable.__eq__`` が ``LpConstraint`` を返すため、最適化に
    残っているすべての変数セルを「固定1」と誤認する。
    """
    return isinstance(v, int | float) and v == 1


def _is_var(v: object) -> bool:
    """定数化していない（最適化に残っている）変数セルか。"""
    return not isinstance(v, int | float)


def _linear(items: Sequence[tuple[object, float]]) -> object:
    """(変数, 係数) の列を線形式へ組み立てる。空なら定数 0 を返す。"""
    if not items:
        return 0
    if len(items) == 1:
        var, coef = items[0]
        if coef == 1:
            return var
        return var * coef
    return pulp.lpSum([var * coef for var, coef in items])


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
    """希望休（不在時間帯）に該当するか。"""
    if prefs is None:
        return False
    return prefs.is_unavailable(day, slot)


@dataclass
class _ModelCtx:
    """構築済み MILP と、デコード・検査に必要な派生情報。"""

    prob: pulp.LpProblem
    work: dict[tuple[str, date, int], object] = field(default_factory=dict)
    brk: dict[tuple[str, date, int], object] = field(default_factory=dict)
    hours: dict[str, object] = field(default_factory=dict)
    day_var: dict[tuple[str, date], object] = field(default_factory=dict)
    day_work: dict[tuple[str, date], object] = field(default_factory=dict)
    shortfall: dict[str, tuple] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    specs: tuple[_ConstraintSpec, ...] = ()

    def counts(self) -> tuple[int, int]:
        return len(self.prob.variables()), len(self.prob.constraints)

    def capture_specs(self) -> None:
        """制約を数値スナップショットとして取り込む（``verify_solution`` 用）。

        検査は PuLP の ``LpConstraint.value()`` に依存させない。``value()`` は
        変数値が未決なら ``None`` を返し、例外を投げる版も存在するため、
        ``try/except`` で囲む実装では「検証できない制約」を
        「違反していない制約」と取り違える。
        """
        self.specs = tuple(
            _ConstraintSpec(name, con.sense, float(con.constant), tuple(con.items()))
            for name, con in self.prob.constraints.items()
        )


@dataclass(frozen=True)
class _ConstraintSpec:
    """制約 1 本を「sense / 定数項 / 係数」に分解した検査用スナップショット。

    ``value()`` は PuLP と同じ ``定数項 + Σ 係数×変数値`` を返す。
    変数値が 1 つでも未決なら ``None``（＝**判定不能**）を返し、
    例外で検査全体が黙って飛ばされないようにする。
    ``None`` をどう扱うか（＝違反には数えない）は :meth:`is_violated` を参照。
    """

    name: str
    sense: int
    offset: float
    terms: tuple[tuple[object, float], ...]

    def value(self) -> float | None:
        total = self.offset
        for var, coef in self.terms:
            raw = getattr(var, "varValue", None)
            if raw is None:
                return None
            total += float(raw) * float(coef)
        return total

    def is_violated(self, tol: float) -> bool:
        """制約違反なら ``True``。**検査不能（変数値が ``None``）なら ``False``**。

        ``None`` を「違反」に倒さないのは意図的な設計で、役割が 2 つに分かれる:

        * ``verify_solution`` = 「**数値的に確定した**ハード制約違反」を列挙する
        * ``_unassigned_variables``（``_run_cbc`` が先に呼ぶ）=
          「そもそも解が読み込まれたか」を判定する

        ``None`` を違反扱いすると、``.solu`` が読めていない状態で
        定数項だけの ``配置基準 expr >= need`` が ``-need`` で未達と判定され、
        **全制約が偽の違反として並ぶ**（誤検出が 300 件超になる）。
        一方 ``None`` を 0 とみなす実装も「配置基準を満たした」と誤判定するため不可。
        どちらも採らず、判定は ``_run_cbc`` の割当チェックに委ねる。
        """
        value = self.value()
        if value is None:
            return False
        if self.sense == pulp.LpConstraintLE:
            return value > tol
        if self.sense == pulp.LpConstraintGE:
            return value < -tol
        if self.sense == pulp.LpConstraintEQ:
            return abs(value) > tol
        return False


def _constraint_specs(ctx: object) -> tuple[_ConstraintSpec, ...]:
    """``ctx`` から制約スナップショットを返す（未収集ならその場で収集する）。"""
    if isinstance(ctx, _ModelCtx):
        if not ctx.specs:
            ctx.capture_specs()
        return ctx.specs
    prob = getattr(ctx, "prob", ctx)
    return tuple(
        _ConstraintSpec(name, con.sense, float(con.constant), tuple(con.items()))
        for name, con in getattr(prob, "constraints", {}).items()
    )


def _hard_le(ctx: _ModelCtx, expr: object, rhs: float, name: str | None = None) -> None:
    """expr <= rhs を追加する。定数化成している場合は矛盾を記録するだけにする。"""
    if isinstance(expr, int | float):
        if float(expr) > rhs + _EPS:
            ctx.conflicts.append(name or "定数条件が矛盾しました")
        return
    if name:
        ctx.prob.addConstraint(expr <= rhs, name)
    else:
        ctx.prob += expr <= rhs


def _hard_ge(ctx: _ModelCtx, expr: object, rhs: float, name: str | None = None) -> None:
    """expr >= rhs を追加する。定数化成している場合は矛盾を記録するだけにする。"""
    if isinstance(expr, int | float):
        if float(expr) < rhs - _EPS:
            ctx.conflicts.append(name or "定数条件が矛盾しました")
        return
    if name:
        ctx.prob.addConstraint(expr >= rhs, name)
    else:
        ctx.prob += expr >= rhs


def _split_penalty(weights: ObjectiveWeights) -> float:
    """勤務/休憩ブロックが分かれたときの1ブロックあたりのペナルティ。

    中抜け勤務（スプリット勤務）を配置不足よりも重く罰し、
    現場で運用不可能な「1日複数回出勤」を確実に抑止する。
    """
    return max(
        weights.split_duty_penalty,
        weights.break_conflict_penalty,
        10.0 * weights.overstaff_penalty,
    )




def _is_sei(member: StaffMember) -> bool:
    """正職員契約かどうか（UI側の説明用）。"""
    return member.contract.employment_type is EmploymentType.SEI


@dataclass
class _SolveInput:
    """最適化 1 回分の入力（2 パス目でも同じものを再利用する）。"""

    children: Sequence[ChildPlan]
    staff: list[StaffMember]
    requirements: RequirementTable
    prefs: dict[str, StaffPreferences]
    fixed: dict[tuple[str, date, str], CellState]
    settings: FacilitySettings
    weights: ObjectiveWeights
    standard: StaffingStandard | None
    patterns: tuple[ShiftPattern, ...] = ()
    relaxation: int = 0
    solver_name: str = config.DEFAULT_SOLVER
    warm_start: bool = True
    warm_start_assignments: dict[tuple[str, date, str], CellState] | None = None
    pattern_mode: bool = False
    pairing_rules: tuple[PairingRule, ...] = ()
