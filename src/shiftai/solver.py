"""PuLP/CBC によるシフト自動作成の MILP モデルと解のデコード。

このモジュールは配置基準エンジンの出力である ``RequirementTable`` だけを受け取る。
園児数や定員比率をここで再計算しないことで「基準を算出する工程」と
「シフトを最適化する工程」を分離している。``children`` は統計表示用に保持するのみ。

作業セルは ``(staff_id, day, slot_index)`` で識別し、各セルに2変数

    w[s,d,t] : 1 なら「勤務」
    b[s,d,t] : 1 なら「休憩（在勤のまま休憩）」

を置く。両者の和が 1 以下のとき「オフ（勤務しない）」とみなす。
one-hot の o を変数化しないことで変数数を 1/3 に抑えている。

1日の在勤は 1 つの連続ブロック（在勤ブロック = 勤務 + 休憩）とし、その内側に休憩を
高々1つの連続ブロックとして置ける。したがって「W-B-W」（昼休みで勤務が分かれる）は
許され、「W-B-W-B-W」は許されない。連続性は追加変数2つ（先頭・末尾の添字）の
線形化で表現しており、線形制約の項数も少ない。

制約の分類:

* ハード: セル状態の排他 / 希望休 / 休園日 / 契約時間帯 / 1日の上限時間 /
  在勤ブロック1つ / 休憩ブロック1つ / 配置基準（必須行）/ UI による手動確定セル
* ソフト: 最低休憩時間 / 同時休憩の集中回避 / 連続勤務日数 / 週の勤務日数 /
  希望休・勤務希望 / 早朝・延長の回避 / 勤務時間の偏り / 月間時間 /
  未使用職員 / 長時間勤務（1日9時間・週44時間）/ 勤務間の休息時間

PuLP 4.0 以降は API が変わるため、古典的な
``LpVariable(name, lowBound, upBound, cat)`` を前提とした実装（pulp 2.9 系）である。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from time import perf_counter

import pulp

from shiftai.config import STATUTORY_DAILY_WORK_HOURS, STATUTORY_WEEKLY_WORK_HOURS
from shiftai.domain import (
    CellState,
    ChildPlan,
    Contract,
    FacilitySettings,
    ObjectiveWeights,
    RequirementTable,
    Role,
    ShiftAssignment,
    ShiftDay,
    Slot,
    SlotKind,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
    to_minutes,
)

_DAILY_LEGAL_CAP_MIN = 600
_OVERTIME_FACTOR = 1.25
_LONG_DAILY_HOURS = STATUTORY_DAILY_WORK_HOURS + 1.0
_BREAK_CONCURRENT_SHARE = 0.25
_UNUSED_HOURS_EPS = 0.25
_DEFAULT_BREAK_MINUTES = 60
_DEFAULT_STAGGER_SLOTS = 1
_EPS = 1e-6
_INFEASIBLE_FALLBACK_MESSAGE = "PuLP では解なし。貪欲法による暫定シフトを生成しました"


def available_solvers() -> list[str]:
    """PuLP が検出できる利用可能な MILP ソルバ名の一覧を返す。"""
    try:
        return list(pulp.listSolvers(onlyAvailable=True))
    except Exception:
        return []


def _daily_cap_minutes(contract: Contract) -> int:
    """その職員が1日に置ける勤務分の上限（法定10時間を超えない）。"""
    base = int(round(contract.daily_hours * 60))
    if not contract.overtime_allowed:
        return int(min(base, _DAILY_LEGAL_CAP_MIN))
    return int(min(base * _OVERTIME_FACTOR, _DAILY_LEGAL_CAP_MIN))


def _week_fraction(n_days: int) -> float:
    """日数を週単位に換算する係数（後方互換のため残す）。"""
    return max(n_days, 1) / 7.0


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
    if isinstance(v, (int, float)):
        return float(v)
    value = v.value()
    return 0.0 if value is None else float(value)


def _is_zero(v: object) -> bool:
    """LpVariable と 0 の比較は LpConstraint を返し常に真になるため、必ず型で判定する。

    PuLP の ``LpVariable.__eq__`` は線形制約オブジェクトを返すので、
    ``if var == 0:`` は常に真になって「変数セルを定数0扱い」になってしまう。
    """
    return isinstance(v, (int, float)) and v == 0


def _is_var(v: object) -> bool:
    """定数化していない（最適化に残っている）変数セルか。"""
    return not isinstance(v, (int, float))


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
        if day.weekday() >= 5:
            return False
        if day in settings.holiday_dates:
            return False
    return True


def _slot_is_contractible(st: StaffMember, slot: Slot) -> bool:
    """その職員が契約上その時間帯に勤務できるかを返す。"""
    contract = st.contract
    return (
        slot.start_minutes >= to_minutes(contract.earliest_start)
        and slot.end_minutes <= to_minutes(contract.latest_end)
    )


def _is_unavailable(
    prefs: StaffPreferences | None, day: date, slot: Slot
) -> bool:
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
    変数値が 1 つでも未決なら ``None``（＝検証不能）を返し、
    例外で検査全体が黙って飛ばされないようにする。
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
    if isinstance(expr, (int, float)):
        if float(expr) > rhs + _EPS:
            ctx.conflicts.append(name or "定数条件が矛盾しました")
        return
    if name:
        ctx.prob.addConstraint(expr <= rhs, name)
    else:
        ctx.prob += expr <= rhs


def _hard_ge(ctx: _ModelCtx, expr: object, rhs: float, name: str | None = None) -> None:
    """expr >= rhs を追加する。定数化成している場合は矛盾を記録するだけにする。"""
    if isinstance(expr, (int, float)):
        if float(expr) < rhs - _EPS:
            ctx.conflicts.append(name or "定数条件が矛盾しました")
        return
    if name:
        ctx.prob.addConstraint(expr >= rhs, name)
    else:
        ctx.prob += expr >= rhs


def _split_penalty(weights: ObjectiveWeights) -> float:
    """勤務/休憩ブロックが分かれたときの1ブロックあたりのペナルティ。

    ObjectiveWeights に専用項目がないため、休憩競合の重みと超過配置の重みから
    「超過配置1名分より重く、配置不足より安く」になる値として導出する。
    """
    return max(weights.break_conflict_penalty, 10.0 * weights.overstaff_penalty)


def _add_block(
    ctx: _ModelCtx,
    seq: Mapping[int, object],
    tag: str,
    n: int,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
) -> None:
    """列 seq が「高々1つの連続ブロック」に近づくよう制約する（ソフトペナルティ）。

    変数 v_i（0/1）に対し「ブロック開始フラグ」s_i を 1 個ずつ置き
    ``s_i >= v_i - v_{i-1}``（v_{-1} = 0）を課す。s_i は目的関数に現れないので
    最小値（= ブロックの開始数）を取り、``Σ s_i - 1 <= slack`` によって
    2つ目以降のブロックだけを罰変数で表現する。ハードにすると「昼休みで勤務が
    分かれる」だけで配置基準を満たせなくなる。そのため
    分割は高コストのソフトペナルティとして扱い、check_violations が
    SPLIT_SHIFT / BREAK_FRAGMENTED を出すことで UI に明示する。
    """
    if n < 1:
        return
    fixed_sum = 0
    terms: list[tuple[object, float]] = []
    for i in range(n):
        cur = seq.get(i, 0)
        prev = seq.get(i - 1, 0) if i > 0 else 0
        if _is_zero(cur) and _is_zero(prev):
            fixed_sum += int(cur) - int(prev)
            continue
        s_var = pulp.LpVariable(f"{tag}_s{i}", lowBound=0, upBound=1)
        ctx.prob += s_var >= cur - prev
        terms.append((s_var, 1))
    if not terms:
        if fixed_sum > 1 and obj is not None and weights is not None:
            gap = pulp.LpVariable(f"{tag}_split", lowBound=0)
            ctx.prob += gap >= fixed_sum - 1
            obj.append(_split_penalty(weights) * gap)
        return
    if obj is None or weights is None:
        _hard_le(ctx, _linear(terms) + fixed_sum, 1)
        return
    slack = pulp.LpVariable(f"{tag}_split", lowBound=0)
    _hard_le(ctx, _linear(terms) + fixed_sum - 1, slack, f"{tag}_splitcap")
    obj.append(_split_penalty(weights) * slack)


def _build_cells(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    fixed: Mapping[tuple[str, date, str], CellState],
    settings: FacilitySettings,
    kinds: Sequence[SlotKind],
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """セル変数の生成と、セル単位のハード制約（排他・在勤/休憩ブロック・1日上限）。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    n = len(slots)
    standby = {d for d in days if not requirements.for_day(d)}
    for st in staff:
        sid = st.staff_id
        prefs_st = prefs.get(sid)
        cap_min = _daily_cap_minutes(st.contract)
        hours = pulp.LpVariable(f"H_{sid}", lowBound=0)
        ctx.hours[sid] = hours
        week_items: list[tuple[object, float]] = []
        for day in days:
            tag = day.strftime("%m%d")
            day_ok = _day_is_workable(st, day, settings) and day not in standby
            if day in standby:
                for i in range(len(slots)):
                    ctx.work[(sid, day, i)] = 0
                    ctx.brk[(sid, day, i)] = 0
                    ctx.day_work[(sid, day)] = 0
                continue
            yvar = pulp.LpVariable(f"yd_{sid}_{tag}", 0, 1, pulp.LpBinary)
            ctx.day_var[(sid, day)] = yvar
            w_cells: dict[int, object] = {}
            b_cells: dict[int, object] = {}
            day_items: list[tuple[object, float]] = []
            for i, slot in enumerate(slots):
                key = (sid, day, i)
                forced = fixed.get((sid, day, slot.label))
                allowed = day_ok and _slot_is_contractible(st, slot)
                if allowed and _is_unavailable(prefs_st, day, slot):
                    allowed = False
                if forced is CellState.WORK:
                    w_cells[i] = 1
                    b_cells[i] = 0
                    # 定数化して最適化から外しても、デコードの復元には必要なので
                    # ctx にも「定数としての値」を残す。以前はここへの記録が
                    # 無く、モデルは固定セルを尊重しているのに出力だけ OFF になっていた。
                    ctx.work[key] = 1
                    ctx.brk[key] = 0
                    day_items.append((1, slot.minutes))
                    week_items.append((1, slot.hours))
                    continue
                if forced is CellState.BREAK:
                    w_cells[i] = 0
                    b_cells[i] = 1
                    ctx.work[key] = 0
                    ctx.brk[key] = 1
                    continue
                if forced is not None or not allowed:
                    w_cells[i] = 0
                    b_cells[i] = 0
                    ctx.work[key] = 0
                    ctx.brk[key] = 0
                    continue
                wvar = pulp.LpVariable(f"w_{sid}_{tag}_{i}", 0, 1, pulp.LpBinary)
                bvar = pulp.LpVariable(f"b_{sid}_{tag}_{i}", 0, 1, pulp.LpBinary)
                ctx.work[key] = wvar
                ctx.brk[key] = bvar
                w_cells[i] = wvar
                b_cells[i] = bvar
                ctx.prob += wvar + bvar <= 1
                day_items.append((wvar, slot.minutes))
                week_items.append((wvar, slot.hours))
                if kinds[i] is SlotKind.EARLY:
                    obj.append(weights.early_shift_penalty * wvar)
                elif kinds[i] is SlotKind.LATE or kinds[i] is SlotKind.LATE_STRICT:
                    obj.append(weights.late_shift_penalty * wvar)

            duty: dict[int, object] = {}
            breaks: dict[int, object] = {}
            for i in range(n):
                wv = w_cells[i]
                bv = b_cells[i]
                if _is_zero(wv) and _is_zero(bv):
                    continue
                duty[i] = wv + bv
                if not _is_zero(bv):
                    breaks[i] = bv
            _add_block(ctx, duty, f"duty_{sid}_{tag}", n, obj, weights)
            _add_block(ctx, breaks, f"brk_{sid}_{tag}", n, obj, weights)

            _hard_le(
                ctx,
                _linear(day_items),
                cap_min,
                f"dailycap_{sid}_{day.isoformat()}",
            )
            ctx.day_work[(sid, day)] = _linear(day_items)
            w_only = [w_cells[i] for i in range(n) if not _is_zero(w_cells[i])]
            if w_only:
                # PuLP の LpAffineExpression は int による除算持っていないため、
                # 「n で割る」を「1/n 倍する」で書く（時間帯 1 個の問題が落ちるのを防ぐ）。
                _hard_ge(ctx, yvar, _linear([(v, 1) for v in w_only]) * (1.0 / n))
                _hard_le(ctx, yvar, _linear([(v, 1) for v in w_only]))
        ctx.prob += hours == _linear(week_items)


def _add_coverage(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
) -> None:
    """配置基準（is_binding=True の行）を課す。

    ``obj`` と ``weights`` を渡すと「ハード制約」ではなく「不足人数 ×
    shortfall_penalty の罰変数」になる。1パス目で実行可能解が得られなかった場合の
    2パス目（ベストエフォート用）で使う。``needed_qualified`` と ``needed_staff``
    は行ごとに独立して合計し、2名ルールで底上げされた行もそのまま扱う。
    """
    soft = obj is not None and weights is not None
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    for day in requirements.all_days():
        by_slot: dict[Slot, list] = {}
        for row in requirements.for_day(day):
            if not row.is_binding:
                continue
            by_slot.setdefault(row.slot, []).append(row)
        for slot, rows in by_slot.items():
            idx = index_of.get(slot)
            if idx is None:
                continue
            need_all = sum(r.needed_staff for r in rows)
            need_q = sum(r.needed_qualified for r in rows)
            all_items = [
                ctx.work[(s.staff_id, day, idx)]
                for s in staff
                if (s.staff_id, day, idx) in ctx.work
            ]
            q_items = [
                ctx.work[(s.staff_id, day, idx)]
                for s in staff
                if s.is_qualified
                and (s.staff_id, day, idx) in ctx.work
                and not _is_zero(ctx.work[(s.staff_id, day, idx)])
            ]
            plans = [(need_q, q_items, "必要保育士", "coverq")]
            if need_all > need_q:
                plans.append((need_all, all_items, "必要人員", "cover"))
            for need, items, label, code in plans:
                if need <= 0:
                    continue
                name = f"{code}_{day.isoformat()}_{slot.label}"
                if not items:
                    ctx.conflicts.append(
                        f"{day.isoformat()} {slot.label} の{label} {need} 名を配置できる職員がいません"
                    )
                    continue
                expr = _linear([(v, 1) for v in items])
                if soft:
                    gap = pulp.LpVariable(f"gap_{name}", lowBound=0)
                    ctx.prob += expr + gap >= need
                    obj.append(weights.shortfall_penalty * gap)
                    ctx.shortfall[name] = (gap, need, day, slot, label)
                else:
                    _hard_ge(ctx, expr, need, name)


def _add_workload(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """超過配置・休憩不足・休憩集中のソフトペナルティ。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    n = len(slots)
    for day in days:
        tag = day.strftime("%m%d")
        need_by_slot: dict[int, int] = {}
        for row in requirements.for_day(day):
            if row.is_binding:
                need_by_slot[index_of.get(row.slot, -1)] = (
                    need_by_slot.get(index_of.get(row.slot, -1), 0) + row.needed_staff
                )
        for idx, need_all in need_by_slot.items():
            if idx < 0 or need_all <= 0:
                continue
            items = [
                (ctx.work[(s.staff_id, day, idx)], 1)
                for s in staff
                if (s.staff_id, day, idx) in ctx.work
                and not _is_zero(ctx.work[(s.staff_id, day, idx)])
            ]
            if not items:
                continue
            over = pulp.LpVariable(f"over_{tag}_{idx}", lowBound=0)
            ctx.prob += _linear(items) - need_all <= over
            obj.append(weights.overstaff_penalty * over)

        n_potential = sum(
            1
            for s in staff
            if any(not _is_zero(ctx.work.get((s.staff_id, day, i), 0)) for i in range(n))
        )
        if n_potential <= 0:
            continue
        cap = max(1, int(_BREAK_CONCURRENT_SHARE * n_potential))
        for i in range(n):
            items = [
                (ctx.brk[(s.staff_id, day, i)], 1)
                for s in staff
                if (s.staff_id, day, i) in ctx.brk and not _is_zero(ctx.brk[(s.staff_id, day, i)])
            ]
            if len(items) <= cap:
                continue
            excess = pulp.LpVariable(f"brkexc_{tag}_{i}", lowBound=0)
            ctx.prob += _linear(items) <= cap + excess
            obj.append(weights.break_conflict_penalty * excess)


def _add_breaks(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    break_minutes: int,
) -> None:
    """勤務日ごとに連続した休憩が取れるようにする（不足分はソフトペナルティ）。"""
    slots = tuple(requirements.slots)
    gran = max(1, int(requirements.granularity_min or slots[0].minutes))
    for st in staff:
        sid = st.staff_id
        cap_min = _daily_cap_minutes(st.contract)
        need_slots = min(
            max(1, -(-break_minutes // gran)), max(0, cap_min // gran)
        )
        if need_slots <= 0:
            continue
        for day in requirements.all_days():
            tag = day.strftime("%m%d")
            yvar = ctx.day_var.get((sid, day))
            if yvar is None:
                continue
            items = [
                (ctx.brk[(sid, day, i)], 1)
                for i in range(len(slots))
                if not _is_zero(ctx.brk.get((sid, day, i), 0))
            ]
            if not items:
                continue
            deficit = pulp.LpVariable(f"brkdef_{sid}_{tag}", lowBound=0)
            ctx.prob += _linear(items) >= need_slots * yvar - deficit
            obj.append(weights.break_conflict_penalty * deficit)


def _add_rest(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """勤務間の最低休息時間を罰変数で扱う（ハードにすると解が消えるため）。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    for a, b in zip(days, days[1:], strict=False):
        for st in staff:
            sid = st.staff_id
            gap = (
                (b - a).days * 24 * 60 - slots[-1].end_minutes + slots[0].start_minutes
            )
            if gap >= st.contract.min_rest_hours * 60 - _EPS:
                continue
            fin = ctx.work.get((sid, a, len(slots) - 1), 0)
            stt = ctx.work.get((sid, b, 0), 0)
            if _is_zero(fin) and _is_zero(stt):
                continue
            rv = pulp.LpVariable(
                f"rest_{sid}_{a.isoformat()}_{b.isoformat()}", lowBound=0, upBound=1
            )
            ctx.prob += rv >= fin + stt - 1
            ctx.prob += rv <= fin
            ctx.prob += rv <= stt
            obj.append(weights.rest_violation_penalty * rv)


def _add_consecutive(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """連続勤務日数と週の勤務日数をスライド窓の罰変数で制限する。"""
    days = requirements.all_days()
    for st in staff:
        sid = st.staff_id
        maxc = max(1, int(st.contract.max_consecutive_days))
        width = maxc + 1
        z_of: dict[tuple[date, date], object] = {}
        for a, b in zip(days, days[1:], strict=False):
            y1 = ctx.day_var.get((sid, a))
            y2 = ctx.day_var.get((sid, b))
            if y1 is None or y2 is None:
                continue
            z = pulp.LpVariable(
                f"z_{sid}_{a.isoformat()}_{b.isoformat()}", lowBound=0, upBound=1
            )
            ctx.prob += z >= y1 + y2 - 1
            ctx.prob += z <= y1
            ctx.prob += z <= y2
            z_of[(a, b)] = z
        if z_of and len(days) >= width:
            for i in range(len(days) - width + 1):
                window = days[i : i + width]
                z_terms = [
                    (z_of[(a, b)], 1)
                    for a, b in zip(window, window[1:], strict=False)
                    if (a, b) in z_of
                ]
                if not z_terms:
                    continue
                slack = pulp.LpVariable(f"cons_{sid}_{i}", lowBound=0)
                ctx.prob += _linear(z_terms) <= maxc - 1 + slack
                obj.append(weights.consecutive_day_penalty * slack)
        if st.contract.max_weekly_days > 0:
            y_items = [
                (ctx.day_var[(sid, d)], 1)
                for d in days
                if (sid, d) in ctx.day_var
            ]
            if y_items:
                slack = pulp.LpVariable(f"weekly_{sid}", lowBound=0)
                ctx.prob += _linear(y_items) <= st.contract.max_weekly_days + slack
                obj.append(weights.consecutive_day_penalty * slack)


def _add_preferences(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    kinds: Sequence[SlotKind],
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """希望休・勤務希望・早朝/延長回避をソフト制約として扱う。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    for st in staff:
        sid = st.staff_id
        p = prefs.get(sid)
        if p is None:
            continue
        for day in days:
            yvar = ctx.day_var.get((sid, day))
            if yvar is None:
                continue
            tag = day.strftime("%m%d")
            if day in p.preferred_off_days:
                miss = pulp.LpVariable(f"poff_{sid}_{tag}", 0, 1, pulp.LpBinary)
                ctx.prob += yvar + miss <= 1
                obj.append(weights.preference_miss_penalty * miss)
            if day in p.preferred_days:
                miss = pulp.LpVariable(f"pon_{sid}_{tag}", 0, 1, pulp.LpBinary)
                ctx.prob += yvar + miss >= 1
                obj.append(weights.preference_match_bonus * miss)
            for slot in p.preferred_slots_for(day):
                idx = index_of.get(slot)
                if idx is None:
                    continue
                wv = ctx.work.get((sid, day, idx), 0)
                if _is_zero(wv):
                    continue
                miss = pulp.LpVariable(f"pslot_{sid}_{tag}_{idx}", 0, 1, pulp.LpBinary)
                ctx.prob += wv + miss >= 1
                obj.append(weights.preference_miss_penalty * miss)
        for kinds_set, flag, cap, weight, name in (
            ((SlotKind.EARLY,), p.avoid_early, p.max_early_shifts, weights.early_shift_penalty, "early"),
            (
                (SlotKind.LATE, SlotKind.LATE_STRICT),
                p.avoid_late,
                p.max_late_shifts,
                weights.late_shift_penalty,
                "late",
            ),
        ):
            if not flag:
                continue
            day_items: list[tuple[object, float]] = []
            for day in days:
                items = [
                    (ctx.work[(sid, day, i)], 1)
                    for i, k in enumerate(kinds)
                    if k in kinds_set and not _is_zero(ctx.work.get((sid, day, i), 0))
                ]
                if not items:
                    continue
                fvar = pulp.LpVariable(
                    f"{name}flag_{sid}_{day.strftime('%m%d')}", lowBound=0, upBound=1
                )
                for item in items:
                    ctx.prob += fvar >= item[0]
                ctx.prob += fvar <= _linear(items)
                day_items.append((fvar, 1))
            if not day_items:
                continue
            slack = pulp.LpVariable(f"{name}over_{sid}", lowBound=0)
            ctx.prob += _linear(day_items) <= max(0, int(cap)) + slack
            obj.append(weight * slack)


def _staff_signature(
    st: StaffMember, prefs: Mapping[str, StaffPreferences] | None
) -> tuple:
    """対称性打断に使う署名（契約・役割・希望が同じ職員は同じ署名になる）。"""
    p = (prefs or {}).get(st.staff_id)
    if p is None:
        pref_key: tuple = ()
    else:
        pref_key = (
            tuple(sorted((u.day.isoformat(), u.start.isoformat(), u.end.isoformat())
                         for u in p.unavailable)),
            tuple(sorted(d.isoformat() for d in p.preferred_off_days)),
            tuple(sorted(d.isoformat() for d in p.preferred_days)),
            p.avoid_early,
            p.avoid_late,
            p.max_early_shifts,
            p.max_late_shifts,
        )
    c = st.contract
    return (
        tuple(sorted(r.value for r in st.roles)),
        c.weekly_hours,
        c.daily_hours,
        c.employment_type.value,
        c.min_monthly_hours,
        c.max_monthly_hours,
        c.max_weekly_days,
        c.max_consecutive_days,
        c.min_rest_hours,
        c.earliest_start,
        c.latest_end,
        c.can_work_holiday,
        c.overtime_allowed,
        pref_key,
    )


def _add_symmetry_breaking(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    prefs: Mapping[str, StaffPreferences] | None,
) -> None:
    """契約・希望が完全に同一の職員グループに勤務時間の大小関係を課す。

    この条件は「同じグループ内では入れ替えても目的関数が変わらない」ことを使う
    WLOG な対称性打断であり、最適解を失わない。職員数が多いほどCBCの探索が
    な探索になり、求解時間を大きく短縮できる。
    """
    groups: dict[tuple, list[str]] = {}
    for st in staff:
        groups.setdefault(_staff_signature(st, prefs), []).append(st.staff_id)
    for _, ids in groups.items():
        if len(ids) < 2:
            continue
        for a, b in zip(ids, ids[1:], strict=False):
            if a in ctx.hours and b in ctx.hours:
                ctx.prob += ctx.hours[a] <= ctx.hours[b]


def _add_hours_objective(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    prefs: Mapping[str, StaffPreferences] | None = None,
) -> None:
    """勤務時間の偏り・月間時間・未使用職員・長時間勤務のペナルティ。"""
    days = requirements.all_days()
    _add_symmetry_breaking(ctx, staff, prefs)
    if staff:
        hmax = pulp.LpVariable("H_max", lowBound=0)
        hmin = pulp.LpVariable("H_min", lowBound=0)
        for sid in ctx.hours:
            ctx.prob += hmax >= ctx.hours[sid]
            ctx.prob += hmin <= ctx.hours[sid]
        obj.append(weights.hours_imbalance_penalty * (hmax - hmin))
    weeks = month_fraction(days)
    for st in staff:
        sid = st.staff_id
        hvar = ctx.hours[sid]
        lo = st.contract.min_monthly_hours * weeks
        hi = st.contract.max_monthly_hours * weeks
        reachable = len(days) * _daily_cap_minutes(st.contract) / 60.0
        if lo > 0:
            short = pulp.LpVariable(f"mshort_{sid}", lowBound=0)
            ctx.prob += hvar + short >= lo
            obj.append(weights.monthly_hours_penalty * short)
        if 0 < hi < reachable:
            over = pulp.LpVariable(f"mover_{sid}", lowBound=0)
            ctx.prob += hvar <= hi + over
            obj.append(weights.monthly_hours_penalty * over)
        unused = pulp.LpVariable(f"unused_{sid}", lowBound=0)
        ctx.prob += hvar + unused >= _UNUSED_HOURS_EPS
        obj.append(weights.unused_staff_penalty * unused)
        long_week = pulp.LpVariable(f"longw_{sid}", lowBound=0)
        ctx.prob += hvar - STATUTORY_WEEKLY_WORK_HOURS <= long_week
        obj.append(weights.max_shift_length_penalty * long_week)
    for sid, day in ctx.day_work:
        expr = ctx.day_work[(sid, day)]
        if isinstance(expr, (int, float)):
            continue
        long_day = pulp.LpVariable(f"longd_{sid}_{day.strftime('%m%d')}", lowBound=0)
        ctx.prob += expr - _LONG_DAILY_HOURS <= long_day
        obj.append(weights.max_shift_length_penalty * long_day)


def _build_problem(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    fixed: Mapping[tuple[str, date, str], CellState],
    settings: FacilitySettings,
    weights: ObjectiveWeights,
    standard: StaffingStandard | None,
    soft_coverage: bool = False,
) -> _ModelCtx:
    """MILP を構築する。

    ``soft_coverage=True`` のときは配置基準を「不足人数 × shortfall_penalty」の
    罰変数にし、他のハード制約はそのまま保つ（ベストエフォート用）。
    """
    ctx = _ModelCtx(prob=pulp.LpProblem("shift_scheduling", pulp.LpMinimize))
    slots = tuple(requirements.slots)
    if standard is not None:
        kinds = [standard.slot_kind(slot) for slot in slots]
        break_minutes = int(standard.break_minutes)
    else:
        kinds = [SlotKind.NORMAL] * len(slots)
        break_minutes = _DEFAULT_BREAK_MINUTES
    obj: list[object] = []
    _build_cells(ctx, staff, requirements, prefs, fixed, settings, kinds, obj, weights)
    if soft_coverage:
        _add_coverage(ctx, staff, requirements, obj, weights)
    else:
        _add_coverage(ctx, staff, requirements)
    _add_workload(ctx, staff, requirements, obj, weights)
    _add_breaks(ctx, staff, requirements, obj, weights, break_minutes)
    _add_rest(ctx, staff, requirements, obj, weights)
    _add_consecutive(ctx, staff, requirements, obj, weights)
    _add_preferences(ctx, staff, requirements, prefs, kinds, obj, weights)
    _add_hours_objective(ctx, staff, requirements, obj, weights, prefs)
    if obj:
        ctx.prob += pulp.lpSum(obj)
    else:
        ctx.prob += 0
    ctx.capture_specs()
    return ctx


@dataclass
class _SolveInput:
    """最適化1回分の入力（2パス目でも同じものを再利用する）。"""

    children: Sequence[ChildPlan]
    staff: list[StaffMember]
    requirements: RequirementTable
    prefs: dict[str, StaffPreferences]
    fixed: dict[tuple[str, date, str], CellState]
    settings: FacilitySettings
    weights: ObjectiveWeights
    standard: StaffingStandard | None


def solve_shift(
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    *,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    settings: FacilitySettings | None = None,
    time_limit_sec: int = 60,
    msg: bool = False,
    standard: StaffingStandard | None = None,
) -> SolveResult:
    """MILP でシフトを最適化する。配置基準を満たせない場合は自動で2パス目に退避する。

    1パス目は配置基準をハード制約として解く。1パス目が「解なし」だった場合のみ、
    配置基準を「不足人数 × shortfall_penalty」の罰変数に置き換えた2パス目で
    再最適化する。ハード制約（希望休・契約時間帯・1日上限・在勤ブロック・休息時間）は
    そのまま保つので、法令・契約に反するシフトは生成されない。貪欲法は2パス目にも
    解がない場合の最終保険。

    :param children: 園児の登降園予定（統計目的にのみ使用する）
    :param staff: 職員一覧
    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param preferences: 職員IDごとの個人希望
    :param weights: 目的関数の重み
    :param fixed_assignments: (職員ID, 日付, "HH:MM-HH:MM") -> CellState の手動確定セル
    :param settings: 園設定（休園日など）
    :param time_limit_sec: ソルバの実行時間上限（秒）
    :param msg: ソルバログを表示するか
    :param standard: 早朝・延長の時間帯区分を判定するための基準（省略時は全て NORMAL）
    :returns: 最適/実行可能/部分的なシフト、違反情報、所要時間などの統計
    """
    started = perf_counter()
    payload = _SolveInput(
        children=list(children),
        staff=list(staff),
        requirements=requirements,
        prefs=dict(preferences or {}),
        fixed=dict(fixed_assignments or {}),
        settings=settings or FacilitySettings(),
        weights=weights or ObjectiveWeights(),
        standard=standard,
    )
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    budget = max(2, int(time_limit_sec))
    stats: dict = {
        "solver": "PULP_CBC_CMD",
        "solver_status": "",
        "solver_status_pass1": "",
        "pass": 1,
        "relaxed": False,
        "elapsed_sec": 0.0,
        "num_variables": 0,
        "num_constraints": 0,
        "num_staff": len(payload.staff),
        "num_children": len(children),
        "num_days": len(days),
        "num_slots": len(slots),
    }

    if not payload.staff or not days or not slots:
        return SolveResult(
            status=SolveStatus.ERROR,
            messages=["職員・対象日・時間帯のいずれかが空です。入力条件を確認してください。"],
            stats=stats,
        )

    try:
        ctx = _build_problem(
            payload.staff, requirements, payload.prefs, payload.fixed,
            payload.settings, payload.weights, payload.standard,
        )
    except Exception as exc:
        stats["elapsed_sec"] = round(perf_counter() - started, 3)
        stats["solver_status"] = "ModelError"
        return _greedy(payload, stats, started, f"モデル構築に失敗しました: {exc}")

    stats["num_variables"], stats["num_constraints"] = ctx.counts()
    if ctx.conflicts:
        stats["solver_status_pass1"] = "PrecheckInfeasible"
        return _best_effort(payload, stats, started, budget, "PrecheckInfeasible")

    status, raw = _run_cbc(ctx, max(1, int(budget * 0.5)), msg)
    stats["solver_status_pass1"] = raw
    if status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        return _finish(ctx, payload, stats, started, status, pass_no=1, relaxed=False)
    stats["solver_status"] = raw
    return _best_effort(payload, stats, started, budget, raw)


def _run_cbc(ctx: _ModelCtx, limit_sec: int, msg: bool) -> tuple[SolveStatus, str]:
    """CBC を実行して SolveStatus と生のステータス文字列を返す。"""
    try:
        ctx.prob.solve(pulp.PULP_CBC_CMD(msg=msg, timeLimit=max(1, int(limit_sec))))
    except Exception as exc:
        return SolveStatus.ERROR, f"SolveError / {exc}"
    status_raw = pulp.LpStatus.get(ctx.prob.status, "Undefined")
    sol_raw = pulp.LpSolution.get(ctx.prob.sol_status, "No Solution Found")
    raw = f"{status_raw} / {sol_raw}"
    if status_raw == "Infeasible":
        return SolveStatus.INFEASIBLE, raw
    if status_raw == "Unbounded":
        return SolveStatus.ERROR, raw
    if not _has_solution(ctx):
        return SolveStatus.INFEASIBLE, raw
    unassigned = _unassigned_variables(ctx)
    if unassigned:
        raw = f"{raw}（{unassigned} 変数の値なし＝解未読込）"
        return SolveStatus.INFEASIBLE, raw
    if _non_integral_variables(ctx):
        # 時間切れで CBC が LP 緩和値（0.19 や 0.83 のような途中値）を書き戻すことがある。
        # それを丸めてハード制約を満たすなら正しい MILP 解なので採用し、
        # 満たさないなら「解なし」として 2 パス目（罰変数化）に退避する。
        _round_to_integral(ctx)
        if _non_integral_variables(ctx):
            return SolveStatus.INFEASIBLE, raw
        raw = f"{raw}（途中値を0/1に丸めて採用）"
    violated = verify_solution(ctx)
    if violated:
        raw = f"{raw}（制約違反 {len(violated)} 本: {violated[0]}）"
        return SolveStatus.INFEASIBLE, raw
    if status_raw == "Optimal" and sol_raw == "Optimal Solution Found":
        return SolveStatus.OPTIMAL, raw
    if status_raw == "Optimal":
        return SolveStatus.FEASIBLE, raw
    return SolveStatus.FEASIBLE, raw


def _has_solution(ctx: _ModelCtx) -> bool:
    """求まった解に勤務セルが1つでも含まれるか。"""
    return any(
        _var_value(v) > 0.5 for v in ctx.work.values() if not isinstance(v, (int, float))
    )


def verify_solution(ctx: _ModelCtx, tol: float = 1e-4) -> list[str]:
    """返ってきた解がすべてのハード制約を満たしているかを確認する。

    CBC は「解なし」と報告した場合にも .solu ファイルへ途中の値を書き残すことがある
    ため、PuLP が Optimal/Feasible と報告しても制約違反した解が読み込まれることがある。
    UI には法令違反を含んだ解を出さないため、実際に値を評価して検証する。

    検査は ``_constraint_specs`` が保持する数値スナップショットで行う。
    ``LpConstraint.value()`` に依存しないのは、同メソッドが変数値未決時に
    ``None``（検査不能）を返し、例外を投げる版も存在するためである。
    「検査できない」を「違反していない」に読み替えると、
    CBC の途中解が常に「制約を満たす解」として採用されてしまう。

    :returns: 違反した制約名のリスト（空なら制約を満たす解）
    """
    return [spec.name for spec in _constraint_specs(ctx) if spec.is_violated(tol)]


def _unassigned_variables(ctx: _ModelCtx) -> int:
    """値が入っていない変数の個数（解が読み込まれていない判定用）。"""
    return sum(1 for v in ctx.prob.variables() if v.varValue is None)


def _non_integral_variables(ctx: _ModelCtx, tol: float = 1e-4) -> int:
    """0/1 になっていない整数変数の個数（CBC の途中値を検出するため）。"""
    count = 0
    for var in ctx.prob.variables():
        if var.cat != pulp.LpInteger:
            continue
        raw = var.varValue
        if raw is None or abs(float(raw) - round(float(raw))) > tol:
            count += 1
    return count


def _round_to_integral(ctx: _ModelCtx, tol: float = 1e-4) -> None:
    """整数変数を 0/1 に丸める（丸めた後にハード制約を検証し直す）。"""
    for var in ctx.prob.variables():
        if var.cat != pulp.LpInteger or var.varValue is None:
            continue
        rounded = float(round(float(var.varValue)))
        if abs(float(var.varValue) - rounded) > tol:
            var.varValue = rounded


def _best_effort(
    payload: _SolveInput,
    stats: dict,
    started: float,
    budget: int,
    pass1_status: str,
) -> SolveResult:
    """配置基準を罰変数に置き換えた2パス目（ベストエフォート）を実行する。"""
    diagnosis = _coverage_diagnosis(payload)
    try:
        soft = _build_problem(
            payload.staff, payload.requirements, payload.prefs, payload.fixed,
            payload.settings, payload.weights, payload.standard, soft_coverage=True,
        )
    except Exception:
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)
    left = budget - (perf_counter() - started)
    status, raw = _run_cbc(soft, max(1, int(left)), False)
    stats["solver_status"] = f"{raw}（配置基準を罰変数化）"
    if status in (SolveStatus.INFEASIBLE, SolveStatus.ERROR):
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)
    stats["num_variables"], stats["num_constraints"] = soft.counts()
    result = _finish(
        soft, payload, stats, started, status, pass_no=2, relaxed=True
    )
    result.messages = [_INFEASIBLE_FALLBACK_MESSAGE, *diagnosis, *result.messages]
    return result


def _greedy(
    payload: _SolveInput,
    stats: dict,
    started: float,
    message: str,
    diagnosis: list[str] | None = None,
) -> SolveResult:
    """貪欲法による最終保険。"""
    result = solve_shift_greedy(
        payload.children, payload.staff, payload.requirements, payload.prefs,
        fixed_assignments=payload.fixed, standard=payload.standard,
        settings=payload.settings,
    )
    result.messages = [message, *(diagnosis or []), *result.messages]
    result.stats.update({k: v for k, v in stats.items() if k != "solver"})
    result.stats["solver"] = "PULP_CBC_CMD"
    result.stats["fallback"] = "greedy"
    return result


def _finish(
    ctx: _ModelCtx,
    payload: _SolveInput,
    stats: dict,
    started: float,
    status: SolveStatus,
    *,
    pass_no: int,
    relaxed: bool,
) -> SolveResult:
    """解をデコードして SolveResult にまとめる。"""
    elapsed = perf_counter() - started
    if not stats.get("solver_status"):
        stats["solver_status"] = stats.get("solver_status_pass1", "")
    stats["elapsed_sec"] = round(elapsed, 3)
    stats["pass"] = pass_no
    stats["relaxed"] = relaxed
    requirements = payload.requirements
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    shift_days, assignments = decode_solution(ctx, payload.staff, days, slots)
    objective = pulp.value(ctx.prob.objective)
    bad = sorted(
        (
            (_var_value(gap), need, day, slot.label, label)
            for gap, need, day, slot, label in ctx.shortfall.values()
        ),
        reverse=True,
    )
    bad = [row for row in bad if row[0] > 0.5]
    if bad:
        status = SolveStatus.PARTIAL
    result = SolveResult(
        status=status,
        shift_days=shift_days,
        assignments=assignments,
        objective_value=float(objective) if objective is not None else None,
        messages=_solution_messages(status, objective, elapsed, relaxed, bad, stats),
        stats=stats,
    )
    _attach_violations(
        result, requirements, payload.staff, payload.prefs, payload.standard, payload.settings
    )
    return result


def _solution_messages(
    status: SolveStatus,
    objective: float | None,
    elapsed: float,
    relaxed: bool,
    bad: list[tuple],
    stats: dict,
) -> list[str]:
    """日本語のサマリーメッセージを組み立てる。"""
    obj = f"{objective:.2f}" if objective is not None else "算出不可"
    if status is SolveStatus.OPTIMAL:
        head = f"最適解を求めました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    elif status is SolveStatus.FEASIBLE:
        head = f"時間制限内で実行可能解を得ました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    elif status is SolveStatus.PARTIAL:
        head = (
            f"配置基準を満たしきれませんでした。可能な限り充填した暫定シフトです"
            f"（目的関数={obj}、所要{elapsed:.2f}秒）。"
        )
    else:
        head = f"シフトを生成しました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    out = [head]
    if relaxed:
        out.append(
            "配置基準を罰変数に置き換えた2パス目（ベストエフォート）で最適化しました。"
            f"1パス目の結果: {stats.get('solver_status_pass1', '-')}"
        )
    if bad:
        total = sum(row[0] for row in bad)
        worst = bad[0]
        out.append(
            f"配置不足は {len(bad)} 時間帯・合計{total:.0f} 名分です。"
            f"最大は {worst[2].isoformat()} {worst[3]} の{worst[4]}が {worst[0]:.0f} 名不足"
            f"（必要 {worst[1]:.0f} 名）です。"
        )
    return out


def _coverage_diagnosis(payload: _SolveInput, top: int = 5) -> list[str]:
    """配置基準を満たせない原因を日本語で説明する（需要と供給の不足を提示する）。"""
    rows = _shortfall_supply(payload)
    out: list[str] = []
    if rows:
        out.append(
            f"配置基準を満たせませんでした。需要 > 供給 となる時間帯は {len(rows)} 件です。"
            f"不足の大きい順に上位{top}件を示します"
            "（供給＝契約時間帯内で希望休でもない職員数）:"
        )
        for day, label, need_all, need_q, sup_all, sup_q, _support in rows[:top]:
            detail = []
            if sup_q < need_q:
                detail.append(f"保育士 不足{need_q - sup_q}名（必要{need_q}名/供給{sup_q}名）")
            if sup_all < need_all:
                detail.append(f"人員 不足{need_all - sup_all}名（必要{need_all}名/供給{sup_all}名）")
            out.append(f"  ・{day.isoformat()} {label}: " + "、".join(detail))
    out.extend(_staffing_advice(payload, rows))
    return out


def _staffing_advice(payload: _SolveInput, rows: Sequence[tuple]) -> list[str]:
    """不足の構造から「何を変えれば足りるか」を日本語で提案する。

    ``need_h`` / ``supply_h`` のどちらかが 0 になりうるため、充足率は必ず
    0 除算を避けて算出する。0 のときは充足率を出さず、原因と対処を
    日本語で返す（``solve_shift`` 全体が例外で落ちないことを優先する）。
    """
    need_h = payload.requirements.total_needed_hours()
    supply_h = _supply_hours(payload)
    if not rows:
        if need_h <= 0:
            return [
                "需要人時が 0 時間です。園児の登降園予定が登録されていないか、"
                "園の開所時間に合っているかを確認してください。"
                "在園予定を登録してから再実行してください。",
            ]
        return []
    staff = payload.staff
    q_total = sum(1 for s in staff if s.is_qualified)
    se = sum(1 for s in staff if s.primary_role is Role.HOIKUSHI
             and s.contract.employment_type is not None and _is_sei(s))
    part_q = q_total - se
    support = sum(1 for s in staff if s.has_role(Role.SHIENSHIIN))
    if supply_h <= 0:
        # 契約時間帯が園の開所時間と 1 分も重なっていない入力では除算が 0 割になる
        ratio_line = (
            f"  必要人員合計 {need_h:.1f} 人時／計画期間内の契約上限合計 0.0 人時"
            "（充足率は算定不可）"
        )
    else:
        ratio_line = (
            f"  必要人員合計 {need_h:.1f} 人時／計画期間内の契約上限合計 {supply_h:.1f} 人時"
            f"（充足率 {need_h / supply_h * 100:.0f}% 相当）"
        )
    advice = [
        "",
        "【人員の見直し】",
        ratio_line,
        f"  保有: 保育士 {q_total} 名（正職員 {se} 名・パート {part_q} 名）"
        f"／子育て支援員 {support} 名／その他 {len(staff) - q_total - support} 名",
    ]
    if supply_h <= 0:
        advice.append(
            "  ・配置できる人時が 0 です。職員の契約時間帯（earliest_start / latest_end）が"
            "園の開所時間・必要人員の時間帯と 1 分も重なっていません。"
            "契約の始業・終業時刻、園の開所時刻、延長保育の有無を調整してから"
            "再実行してください。"
        )
    elif supply_h < need_h:
        # 0 < supply_h < need_h: 供給はあるが明らかに不足。
        # 表現に注意: この分岐は supply_h < need_h なので need_h/supply_h > 1 であり、
        # 「必要量は供給の何倍か」と読む表現にしなければならない
        # （「供給は必需の何倍」と書くと供給が多いという逆の誤解を招く）。
        advice.append(
            f"  ・必要人時は配置可能人時の {need_h / supply_h:.1f} 倍です"
            f"（必要 {need_h:.1f} 人時に対し契約上限は {supply_h:.1f} 人時のみ）。"
            "計画期間全体で見ても人員が不足する計算です。職員数の増員、"
            "登降園予定の見直し、延長保育の縮小をご検討ください。"
        )
    for day, label, need_all, need_q, sup_all, sup_q, _support in rows[:3]:
        if sup_q < need_q and sup_all >= need_all:
            advice.append(
                f"  ・{day.isoformat()} {label} は「人数は足りるが保育士が足りない」状態です。"
                f"{label} の保育標準時間は保育士配置が必要なので、"
                "①保育士の増員、②他職種（支援員）を充てる条件の緩和、"
                "③保育標準時間帯の在園目標数の見直しをご検討ください。"
            )
        elif sup_all < need_all:
            advice.append(
                f"  ・{day.isoformat()} {label} は人員そのものが足りません（{need_all}名必要/"
                f"最大{sup_all}名配置可能）。職員数の増員、登降園時間の調整、"
                "延長保育の縮小をご検討ください。"
            )
        else:
            advice.append(
                f"  ・{day.isoformat()} {label} は供給自体は足りますが、"
                "1日の上限時間・連続勤務日数・最低休息時間により同時刻に揃えることが"
                "できない可能性があります。契約時間の変更（1日の上限・週所定日数）"
                "をご検討ください。"
            )
    advice.append(
        "  ・支援員は「必要保育士数」に数えられないため、保育標準時間には配置できません。"
        "保育基準で代替を認めている時間帯（延長保育の緩和措置など）を利用してください。"
    )
    return advice


def _is_sei(member: StaffMember) -> bool:
    """正職員契約かどうか（UI 側の説明用）。"""
    from shiftai.domain import EmploymentType

    return member.contract.employment_type is EmploymentType.SEI


def supply_hours(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
) -> float:
    """計画期間内に配置できる勤務分の上限合計（人時）を返す。

    UI の「必要／供給比」とソルバの診断で **同じ式**を使うための公開関数。
    以前 UI 側は「週契約時間 × 日数/5」を独自に計算しており、ソルバ側と
    約 1.2〜1.3 倍乖離していた。UI が「充足可能」と緑表示でもソルバが
    不足を返すような、表示と実態が食い違う不具合があった。

    算出根拠:
      * 必要人員の行がある日（＝休園日を除く）だけを数える
      * 契約時間帯・希望休のどちらにも該当しない時間帯しか無い職員は 0
      * 実際に在勤できる日数と ``max_weekly_days`` の小さい方を日数とし、
        1日の上限時間（法定10時間を超えない範囲）を掛ける
      * ``max_weekly_days`` が 0 のときは「上限なし」と解釈する
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    days = [d for d in requirements.all_days() if requirements.for_day(d)]
    total = 0.0
    for member in staff:
        workable = sum(
            1
            for d in days
            if _day_is_workable(member, d, fac)
            and any(
                _slot_is_contractible(member, s)
                and not _is_unavailable(prefs.get(member.staff_id), d, s)
                for s in requirements.slots
            )
        )
        if workable <= 0:
            continue
        weekly_cap = member.contract.max_weekly_days
        cap_days = workable if weekly_cap <= 0 else min(workable, weekly_cap)
        total += cap_days * _daily_cap_minutes(member.contract) / 60.0
    return total


def _supply_hours(payload: _SolveInput) -> float:
    """``supply_hours`` の ``_SolveInput`` 版（内部呼び出し用）。"""
    return supply_hours(
        payload.staff, payload.requirements, payload.prefs, payload.settings
    )


def _shortfall_supply(payload: _SolveInput) -> list[tuple]:
    """需要 > 供給となる (日, 時間帯) を不足量の大きい順に返す。"""
    slots = tuple(payload.requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    need: dict[tuple[date, int], list[int]] = {}
    for day in payload.requirements.all_days():
        for row in payload.requirements.for_day(day):
            if not row.is_binding:
                continue
            idx = index_of.get(row.slot)
            if idx is None:
                continue
            acc = need.setdefault((day, idx), [0, 0])
            acc[0] += row.needed_staff
            acc[1] += row.needed_qualified
    out: list[tuple] = []
    for (day, idx), (need_all, need_q) in need.items():
        if need_all <= 0 and need_q <= 0:
            continue
        sup_all = sup_q = sup_qualified_only = 0
        for member in payload.staff:
            if not _day_is_workable(member, day, payload.settings):
                continue
            if not _slot_is_contractible(member, slots[idx]):
                continue
            if _is_unavailable(payload.prefs.get(member.staff_id), day, slots[idx]):
                continue
            sup_all += 1
            if member.is_qualified:
                sup_q += 1
            elif member.has_role(Role.SHIENSHIIN):
                sup_qualified_only += 1
        gap = max(0, need_all - sup_all) + max(0, need_q - sup_q)
        if gap > 0:
            out.append((day, slots[idx].label, need_all, need_q, sup_all, sup_q,
                        sup_qualified_only))
    out.sort(key=lambda r: (-(max(0, r[2] - r[4]) + max(0, r[3] - r[5])), r[0], r[1]))
    return out


def _attach_violations(
    result: SolveResult,
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    prefs: Mapping[str, StaffPreferences],
    standard: StaffingStandard | None,
    settings: FacilitySettings,
) -> None:
    """gap_analysis の検査結果を SolveResult に載せる。

    検査は法令遵守の最後の砦なので **fail closed** で扱う。検査器が
    import できない／例外を投げた場合は「違反なし」とは報告せず、
    BLOCKER を 1 件出して ``SolveStatus.ERROR`` に落とす。
    単に ``violations=[]`` にすると UI は「違反 0 件」と表示し、
    利用者は適合したシフトだと思って出力・配布してしまう。
    """
    try:
        from shiftai import gap_analysis
    except Exception as exc:  # pragma: no cover - 依存関係が壊れている場合のみ
        result.violations = [_checker_unavailable(exc)]
        result.status = SolveStatus.ERROR
        result.messages = [
            f"法令違反の検査を実行できませんでした（{exc}）。"
            "適合可否は判定できていないため、出力物を確定的に使わないでください。"
        ]
        return
    try:
        result.violations = gap_analysis.check_violations(
            requirements, result, staff, prefs, standard=standard, settings=settings
        )
    except Exception as exc:
        result.violations = [_checker_unavailable(exc)]
        result.status = SolveStatus.ERROR
        result.messages = [
            f"法令違反の検査中にエラーが発生しました（{exc}）。"
            "適合可否は判定できていないため、出力物を確定的に使わないでください。"
        ]


def _checker_unavailable(exc: BaseException) -> Violation:
    """検査不能を表す BLOCKER 違反を組み立てる。"""
    return Violation(
        severity=ViolationSeverity.BLOCKER,
        code="CHECK_UNAVAILABLE",
        message=(
            "法令違反の検査が完了しなかったため、適合性を確認できていません"
            f"（{type(exc).__name__}: {exc}）。"
        ),
        detail={"error": repr(exc)},
    )


def _extract_vars(problem_vars: object) -> tuple[dict, dict]:
    """内部モデルでも dict でも受け取れるように変数表を取り出す。"""
    if isinstance(problem_vars, _ModelCtx):
        return problem_vars.work, problem_vars.brk
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
    staff_list = list(staff)
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

    break_minutes = (
        int(standard.break_minutes) if standard is not None else _DEFAULT_BREAK_MINUTES
    )
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
    day_open_days = set(days)

    def _cell_ok(sid: str, day: date, i: int) -> bool:
        if not _day_is_workable(by_id[sid], day, settings):
            return False
        if not _slot_is_contractible(by_id[sid], slots[i]):
            return False
        if _is_unavailable(prefs.get(sid), day, slots[i]):
            return False
        return True

    def _day_ok(sid: str, day: date) -> bool:
        contract = by_id[sid].contract
        if len(worked_days[sid] & day_open_days) >= contract.max_weekly_days > 0:
            return False
        prev = day - timedelta(days=1)
        if prev in worked_days[sid]:
            streak = _consecutive_streak(worked_days[sid], prev)
            if streak >= contract.max_consecutive_days:
                return False
            last = worked_days[sid] and max(
                d for d in worked_days[sid] if d < day
            )
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
        """idx を含む連続在勤ブロックを、必要人数の多い時間帯へ伸ばして積む。

        必要人数の薄い時間帯に勤務時間を浪費するため、隣接した時間帯の
        必要人数を比較して多いほうを優先して伸ばす。
        1日上限（cap）に達した時点で止め、勤務ブロックは必ず連続に保つ。
        """
        row = cells[(sid, day)]
        cap = _daily_cap_minutes(by_id[sid].contract)
        used = sum(
            slots[i].minutes for i in range(n)
            if row[i] in (CellState.WORK, CellState.BREAK)
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
                # 日上限に到達しており、この時間帯は伸ばせない。
                # 局所変数を False にするだけではループ先頭の再計算で True に戻り
                # 進捗なしで永久に continue していた（無限ループ）。
                # 「伸ばせなかった時間帯」を集合に記録して恒久的に除外する。
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
            1
            for other in order
            for i in range(n)
            if cells[(other, day)][i] is CellState.BREAK
        )
        cap = max(1, int(_BREAK_CONCURRENT_SHARE * on_duty)) + break_slots - 1
        if concurrent >= cap:
            return
        span = len(duty) - break_slots
        pos = 1 + (len(order) + order.index(sid) * stagger) % max(1, span)
        pos = min(max(0, pos), max(0, len(duty) - break_slots))
        for k in range(pos, min(pos + break_slots, n)):
            if row[k] is CellState.WORK:
                row[k] = CellState.BREAK

    for day in days:
        keys = [i for i in range(n) if need_all.get((day, i), 0) > 0]
        keys.sort(key=lambda i: (-need_all[(day, i)], i))
        for idx in keys:
            for _ in range(need_all[(day, idx)] + need_q[(day, idx)]):
                cur_all = sum(
                    1 for sid in order if cells[(sid, day)][idx] is CellState.WORK
                )
                cur_q = sum(
                    1
                    for sid in order
                    if cells[(sid, day)][idx] is CellState.WORK and by_id[sid].is_qualified
                )
                short_all = need_all[(day, idx)] - cur_all
                short_q = need_q[(day, idx)] - cur_q
                if short_all <= 0 and short_q <= 0:
                    break
                want_qualified = short_q > 0
                cands = [
                    sid
                    for sid in order
                    if cells[(sid, day)][idx] is CellState.OFF
                    and _cell_ok(sid, day, idx)
                    and _day_ok(sid, day)
                    and (by_id[sid].is_qualified or not want_qualified)
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

    for (sid, day, label), state in fixed.items():
        if (sid, day) not in cells:
            continue
        try:
            idx = slots.index(_slot_by_label(slots, label))
        except KeyError:
            continue
        cells[(sid, day)][idx] = state

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
        messages=["貪欲法による暫定シフトを生成しました（最適解ではありません）。"],
        stats=stats,
    )
    try:
        from shiftai import gap_analysis

        result.violations = gap_analysis.check_violations(
            requirements, result, staff_list, prefs, standard=standard, settings=settings
        )
    except Exception as exc:
        # 検査不能を「違反 0 件」で済ませない（_attach_violations と同じ方針）
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
    for a in result.assignments:
        if a.staff_id in totals and a.state is CellState.WORK:
            totals[a.staff_id] += a.slot.hours
    return totals


def staff_shift_count(result: SolveResult) -> dict[str, int]:
    """職員ごとの勤務日数。"""
    days_by_staff: dict[str, set[date]] = {}
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
