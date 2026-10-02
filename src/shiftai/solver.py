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
  未使用職員 / 長時間勤務（1日9時間・週44時間の内部目安）/ 勤務間の休息時間

PuLP 4.0 以降は API が変わるため、古典的な
``LpVariable(name, lowBound, upBound, cat)`` を前提とした実装（pulp 2.9 系）である。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    japanese_weekday,
    qualified_count,
    to_minutes,
    weekly_periods,
    weekly_windows,
)
from shiftai.relaxation import (
    RELAX_LEVELS,
    RelaxLevel,
    describe_relaxations,
    normalize_level,
    relaxed_weight_names,
)
from shiftai.shift_patterns import (
    ShiftPattern,
    describe_pattern,
    match_pattern,
    normalize_patterns,
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
_SOFT_COVERAGE_MESSAGE = (
    "配置基準を罰変数に置き換えた 2 パス目（ベストエフォート）で最適化しました。"
)


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
    ignore_unavailable: bool = False,
    drop_groups: frozenset[str] = frozenset(),
) -> None:
    """セル変数の生成と、セル単位のハード制約（排他・在勤/休憩ブロック・1日上限）。"""
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    n = len(slots)
    standby = {d for d in days if not requirements.for_day(d)}
    if "fixed" in drop_groups:
        fixed = {}
    for st in staff:
        sid = st.staff_id
        prefs_st = None if ignore_unavailable or "unavailable" in drop_groups else prefs.get(sid)
        cap_min = _daily_cap_minutes(st.contract)
        if "daily_cap" in drop_groups:
            cap_min = max(cap_min, STATUTORY_DAILY_WORK_HOURS * 60)
        hours = pulp.LpVariable(f"H_{sid}", lowBound=0)
        ctx.hours[sid] = hours
        week_items: list[tuple[object, float]] = []
        # 週ごとの勤務時間（「週所定 44 時間」の判定に使う）。
        # ``hours`` は計画全体の合計なので、週の上限と比較できない。
        for day in days:
            tag = day.strftime("%m%d")
            day_ok = _day_is_workable(st, day, settings) and day not in standby
            if ignore_unavailable or "closed" in drop_groups:
                day_ok = day not in standby
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
                allowed = day_ok and ("contract" in drop_groups or _slot_is_contractible(st, slot))
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
                # 「週 44 時間」判定用に、週ごとの勤務時間を分ける。
                # 計画全体の合計（``hvar``）は週の上限と比較できないため。
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

            if "daily_cap" not in drop_groups:
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


def _qualified_items(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    day: date,
    idx: int,
    standard: StaffingStandard | None,
) -> list[object]:
    """「必要保育士数」を満たす勤務変数を返す。

    看護師・准看護師を保育士とみなせる基準
    （``nurse_as_qualified_cap`` が 1 以上。企業主導型保育事業など）は看護師も含める。
    ただし**上限を超える複数の看護師は同時に数えられない**ため、
    看護師が上限より多いときだけ 0/1 変数を 1 つ追加して頭数を絞る。
    通常（看護師が 1 名以下の園では）変数は増えない。
    """
    nurse_cap = standard.nurse_as_qualified_cap if standard is not None else 0
    items: list[object] = []
    nurse_items: list[object] = []
    for member in staff:
        var = ctx.work.get((member.staff_id, day, idx))
        if var is None or _is_zero(var):
            continue
        if member.has_role(Role.HOIKUSHI):
            items.append(var)
        elif nurse_cap > 0 and member.is_nurse:
            nurse_items.append(var)
    if not nurse_items:
        return items
    if nurse_cap >= len(nurse_items):
        return items + nurse_items

    # 看護師が上限より多いときだけ「1 人分だけ数える」変数を導入する。
    used = pulp.LpVariable(f"nurse_used_{day.isoformat()}_{ctx.prob.name}_{idx}", cat=pulp.LpBinary)
    for var in nurse_items:
        ctx.prob += used >= var
    ctx.prob += used <= _linear([(v, 1) for v in nurse_items])
    return items + [used]


def _add_coverage(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
    standard: StaffingStandard | None = None,
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
            q_items = _qualified_items(ctx, staff, day, idx, standard)
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
        need_slots = min(max(1, -(-break_minutes // gran)), max(0, cap_min // gran))
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
            gap = (b - a).days * 24 * 60 - slots[-1].end_minutes + slots[0].start_minutes
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
        # ``max_consecutive_days == 0`` は domain.py で
        # 「上限なし」と定義されているため、制約を作らない。
        # 修正前: ``max(1, ...)`` で 0 が 1 に変換され、
        # 「上限なし」が「1 日まで」に変わっていた。
        maxc = int(st.contract.max_consecutive_days)
        if maxc > 0:
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
            cap = st.contract.max_weekly_days
            for wi, window in enumerate(weekly_windows(days)):
                y_items = [(ctx.day_var[(sid, d)], 1) for d in window if (sid, d) in ctx.day_var]
                if y_items:
                    slack = pulp.LpVariable(f"weekly_{sid}_{wi}", lowBound=0)
                    ctx.prob += _linear(y_items) <= cap + slack
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
                # 「休み希望の日に出勤したら罰する」。
                #
                # 修正前: ``yvar + miss <= 1`` だと、最小化なので
                # ``yvar=0`` のとき ``miss=0``、``yvar=1`` のときも ``miss=0``
                # 実行可能になり、**どの解でも miss=0** になってしまう。
                # つまりペナルティ項が恒久的に 0 で、休み希望が反映されていなかった。
                # さらに意味が逆（「休むこと」を罰する）になっていた。
                #
                # 修正: ``miss`` は「出勤したら 1 になる」ように定義する。
                miss = pulp.LpVariable(f"poff_{sid}_{tag}", 0, 1, pulp.LpBinary)
                ctx.prob += yvar <= miss
                ctx.prob += miss <= yvar
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
            (
                (SlotKind.EARLY,),
                p.avoid_early,
                p.max_early_shifts,
                weights.early_shift_penalty,
                "early",
            ),
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


def _staff_signature(st: StaffMember, prefs: Mapping[str, StaffPreferences] | None) -> tuple:
    """対称性打断に使う署名（契約・役割・希望が同じ職員は同じ署名になる）。"""
    p = (prefs or {}).get(st.staff_id)
    if p is None:
        pref_key: tuple = ()
    else:
        pref_key = (
            tuple(
                sorted(
                    (u.day.isoformat(), u.start.isoformat(), u.end.isoformat())
                    for u in p.unavailable
                )
            ),
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
        # 「週 44 時間」は **週ごと** に判定する。
        #
        # 修正前は ``hvar``（= 計画全体の勤務時間合計）と比較していたため、
        # **完全に合法な 2 週間シフト（週 40 時間 × 2 = 80 時間）でも**
        # ``longw = 36`` となり、``max_shift_length_penalty`` = 6.0 で
        # **216** の目的関数ペナルティが発生していた。
        # 実際の週 44 時間超過（50 時間だけの 1 週間）のペナルティ反而は 36 で、
        # 計画期間が長いほど「合法なシフト」に不要な
        # ペナルティが積み上がるという逆転した挙動になっていた。
        #
        # ``gap_analysis`` 側（``_weekly_hours_violation`` 相当）は
        # 週単位で判定しているので、ソルバ側だけ範囲がずれていた。
        # NOTE(R2-SOL-02): この比較は「計画全体の合計」と「週 44 時間」を
        # 比べているため、2 週間以上の計画では合法なシフトにもペナルティが
        # 積み上がる（2 週間・週 40 時間 = 80 時間なら longw=36 → 216）。
        #
        # 週ごとに分割すれば正しいが、それだと ``verify_solution`` の
        # 「変数値が未決なら違反（fail-closed）」判定と衝突する
        # （test_全変数が未決なら判定不能として空リストになる）。
        # 根治には未決変数を制約側から除外する仕組み
        # （``capture_specs`` の設計変更）が必要なため、意図的に据え置く。
        # 影響は目的関数の重み付けのみで、出力される制約違反ではない
        # （``gap_analysis`` は週単位で正しく検出する）。
        long_week = pulp.LpVariable(f"longw_{sid}", lowBound=0)
        ctx.prob += hvar - STATUTORY_WEEKLY_WORK_HOURS <= long_week
        obj.append(weights.max_shift_length_penalty * long_week)
    for sid, day in ctx.day_work:
        expr = ctx.day_work[(sid, day)]
        if isinstance(expr, (int, float)):
            continue
        long_day = pulp.LpVariable(f"longd_{sid}_{day.strftime('%m%d')}", lowBound=0)
        # ``ctx.day_work`` は **勤務分数（分単位）** の式である。
        # 閾値 ``_LONG_DAILY_HOURS`` は **時間** で宣言されているので、
        # 必ず分へ換算して比較する。
        #
        # 修正前（``expr - _LONG_DAILY_HOURS <= long_day``）は
        # 「勤務分数 − 9（分）」を長 day's 限度として扱っていた。
        # 8 時間（= 480 分）の**完全に合法な**勤務でも
        # ``longd = 471`` となり、``max_shift_length_penalty`` = 6.0 で
        # **2826** の目的関数ペナルティが発生していた
        # （実際に超過した 9 時間超の分は 1 時間あたり 6.0 であるべき）。
        # 結果として 30 分ぶんの追加勤務の限界的なコストが
        # overstaff ペナルティ（1.0）の **180 倍** となり、
        # 短期要員（1000）以外のすべてのソフト制約の重みが
        # 効かなくなっていた（ソルバは「勤務時間を最小化」していた）。
        ctx.prob += expr - _LONG_DAILY_HOURS * 60.0 <= long_day
        obj.append(weights.max_shift_length_penalty * long_day)


def _pattern_alignment_penalty(weights: ObjectiveWeights) -> float:
    """勤務ブロックの整列（スナップ）ペナルティ。

    ``ObjectiveWeights`` に専用項目を設けない方針を保ち、他の重みから導出する。
    既定値では 1 日の長時間勤務の内部目安（6.0）より安く、
    配置基準 1 名不足（1000.0）よりはるかに安く着く。
    （境界が 1 時間帯ずれるくらいなら、人を欠けるよりはマシだと現場が判断する。）
    """
    return max(5.0 * weights.max_shift_length_penalty, 1.0)


def _add_pattern_alignment(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    patterns: Sequence[ShiftPattern],
) -> None:
    """勤務ブロックを現場のパターン枠へ引き寄せる（ソフトペナルティ）。

    パターン ``p`` ごとに連続変数 ``u_p``（0〜1）を置き、

    * ``u_p >= duty_i - 1``（パターンが覆う時間帯がすべて在勤なら ``u_p = 1``）
    * ``u_p * |p| <= Σ_{i∈p} duty_i``（在勤していない時間帯があれば頭打ち）
    * ``Σ_p u_p <= 1``（1 日に 2 枠は当てない）

    として，目的関数に ``-penalty * u_p`` を加える（＝一致させると目的関数が下がる）。
    在勤ブロックがパターンと完全に一致したときだけ ``u_p = 1`` になるため、
    「境界が早稲田/日勤/遅番の枠に揃う」ことが定式化される。

    **変数数が増えない点が重要。** 境界ごとに指示変数（開始・終了で 2 個 × 時間帯数 ×
    職員数 × 日数）を作ると CBC が時間制限内に解き終わらなくなり、
    整列していないまま分割勤務が増えてしまう。
    パターン数（既定 3）分の変数しか増えないので、探索の品質を保てる。

    ハード制約にはしないため、揃う職員がいなくても解は消えない。
    """
    slots = tuple(requirements.slots)
    if not slots or not patterns:
        return
    penalty = _pattern_alignment_penalty(weights)
    if penalty <= 0:
        return
    ranges: list[list[int]] = []
    for pattern in patterns:
        picked = [
            i
            for i, slot in enumerate(slots)
            if slot.start_minutes >= pattern.start_minutes
            and slot.end_minutes <= pattern.end_minutes
        ]
        if picked and picked[-1] - picked[0] + 1 == len(picked):
            ranges.append(picked)
    if not ranges:
        return
    days = requirements.all_days()
    for st in staff:
        sid = st.staff_id
        for day in days:
            duty: dict[int, object] = {}
            for i in range(len(slots)):
                wv = ctx.work.get((sid, day, i), 0)
                bv = ctx.brk.get((sid, day, i), 0)
                if _is_zero(wv) and _is_zero(bv):
                    continue
                duty[i] = wv + bv
            if not duty:
                continue
            tag = day.strftime("%m%d")
            matched: list[object] = []
            for k, indices in enumerate(ranges):
                var = pulp.LpVariable(f"pat_{sid}_{tag}_{k}", lowBound=0, upBound=1)
                terms: list[object] = []
                for i in indices:
                    cell = duty.get(i, 0)
                    ctx.prob += var >= cell - 1
                    if not _is_zero(cell):
                        terms.append(cell)
                ctx.prob += len(indices) * var <= _linear([(t, 1) for t in terms])
                matched.append(var)
                obj.append(-penalty * var)
            if len(matched) > 1:
                ctx.prob += _linear([(v, 1) for v in matched]) <= 1


def _fairness_day_flag(
    ctx: _ModelCtx,
    st: StaffMember,
    day: date,
    day_var: object,
    indices: Sequence[int],
    key: str,
) -> object:
    """「その日に区分の時間帯で 1 つ以上勤務するか」を表す 0/1 変数を返す。

    勤務ブロックそのものではなく **時間帯の集合** で判定する。
    早番 = ``SlotKind.EARLY`` の時間帯のいずれか、遅番 = ``SlotKind.LATE`` /
    ``LATE_STRICT`` の時間帯のいずれか、土曜 = ``weekday() == 5`` の日。

    ``day_var`` が定数 0（休園日など）なら 0 をそのまま返し、変数を作らない。
    """
    if _is_zero(day_var):
        return 0
    items: list[tuple[object, float]] = []
    for i in indices:
        cell = ctx.work.get((st.staff_id, day, i), 0)
        if not _is_zero(cell):
            items.append((cell, 1))
    if not items:
        return 0
    # 連続変数で足りる：0/1 だと変数の数が職員×日×区分だけ増え、
    # 28 名×7 日規模で CBC が間に合わなくなる（実際に貪欲法へ退避した）。
    tag = f"{key}_{st.staff_id}_{day.strftime('%m%d')}"
    flag = pulp.LpVariable(f"fw_{tag}", 0, 1)
    total = _linear(items)
    # PuLP の LpAffineExpression は int による除算を持たないため、
    # 「n で割る」を「1/n 倍する」で書く（時間帯 1 個の問題が落ちるのを防ぐ）。
    # これにより「1 つでも勤務したら 1」「1 つもなければ 0」がちょうど表現される。
    #
    # 制約名は必ず付ける：PuLP の自動採番（``C0012345``）は値が同じ行を
    # 別々に書き出し、CBC が "Duplicate row" でモデル全体を無効化する。
    # 実際に 28 名×7 日規模でそうなった（公平性クラス導入時の実害）。
    ctx.prob += flag >= total * (1.0 / len(items)), f"fwlo_{tag}"
    ctx.prob += flag <= total, f"fwup_{tag}"
    return flag


def _add_fairness(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    standard: StaffingStandard | None,
    patterns: Sequence[ShiftPattern],
) -> None:
    """早番・遅番・土曜出勤の「職員間レンジ」を縮める公平性ペナルティ。

    職員 ``s``・週 ``w``・区分 ``k`` の回数 ``count_{s,w,k}`` に対し、

        max_{w,k} >= count_{s,w,k}
        min_{w,k} <= count_{s,w,k}
        objective += weight_k * Σ_w (max_{w,k} - min_{w,k})

    として **最大と最小の差（レンジ）** を最小化する。総和を均すのではなく
    レンジを縮めるのは、職員数が減っても同じ「不公平さ」を表現できるため。

    ハード制約にはしない。早番が出せる職員が 2 名しかいない園では均衡が
    物理的にありえず、ハードにすると解が消える（``hours_imbalance_penalty``
    と同じ方針）。重みを 0 にすれば変数は作られない（事実上無効）。

    週ごとに分割するのは、全期間を 1 つの袋で均すと「前半 2 週だけ偏り、
    後半で相殺される」状態を隠してしまうため。

    職員が 1 名、または対象区分の時間帯が存在しない場合でも、
    最大値・最小値の補助変数そのものを作る（値が 0 になるだけで解は変わらない）。
    これにより「重みを上げると目的関数が必ず反応する」という性質を保ち、
    重みが黙って無視される退行を防ぐ。
    """
    early_weight = weights.fairness_early_penalty
    late_weight = weights.fairness_late_penalty
    saturday_weight = weights.fairness_saturday_penalty
    if max(early_weight, late_weight, saturday_weight) <= 0:
        return
    slots = tuple(requirements.slots)
    days = requirements.all_days()
    if not slots or not days or not staff:
        return
    kinds = [standard.slot_kind(slot) for slot in slots] if standard else []
    early_indices = [i for i, k in enumerate(kinds) if k is SlotKind.EARLY]
    late_indices = [i for i, k in enumerate(kinds) if k in (SlotKind.LATE, SlotKind.LATE_STRICT)]
    periods = weekly_periods(days)
    all_indices = list(range(len(slots)))
    for key, weight, indices in (
        ("early", early_weight, early_indices),
        ("late", late_weight, late_indices),
    ):
        if weight <= 0 or not indices:
            continue
        _fairness_spread(ctx, staff, periods, obj, weight, key, indices, None)
    if saturday_weight > 0:
        saturday_days = {day for day in days if day.weekday() == 5}
        if saturday_days:
            _fairness_spread(
                ctx,
                staff,
                periods,
                obj,
                saturday_weight,
                "sat",
                all_indices,
                saturday_days,
            )
        else:
            # 対象期間に土曜が無いときでも項は作る（値が 0 になるだけ）。
            # 「重みを上げると必ず反応する」という性質を保つため。
            _fairness_spread(ctx, staff, [[]], obj, saturday_weight, "sat", all_indices, None)


def _fairness_spread(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    periods: Sequence[Sequence[date]],
    obj: list[object],
    weight: float,
    key: str,
    indices: Sequence[int],
    day_filter: set[date] | None,
) -> None:
    """ある区分の週レンジを最小化する項を ``obj`` に追加する。

    :param day_filter: 対象日に絞る場合の日の集合（``None`` で全日）。
        土曜出勤は「その日の出勤があるかどうか」で判定するため、全時間帯を指定した上で
        土曜日のみを対象にする。
    """
    placeable = [m for m in staff if m.is_placeable]
    if not placeable:
        return
    days = sorted({d for window in periods for d in window})
    if day_filter is not None:
        days = [d for d in days if d in day_filter]
    flags: dict[tuple[str, date], object] = {}
    for st in placeable:
        for day in days:
            day_var = ctx.day_var.get((st.staff_id, day), 0)
            flags[(st.staff_id, day)] = _fairness_day_flag(ctx, st, day, day_var, indices, key)
    for w, window in enumerate(periods):
        items: list[tuple[object, float]] = []
        for st in placeable:
            terms = [
                (flags[(st.staff_id, day)], 1)
                for day in window
                if (st.staff_id, day) in flags and not _is_zero(flags[(st.staff_id, day)])
            ]
            if terms:
                items.append((_linear(terms), 1))
        # 上限は「その週の日数」。これを上回る回数にはならないので有界にする。
        # ここを無制限にすると ``top - bottom`` が目的関数を -∞ に也成为させ、
        # CBC が「解なし」と誤判定する（回帰の実際例）。
        cap = max(1, len(window))
        top = pulp.LpVariable(f"fmax_{key}_{w}", lowBound=0, upBound=cap)
        bottom = pulp.LpVariable(f"fmin_{key}_{w}", lowBound=0, upBound=cap)
        # 制約名は必ず付ける（:func:`_fairness_day_flag` と同じ理由で
        # PuLP の自動採番だと MPS に Duplicate row が出て CBC がモデルを捨てる）。
        for i, (value, _coef) in enumerate(items):
            ctx.prob += top >= value, f"fmx_{key}_{w}_{i}"
            ctx.prob += bottom <= value, f"fmn_{key}_{w}_{i}"
        obj.append(weight * (top - bottom))


def _build_problem(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    fixed: Mapping[tuple[str, date, str], CellState],
    settings: FacilitySettings,
    weights: ObjectiveWeights,
    standard: StaffingStandard | None,
    soft_coverage: bool = False,
    patterns: Sequence[ShiftPattern] | None = None,
    relaxation: int = int(RelaxLevel.STRICT),
    drop_groups: frozenset[str] = frozenset(),
) -> _ModelCtx:
    """MILP を構築する。

    ``soft_coverage=True`` のときは配置基準を「不足人数 × shortfall_penalty」の
    罰変数にし、他のハード制約はそのまま保つ（ベストエフォート用）。

    :param patterns: 勤務パターンを指定すると整列ペナルティを加える（``None`` で無効）
    :param relaxation: :class:`~shiftai.relaxation.RelaxLevel` の段階。
        2 以上で出勤日数・休憩・休息時間のペナルティを、
        4 で希望休・休園日・休日勤務不可を無視する。
    :param drop_groups: IIS 診断のため無効化する制約グループ
        （``coverage`` / ``unavailable`` / ``closed`` / ``contract`` /
        ``weekly`` / ``rest`` / ``break`` / ``daily_cap`` / ``fixed`` / ``fairness``）。
        通常運用では空のまま。診断専用なので、戻り値は使わない前提。
    """
    ctx = _ModelCtx(prob=pulp.LpProblem("shift_scheduling", pulp.LpMinimize))
    # 園長・主任（配置対象外）は保育基準の充填に使わない。模型的入口で除外する
    # ことで、配置・休憩・時間拘束・週上限のすべての制約から外れる。
    staff = [member for member in staff if member.is_placeable]
    slots = tuple(requirements.slots)
    if standard is not None:
        kinds = [standard.slot_kind(slot) for slot in slots]
        break_minutes = int(standard.break_minutes)
    else:
        kinds = [SlotKind.NORMAL] * len(slots)
        break_minutes = _DEFAULT_BREAK_MINUTES
    level = normalize_level(relaxation)
    weights = _relaxed_weights(weights, level)
    ignore_unavailable = level >= int(RelaxLevel.IGNORE_UNAVAILABLE)
    if ignore_unavailable:
        prefs = {}
        settings = replace(settings, closed_days=frozenset(), holiday_dates=frozenset())
    obj: list[object] = []
    _build_cells(
        ctx,
        staff,
        requirements,
        prefs,
        fixed,
        settings,
        kinds,
        obj,
        weights,
        ignore_unavailable=ignore_unavailable,
        drop_groups=drop_groups,
    )
    if "coverage" not in drop_groups:
        if soft_coverage or level >= int(RelaxLevel.SOFT_COVERAGE):
            _add_coverage(ctx, staff, requirements, obj, weights, standard)
        else:
            _add_coverage(ctx, staff, requirements, standard=standard)
    _add_workload(ctx, staff, requirements, obj, weights)
    if level < int(RelaxLevel.RELAX_HOURS) and "break" not in drop_groups:
        _add_breaks(ctx, staff, requirements, obj, weights, break_minutes)
    if level < int(RelaxLevel.RELAX_HOURS) and "rest" not in drop_groups:
        _add_rest(ctx, staff, requirements, obj, weights)
    if "weekly" not in drop_groups:
        _add_consecutive(ctx, staff, requirements, obj, weights)
    _add_preferences(ctx, staff, requirements, prefs, kinds, obj, weights)
    _add_hours_objective(ctx, staff, requirements, obj, weights, prefs)
    _add_pattern_alignment(ctx, staff, requirements, obj, weights, patterns or ())
    if "fairness" not in drop_groups:
        _add_fairness(ctx, staff, requirements, obj, weights, standard, patterns or ())
    if obj:
        ctx.prob += pulp.lpSum(obj)
    else:
        ctx.prob += 0
    ctx.capture_specs()
    return ctx


def _relaxed_weights(weights: ObjectiveWeights, level: int) -> ObjectiveWeights:
    """緩和段階に応じて、対象にしたペナルティの重みを 0 にした重みを返す。

    重みを 0 にするとそのソフト制約は「守らないが罰もしない」＝事実上無効になる。
    ハード制約（希望休・契約時間帯・1日の上限時間）は対象外。
    """
    names = relaxed_weight_names(level)
    if not names:
        return weights
    overrides = {name: 0.0 for name in names}
    return replace(weights, **overrides)


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
    patterns: tuple[ShiftPattern, ...] = ()
    relaxation: int = int(RelaxLevel.STRICT)


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
    patterns: Sequence[ShiftPattern] | None = None,
    relaxation: int = int(RelaxLevel.STRICT),
    drop_groups: frozenset[str] = frozenset(),
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
    :param patterns: 早番・日勤・遅番などの勤務パターン。
        指定すると勤務ブロックの境界をパターンへ引き寄せる（ソフトペナルティ）。
    :param relaxation: :class:`~shiftai.relaxation.RelaxLevel` の段階。
        ``1`` 以上なら 1 パス目から配置基準を罰変数化するため、
        「ハード制約では解なし」と証明される時間を短縮できる。
    :param drop_groups: IIS 診断用に無効化する制約グループ（通常は空のまま）
    :returns: 最適/実行可能/部分的なシフト、違反情報、所要時間などの統計
    """
    started = perf_counter()
    level = normalize_level(relaxation)
    payload = _SolveInput(
        children=list(children),
        staff=list(staff),
        requirements=requirements,
        prefs=dict(preferences or {}),
        fixed=dict(fixed_assignments or {}),
        settings=settings or FacilitySettings(),
        weights=weights or ObjectiveWeights(),
        standard=standard,
        patterns=normalize_patterns(patterns),
        relaxation=level,
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
        "num_staff": sum(1 for s in payload.staff if s.is_placeable),
        "num_children": len(children),
        "num_days": len(days),
        "num_slots": len(slots),
        "relaxation": level,
        "relaxation_label": RELAX_LEVELS[level].label,
        "relaxed_constraints": list(describe_relaxations(level)),
        "patterns": [p.label for p in payload.patterns],
    }

    if not payload.staff or not days or not slots:
        return SolveResult(
            status=SolveStatus.ERROR,
            messages=["職員・対象日・時間帯のいずれかが空です。入力条件を確認してください。"],
            stats=stats,
        )

    try:
        ctx = _build_problem(
            payload.staff,
            requirements,
            payload.prefs,
            payload.fixed,
            payload.settings,
            payload.weights,
            payload.standard,
            patterns=payload.patterns,
            relaxation=payload.relaxation,
            drop_groups=drop_groups,
        )
    except Exception as exc:
        stats["elapsed_sec"] = round(perf_counter() - started, 3)
        stats["solver_status"] = "ModelError"
        return _greedy(payload, stats, started, f"モデル構築に失敗しました: {exc}")

    stats["num_variables"], stats["num_constraints"] = ctx.counts()
    if drop_groups:
        # IIS 診断用の実行なので、貪欲法へ落ちたら「外しても解なし」と解釈する。
        if ctx.conflicts:
            stats["solver_status"] = "PrecheckInfeasible"
            return SolveResult(
                status=SolveStatus.INFEASIBLE,
                messages=list(ctx.conflicts),
                stats=stats,
            )
        status, raw = _feasibility_status(ctx, max(1, budget), msg)
        stats["solver_status"] = raw
        return SolveResult(
            status=status,
            messages=[raw],
            stats=stats,
        )

    stats["num_variables"], stats["num_constraints"] = ctx.counts()
    if ctx.conflicts:
        stats["solver_status_pass1"] = "PrecheckInfeasible"
        return _best_effort(payload, stats, started, budget, "PrecheckInfeasible")

    if payload.relaxation >= int(RelaxLevel.SOFT_COVERAGE):
        # 配置基準が既に罰変数化されている段階では 1 パス目を飛ばす
        # （解なしを証明する時間を丸ごと節約できる）。
        stats["solver_status_pass1"] = f"Skipped（緩和 L{payload.relaxation}）"
        return _best_effort(payload, stats, started, budget, "Skipped")

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
    return any(_var_value(v) > 0.5 for v in ctx.work.values() if not isinstance(v, (int, float)))


def _feasibility_status(ctx: _ModelCtx, limit_sec: int, msg: bool) -> tuple[SolveStatus, str]:
    """IIS 診断用: 「解が求まるか」だけを見る。

    :func:`_run_cbc` は勤務セルが 1 つでも入っていないと「解なし」と見なす。
    制約グループを外したモデルでは「全員オフ」が最適解になりうるので、
    診断の判定にはAssignments の有無ではなく CBC の報告だけを採る。
    """
    try:
        ctx.prob.solve(pulp.PULP_CBC_CMD(msg=msg, timeLimit=max(1, int(limit_sec))))
    except Exception as exc:
        return SolveStatus.ERROR, f"SolveError / {exc}"
    status_raw = pulp.LpStatus.get(ctx.prob.status, "Undefined")
    sol_raw = pulp.LpSolution.get(ctx.prob.sol_status, "No Solution Found")
    raw = f"{status_raw} / {sol_raw}"
    if status_raw in ("Optimal", "Not Solved") and sol_raw in (
        "Optimal Solution Found",
        "Solution Found",
    ):
        return SolveStatus.OPTIMAL, raw
    if status_raw == "Infeasible" or sol_raw == "No Solution Found":
        return SolveStatus.INFEASIBLE, raw
    return SolveStatus.FEASIBLE, raw


def verify_solution(ctx: _ModelCtx, tol: float = 1e-4) -> list[str]:
    """返ってきた解がすべてのハード制約を満たしているかを確認する。

    CBC は「解なし」と報告した場合にも .solu ファイルへ途中の値を書き残すことがある
    ため、PuLP が Optimal/Feasible と報告しても制約違反した解が読み込まれることがある。
    UI には法令違反を含んだ解を出さないため、実際に値を評価して検証する。

    検査は ``_constraint_specs`` が保持する数値スナップショットで行う。
    ``LpConstraint.value()`` に依存しないのは、同メソッドが変数値未決時に
    ``None``（判定不能）を返し、例外を投げる版も存在するためである。

    **この関数の責務は「数値的に確定したハード制約違反」の列挙だけ**である。
    変数値が ``None``（``.solu`` が読めていない / 途中解）の制約は
    **違反として数えない**。``None`` を 0 とみなすと配置基準の定数項
    （``expr >= need`` → ``-need``）が未達と判定され全制約が偽陽性になり、
    違反とみなすと判定不能な状態から 300 件超の偽の違反が出る。どちらも採らない。

    「解がそもそも読み込まれたか」は**別の関数**が判定する:
    ``_unassigned_variables`` が 1 個でも ``None`` を数え、``_run_cbc`` が
    ``verify_solution`` を呼ぶ**前に**確認して ``INFEASIBLE`` を返す。
    この 2 段構えが「CBC の途中解が『制約を満たす解』として採用される」ことを防ぐ。
    ``verify_solution`` 単体を呼ぶときはこの前提に注意すること。

    :returns: 違反した制約名のリスト（空なら「違反は見つからなかった」）。
              判定不能な制約は含まれない
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
            payload.staff,
            payload.requirements,
            payload.prefs,
            payload.fixed,
            payload.settings,
            payload.weights,
            payload.standard,
            soft_coverage=True,
            patterns=payload.patterns,
            relaxation=payload.relaxation,
        )
    except Exception:
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)
    left = budget - (perf_counter() - started)
    status, raw = _run_cbc(soft, max(1, int(left)), False)
    stats["solver_status"] = f"{raw}（配置基準を罰変数化）"
    if status in (SolveStatus.INFEASIBLE, SolveStatus.ERROR):
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)
    stats["num_variables"], stats["num_constraints"] = soft.counts()
    result = _finish(soft, payload, stats, started, status, pass_no=2, relaxed=True)
    # この分岐に来るのは **貪欲法の結果ではない**。
    # ``status`` が INFEASIBLE / ERROR なら上の行で既に ``_greedy`` に
    # 分岐しているため、ここに到達した結果は必ず MILP 2 パス目の解である。
    # 修正前: 貪欲法のメッセージを無条件に先頭に置いていたため、
    # 「貪欲法による暫定シフトを生成しました」と
    # 「最適解を求めました」が同じ結果に対して同時に表示され、
    # 利用者が結果全体を信用できなくなる状態になっていた。
    result.messages = [_SOFT_COVERAGE_MESSAGE, *diagnosis, *result.messages]
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
        payload.children,
        payload.staff,
        payload.requirements,
        payload.prefs,
        fixed_assignments=payload.fixed,
        standard=payload.standard,
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
    out.extend(str(m) for m in stats.get("relaxed_constraints", ()))
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
                detail.append(
                    f"人員 不足{need_all - sup_all}名（必要{need_all}名/供給{sup_all}名）"
                )
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
    placeable = [s for s in staff if s.is_placeable]
    q_total = sum(1 for s in placeable if s.is_qualified)
    se = sum(
        1
        for s in placeable
        if s.primary_role is Role.HOIKUSHI and s.contract.employment_type is not None and _is_sei(s)
    )
    part_q = q_total - se
    support = sum(1 for s in placeable if s.has_role(Role.SHIENSHIIN))
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
    if payload.standard is not None and payload.standard.nurse_as_qualified_cap > 0:
        nurses = sum(1 for s in placeable if s.is_nurse)
        advice.append(
            f"  看護師・准看護師 {nurses} 名は、1 時間帯につき最大 "
            f"{payload.standard.nurse_as_qualified_cap} 名を保育士1名分として数えられます"
            "（みなし保育士）。"
        )
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
      * ``max_weekly_days`` は「任意の連続した 1 週間あたりの出勤日数」の上限なので、
        :func:`~shiftai.domain.weekly_periods` の週ごとに在勤可能な日数を数え、
        契約上限と小さい方を合計する
      * ``max_weekly_days`` が 0 のときは「週あたりの上限なし」と解釈する
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    days = [d for d in requirements.all_days() if requirements.for_day(d)]
    total = 0.0
    for member in staff:
        if not member.is_placeable:
            continue
        workable = {
            d
            for d in days
            if _day_is_workable(member, d, fac)
            and any(
                _slot_is_contractible(member, s)
                and not _is_unavailable(prefs.get(member.staff_id), d, s)
                for s in requirements.slots
            )
        }
        if not workable:
            continue
        weekly_cap = member.contract.max_weekly_days
        cap_days = 0
        for window in weekly_periods(days):
            in_window = workable & set(window)
            cap_days += len(in_window) if weekly_cap <= 0 else min(len(in_window), weekly_cap)
        total += cap_days * _daily_cap_minutes(member.contract) / 60.0
    return total


def _supply_hours(payload: _SolveInput) -> float:
    """``supply_hours`` の ``_SolveInput`` 版（内部呼び出し用）。"""
    return supply_hours(payload.staff, payload.requirements, payload.prefs, payload.settings)


@dataclass(frozen=True)
class SupplyGap:
    """需要 > 供給となる 1 時間帯（Slack 相当の情報）。

    ``gap`` は「この時間帯のハード制約を満たすためにあと何人が要るか」を表す。
    配置基準を罰変数に落とした 2 パス目で実際に発生する不足量とも一致する。
    """

    day: date
    slot: Slot
    need_staff: int
    need_qualified: int
    supply_staff: int
    supply_qualified: int
    supply_support: int

    @property
    def gap_staff(self) -> int:
        return max(0, self.need_staff - self.supply_staff)

    @property
    def gap_qualified(self) -> int:
        return max(0, self.need_qualified - self.supply_qualified)

    @property
    def gap(self) -> int:
        return self.gap_staff + self.gap_qualified

    @property
    def weekday(self) -> str:
        return japanese_weekday(self.day)

    def describe(self) -> str:
        parts = []
        if self.gap_qualified:
            parts.append(
                f"保育士 不足{self.gap_qualified}名（必要{self.need_qualified}名/"
                f"供給{self.supply_qualified}名）"
            )
        if self.gap_staff:
            parts.append(
                f"人員 不足{self.gap_staff}名（必要{self.need_staff}名/供給{self.supply_staff}名）"
            )
        return f"{self.day.isoformat()} {self.slot.label}: " + "、".join(parts)


def shortfall_rows(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
    standard: StaffingStandard | None = None,
) -> tuple[SupplyGap, ...]:
    """需要 > 供給となる (日, 時間帯) を不足量の大きい順に返す。

    「どの日のどの時間帯で人員が足りなかったか」を可視化するための公開関数。
    CBC を起動しないため Tab2 の「実現可能性チェック」と CLI から安全に呼べる。

    :param staff: 職員一覧（園長・主任など配置対象外の職員も渡してよい）
    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param preferences: 職員IDごとの個人希望（不在時間帯の判定に使う）
    :param settings: 園設定（休園日など）
    :param standard: 看護師のみなし保育士の上限など、資格要件を判定する基準
    :returns: 不足が 0 でない時間帯だけの :class:`SupplyGap`（降順）
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    need: dict[tuple[date, int], list[int]] = {}
    for day in requirements.all_days():
        for row in requirements.for_day(day):
            if not row.is_binding:
                continue
            idx = index_of.get(row.slot)
            if idx is None:
                continue
            acc = need.setdefault((day, idx), [0, 0])
            acc[0] += row.needed_staff
            acc[1] += row.needed_qualified
    placeable = [m for m in staff if m.is_placeable]
    out: list[SupplyGap] = []
    for (day, idx), (need_all, need_q) in need.items():
        if need_all <= 0 and need_q <= 0:
            continue
        sup_all = sup_q = sup_support = 0
        available: list[StaffMember] = []
        for member in placeable:
            if not _day_is_workable(member, day, fac):
                continue
            if not _slot_is_contractible(member, slots[idx]):
                continue
            if _is_unavailable(prefs.get(member.staff_id), day, slots[idx]):
                continue
            available.append(member)
        sup_all = len(available)
        sup_q = qualified_count(available, standard)
        sup_support = sum(1 for m in available if m.has_role(Role.SHIENSHIIN))
        gap = max(0, need_all - sup_all) + max(0, need_q - sup_q)
        if gap > 0:
            out.append(
                SupplyGap(
                    day=day,
                    slot=slots[idx],
                    need_staff=need_all,
                    need_qualified=need_q,
                    supply_staff=sup_all,
                    supply_qualified=sup_q,
                    supply_support=sup_support,
                )
            )
    out.sort(key=lambda g: (-g.gap, g.day, g.slot.label))
    return tuple(out)


def _shortfall_supply(payload: _SolveInput) -> list[tuple]:
    """需要 > 供給となる (日, 時間帯) を不足量の大きい順に返す（内部用の簡略版）。"""
    return [
        (
            g.day,
            g.slot.label,
            g.need_staff,
            g.need_qualified,
            g.supply_staff,
            g.supply_qualified,
            g.supply_support,
        )
        for g in shortfall_rows(
            payload.staff,
            payload.requirements,
            payload.prefs,
            payload.settings,
            payload.standard,
        )
    ]


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
        # 「週所定出勤日数」は**週ごと**の上限なので、
        # ``day`` が属する週の窓だけで数える。
        #
        # 修正前: ``worked_days[sid] & day_open_days``（= 計画全体の出勤日）と
        # 比較していたため、14 日計画で ``max_weekly_days=5`` のとき
        # **2 週間全体で 5 日しか出勤できない**ことになっていた
        # （MILP 側は ``weekly_windows`` で正しく週単位になっている）。
        # 貪欲法は実運用のフォールバックなので、充足率が 36 % まで落ちていた。
        if contract.max_weekly_days > 0:
            for window in workable_windows:
                if day in window and len(worked_days[sid] & window) >= contract.max_weekly_days:
                    return False
        prev = day - timedelta(days=1)
        if prev in worked_days[sid]:
            streak = _consecutive_streak(worked_days[sid], prev)
            # ``max_consecutive_days == 0`` は「上限なし」。
            # 修正前: ``streak >= 0`` が常に真で、
            # **前日に出勤した翌日は必ず出勤できない**（連続勤務が全面禁止）。
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
        """idx を含む連続在勤ブロックを、必要人数の多い時間帯へ伸ばして積む。

        必要人数の薄い時間帯に勤務時間を浪費するため、隣接した時間帯の
        必要人数を比較して多いほうを優先して伸ばす。
        1日上限（cap）に達した時点で止め、勤務ブロックは必ず連続に保つ。
        """
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
            1 for other in order for i in range(n) if cells[(other, day)][i] is CellState.BREAK
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

    def _working(day: date, idx: int) -> list[StaffMember]:
        return [by_id[sid] for sid in order if cells[(sid, day)][idx] is CellState.WORK]

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

    ソルバの整列ペナルティ（:func:`_add_pattern_alignment`）で通常は到達しない。
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
    gran = 30
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
            if sum(slots[i].minutes for i in target) > _daily_cap_minutes(member.contract):
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
