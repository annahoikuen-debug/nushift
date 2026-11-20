"""MILP モデルの構築部品 (model_builder)。

``shiftai/solver.py`` のモノリスから切り出したモデル構築関数をここに集約する。

責務:
- ``_ModelCtx`` / ``_ConstraintSpec`` — 構築済み MILP と制約スナップショット
- ハード/ソフト制約ヘルパー (``_hard_le`` / ``_hard_ge`` / ``_split_penalty``)
- 各制約ブロックの構築 (``_add_block`` / ``_add_interval_block`` / ``_add_coverage`` 等)
- 問題構築エントリポイント (``_build_problem``)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

import pulp

from shiftai import config
from shiftai.config import (
    STATUTORY_DAILY_WORK_HOURS,
    STATUTORY_WEEKLY_WORK_HOURS,
)
from shiftai.domain import (
    CellState,
    ClassRoom,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    PairingRule,
    RequirementTable,
    Role,
    Slot,
    SlotKind,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    daily_cap_minutes,
    to_minutes,
    weekly_periods,
    weekly_windows,
)
from shiftai.shift_patterns import ShiftPattern

_DAILY_LEGAL_CAP_MIN = int(config.STATUTORY_MAX_DAILY_WORK_HOURS * 60)
_OVERTIME_FACTOR = config.STATUTORY_OVERTIME_MULTIPLIER
_LONG_DAILY_HOURS = config.INTERNAL_DAILY_LONG_HOURS
_BREAK_CONCURRENT_SHARE = config.BREAK_CONCURRENT_SHARE
_UNUSED_HOURS_EPS = 0.25
_DEFAULT_BREAK_MINUTES = config.DEFAULT_BREAK_MINUTES
_DEFAULT_STAGGER_SLOTS = 1
_EPS = config.FLOAT_TOLERANCE
_INFEASIBLE_FALLBACK_MESSAGE = "PuLP では解なし。貪欲法による暫定シフトを生成しました"
_SOFT_COVERAGE_MESSAGE = (
    "配置基準を罰変数に置き換えた 2 パス目（ベストエフォート）で最適化しました。"
)


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


@dataclass(frozen=True)
class _ConstraintSpec:
    """制約 1 本を「sense / 定数項 / 係数」に分解した検査用スナップショット。

    PuLP の ``LpConstraint.value()`` は変数値が未決なら ``None`` を返すため、
    求解後の検証には依存しない。``solver.capture_specs()`` で構築時に収集した
    スナップショットを用いて、制約の充足を再評価する。
    """

    name: str
    sense: int
    constant: float
    items: tuple[tuple[object, float], ...]

    def is_violated(self, tol: float) -> bool:
        """この制約が tol を超えて違反しているか。"""
        # 派生クラス/呼び出し側で変数値を代入して評価する。
        raise NotImplementedError


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
            _ConstraintSpec(
                name=name,
                sense=con.sense,
                constant=float(con.constant),
                items=tuple(con.items()),
            )
            for name, con in self.prob.constraints.items()
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
    """
    return max(
        weights.split_duty_penalty,
        weights.break_conflict_penalty,
        10.0 * weights.overstaff_penalty,
    )


def _add_block(
    ctx: _ModelCtx,
    seq: Mapping[int, object],
    tag: str,
    n: int,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
    max_blocks: int | None = None,
) -> list[object]:
    """列 seq が「高々1つの連続ブロック（または max_blocks 以下）」に近づくよう制約する。

    変数 v_i（0/1）に対し「ブロック開始フラグ」s_i を 1 個ずつ置き
    ``s_i >= v_i - v_{i-1}``（v_{-1} = 0）を課す。s_i は最小値（= ブロックの
    開始数）を取るので、2つ目以降のブロックだけを slack 変数で表現できる。

    :param obj: ``None`` を渡すと**ハード制約**（``Σ s_i <= max_blocks or 1``）になり、
        分割すると構造的に不可能になる。``obj`` を渡すと従来のソフトペナルティ。
    :param max_blocks: 許容する最大ブロック数。3分割禁止時は ``2`` を渡す。
    :returns: 作った ``s_i`` 変数の列（ハード判定の検証に使う）。
    """
    if n < 1:
        return []
    fixed_sum = 0
    terms: list[tuple[object, float]] = []
    starts: list[object] = []
    for i in range(n):
        cur = seq.get(i, 0)
        prev = seq.get(i - 1, 0) if i > 0 else 0
        if _is_zero(cur) and _is_zero(prev):
            fixed_sum += int(cur) - int(prev)
            continue
        s_var = pulp.LpVariable(f"{tag}_s{i}", lowBound=0, upBound=1)
        ctx.prob += s_var >= cur - prev
        terms.append((s_var, 1))
        starts.append(s_var)
    limit_blocks = max_blocks if max_blocks is not None else 1
    if not terms:
        if fixed_sum > limit_blocks:
            if (
                obj is None
                or weights is None
                or (max_blocks is not None and fixed_sum > max_blocks)
            ):
                ctx.conflicts.append(
                    f"{tag}: 勤務が {fixed_sum} ブロックに分かれており上限 {limit_blocks} を超過しています"
                )
            else:
                gap = pulp.LpVariable(f"{tag}_split", lowBound=0)
                ctx.prob += gap >= fixed_sum - 1
                obj.append(_split_penalty(weights) * gap)
        return []
    if obj is None or weights is None:
        _hard_le(ctx, _linear(terms) + fixed_sum, limit_blocks, f"{tag}_oneblock")
        return starts
    slack = pulp.LpVariable(f"{tag}_split", lowBound=0)
    _hard_le(ctx, _linear(terms) + fixed_sum - 1, slack, f"{tag}_splitcap")
    obj.append(_split_penalty(weights) * slack)
    if max_blocks is not None and max_blocks >= 1:
        _hard_le(ctx, _linear(terms) + fixed_sum, max_blocks, f"{tag}_maxblocks")
    return starts


def _interval_candidates(
    seq: Mapping[int, object],
    n: int,
    host: Mapping[int, object] | None = None,
    fixed_length: int | None = None,
) -> tuple[list[tuple[int, int]], list[int]]:
    """在勤が取り得る連続区間の候補と、固定1セルの添字列を返す。

    在勤可能セル（``_is_zero`` が偽）が連続するランごとに全区間を列挙する。
    固定1セル（手動確定など）がある場合は、その区間は必ず固定セルを
    含むよう範囲を狭める。

    :param host: この列が従属する側の在勤列（休息は在勤に属する）。
        指定すると「host の在勤可能セルが区間の前にも後ろにも存在する」
        区間だけを候補に残す。これは「休息には前後に勤務が必要」（D3）を
        変数なしで課すもので、休息区間が在勤区間の外へ出るのを防ぐ。
    """
    runs: list[list[int]] = []
    current: list[int] = []
    for i in range(n):
        if _is_zero(seq.get(i, 0)):
            if current:
                runs.append(current)
                current = []
            continue
        current.append(i)
    if current:
        runs.append(current)

    fixed_ones = [i for i in range(n) if _is_one(seq.get(i, 0))]
    fix_first = fixed_ones[0] if fixed_ones else 0
    fix_last = fixed_ones[-1] if fixed_ones else 0
    host_idx = [i for i in range(n) if not _is_zero(host.get(i, 0))] if host else []
    candidates: list[tuple[int, int]] = []
    for run in runs:
        lo, hi = run[0], run[-1]
        if fixed_ones and not all(lo <= i <= hi for i in fixed_ones):
            # 固定セルが別のランにある。連続1区間に収まらないので候補なし。
            continue
        if fixed_length is not None:
            flen = fixed_length
            if flen <= 0 or (hi - lo + 1) < flen:
                continue
            max_start = hi - flen + 1
            for start_i in range(lo, max_start + 1):
                end_i = start_i + flen - 1
                if fixed_ones and not (start_i <= fix_first and end_i >= fix_last):
                    continue
                if host is not None:
                    if not any(i < start_i for i in host_idx):
                        continue
                    if not any(i > end_i for i in host_idx):
                        continue
                candidates.append((start_i, end_i))
        else:
            for start_i in range(lo, (fix_first if fixed_ones else hi) + 1):
                for end_i in range((fix_last if fixed_ones else start_i), hi + 1):
                    if host is not None:
                        # 前後に在勤できるセルがなければ休息区間として成立しない。
                        if not any(i < start_i for i in host_idx):
                            continue
                        if not any(i > end_i for i in host_idx):
                            continue
                    candidates.append((start_i, end_i))
    return candidates, fixed_ones


def _add_interval_block(
    ctx: _ModelCtx,
    seq: Mapping[int, object],
    tag: str,
    n: int,
    host: Mapping[int, object] | None = None,
    fixed_length: int | None = None,
) -> bool:
    """「在勤は高々1つの連続区間」を**区間列挙**でハード制約化する。

    ブロック開始フラグ（``s_i >= duty_i - duty_{i-1}``）と ``Σ s_i <= 1``
    の素朴な定式化では、線形緩和が ``duty_i = 0.5`` を許してしまうため
    CBC がハード制約を満たす整数解に到達できなくなる（実測: 7日計画で
    1パス目が解なし）。区間選択に置き換えると緩和が
    「区間の重み付き平均」になり、最大の必要人員を満たす区間を自然に選ぶため
    緩和が強くなる。

    :returns: 制約を張れたら ``True``、候補が膨大すぎて諦めたら ``False``。
    """
    candidates, fixed_ones = _interval_candidates(seq, n, host, fixed_length)
    if not candidates:
        if fixed_ones:
            ctx.conflicts.append(f"{tag}: 固定セルを含む在勤区間がありません")
        return True
    if len(candidates) > config.MAX_INTERVAL_CANDIDATES:
        return False
    z_vars: list[tuple[tuple[int, int], object]] = []
    for start_i, end_i in candidates:
        z = pulp.LpVariable(f"{tag}_z{start_i}_{end_i}", 0, 1, pulp.LpBinary)
        z_vars.append(((start_i, end_i), z))
    # 高々 1 区間を選ぶ（0 なら「その日は在勤なし」）。
    _hard_le(ctx, _linear([(z, 1) for _iv, z in z_vars]), 1, f"{tag}_oneinterval")
    for i in range(n):
        cell = seq.get(i, 0)
        if _is_zero(cell):
            continue
        covering = [z for (iv_start, iv_end), z in z_vars if iv_start <= i <= iv_end]
        # 候補生成で固定セルを含む区間しか残していないので covering は非空。
        ctx.prob += cell == pulp.lpSum(covering)
    return True


def _commute_reach_slots(n: int) -> int:
    """「帰宅を挟む」とみなす下班の長さを時間帯数で返す。

    :data:`config.MIN_CONTIGUOUS_DUTY_MINUTES` から粒度 30 分で換算する。
    1 未満になる（粒度が閾値より粗い）ときは 0（＝ D4 を課さない）。
    """
    reach = config.MIN_CONTIGUOUS_DUTY_MINUTES // 30
    if reach < 1:
        return 0
    return min(reach, max(0, n - 1))


def _add_commute_penalty(
    ctx: _ModelCtx,
    duty: Mapping[int, object],
    tag: str,
    n: int,
    reach: int,
    obj: list[object],
    weights: ObjectiveWeights,
) -> None:
    """勤務ブロックが「長い下班」の後ろで始まることに出勤コストを課す（案1の D4）。

    現場metricの欠陥は「勤務 → 帰宅 → 再出勤」で、これは**昼休みでは起こらず**
    長い下班でのみ起きる。そこで「在勤セルの直前の ``reach`` 個の時間帯に
    在勤が 1 セル以上必要」と課し、満たせなかった分だけを罰変数で表す。
    変数は在勤セル数に比例するので O(n) で済む。

    :param reach: 在勤を要求する遡及セル数（= 通勤とみなす下班の長さ）。
    """
    if n < 2 or reach < 1:
        return
    reach = min(reach, n - 1)
    slack_items: list[object] = []
    prior: list[object] = [0] * n
    for i in range(1, n):
        start = duty.get(i, 0)
        prev_prior = prior[i - 1]
        duty_prev = duty.get(i - 1, 0)
        # prior_i = OR(duty_0..duty_{i-1})：先行在勤があるか。
        prior_i = pulp.LpVariable(f"{tag}_prior{i}", lowBound=0, upBound=1)
        ctx.prob += prior_i >= prev_prior
        if not _is_zero(duty_prev):
            ctx.prob += prior_i >= duty_prev
            ctx.prob += prior_i <= prev_prior + duty_prev
        else:
            ctx.prob += prior_i <= prev_prior
        prior[i] = prior_i
        if _is_zero(start):
            continue
        window = [duty.get(j, 0) for j in range(max(0, i - reach), i)]
        window_vars = [v for v in window if not _is_zero(v)]
        slack = pulp.LpVariable(f"{tag}_commute{i}", lowBound=0)
        # 先行在勤がない（初回ブロック）ときは slack を立てない。
        # 先行在勤がある窓外の長い下班後の再開は罰する。
        _hard_le(
            ctx,
            start - pulp.lpSum(window_vars) - slack + prior_i,
            1,
            f"{tag}_commute_{i}",
        )
        slack_items.append(slack)
    if slack_items:
        obj.append(weights.split_duty_penalty * pulp.lpSum(slack_items))


def _add_duty_block(
    ctx: _ModelCtx,
    duty: Mapping[int, object],
    work: Mapping[int, object],
    brk: Mapping[int, object],
    tag: str,
    n: int,
    *,
    allow_split: bool,
    commute_reach: int = 0,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
    break_slots: int | None = None,
    max_work_blocks: int = 2,
) -> None:
    """1職員1日の勤務を運用可能な形に制約する（案1の中核）。

    園の実運用では出退勤は1日1回である。実測では 115 職員日中
    109 職員日に「勤務 → 帰宅 → 再出勤」の穴が混入していたため、現場で
    そのシフトを使えなかった。ここでは次の3点を課す。

    D1 **在勤（勤務＋休息）は高々1つの連続ブロック**
        昼休みは在勤列では連続しているので許される。「帰宅を挟む穴」だけが
        弾かれる。``allow_split=True`` のときは従来のソフトペナルティに戻る。

    D2 **休息は高々1つの連続ブロック**
        1日複数回の休息は現場では取得できない。

    D3 **休息には前後に勤務が必要**
        始業直後の休息／終業直前の休息は業務上ありえないため、
        休息セルの両隣が在勤であることを個別に課す。
    """
    if n < 1:
        return
    soft_obj = obj if allow_split else None
    soft_weights = weights if allow_split else None
    edge_items: list[object] = []
    # D1: 在勤は1連続区間。
    # 区間列挙は高々 O(n²) 変数になり、CBC が追いつかない（実測:
    # 7日計画で変数 13,295 → 77,505、147秒でも 1パス目が解なし）。
    # なので候補が上限内に収まる場合だけハード化し、それ以外はソフトに落とす。
    # ソフトの場合でも D4（通勤）で現場不明白な穴は罰される。
    if allow_split:
        _add_block(ctx, duty, f"duty_{tag}", n, soft_obj, soft_weights)
    elif not _add_interval_block(ctx, duty, f"duty_{tag}", n):
        # 候補が上限超え。ソフトペナルティにフォールバックする
        # （SPLIT_SHIFT で UI に通知される）。
        _add_block(ctx, duty, f"duty_{tag}", n, obj, weights)
    # D2: 休息は1連続区間。休息区間は必ず在勤区間の内側に入る。
    # 排他制約（w_i + b_i <= 1）と在勤の定義から休息は在勤に含まれるので、
    # 包含関係そのものは自動的に満たされる。ここでは候補生成で
    # 「休息区間の前後に在勤可能なセルがある」こと（D3）を課す。
    if allow_split:
        _add_block(ctx, brk, f"brk_{tag}", n, soft_obj, soft_weights)
    elif not _add_interval_block(ctx, brk, f"brk_{tag}", n, host=duty, fixed_length=break_slots):
        _add_block(ctx, brk, f"brk_{tag}", n, obj, weights)
    # D1-2: 勤務時間の3分割禁止（高々 max_work_blocks ブロック、既定2: 勤務-休憩-勤務まで）
    if max_work_blocks is not None and max_work_blocks >= 1:
        _add_block(ctx, work, f"work_{tag}", n, obj=None, weights=None, max_blocks=max_work_blocks)
    # D3: 休息の前後に在勤があること（**ソフト**）。
    # ハードにすると 2〜3 時間帯しか働かない短勤務には「休息を入れても
    # 前後に勤務がない」ため休息自体が入らず、総休憩時間が 138h → 56h まで
    # 削られた（実測）。そこで不足分だけを罰変数で表す。
    for i in range(n):
        cell = brk.get(i, 0)
        if _is_zero(cell):
            continue
        for neighbour, suffix in (
            (duty.get(i - 1, 0) if i > 0 else 0, "l"),
            (duty.get(i + 1, 0) if i + 1 < n else 0, "r"),
        ):
            if _is_zero(neighbour):
                continue
            edge = pulp.LpVariable(f"brkedge_{tag}_{i}_{suffix}", lowBound=0)
            _hard_le(ctx, cell - neighbour - edge, 0, f"brkedge_{tag}_{i}_{suffix}")
            edge_items.append(edge)
    # D4: 通勤（下班）を挟む出勤に出勤コストを課す。
    if commute_reach >= 1 and obj is not None and weights is not None:
        _add_commute_penalty(ctx, duty, f"duty_{tag}", n, commute_reach, obj, weights)
    if edge_items and obj is not None and weights is not None:
        obj.append(weights.break_edge_penalty * pulp.lpSum(edge_items))


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
    allow_split: bool = False,
    break_minutes: int = _DEFAULT_BREAK_MINUTES,
) -> None:
    """セル変数の生成と、セル単位のハード制約（排他・在勤/休憩ブロック・1日上限）。

    :param allow_split: ``True`` なら「在勤1日1ブロック・休息1日1回」を
        ソフトペナルティに戻す（``relaxation=RELAX_HOURS`` で有効）。
    """
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    n = len(slots)
    standby = {d for d in days if not requirements.for_day(d)}
    if "fixed" in drop_groups:
        fixed = {}
    for st in staff:
        sid = st.staff_id
        prefs_st = None if ignore_unavailable or "unavailable" in drop_groups else prefs.get(sid)
        cap_min = daily_cap_minutes(st.contract)
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
                allowed = (
                    day_ok
                    and ("contract" in drop_groups or _slot_is_contractible(st, slot))
                    and st.is_placeable
                )
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
            work_map: dict[int, object] = {}
            breaks: dict[int, object] = {}
            for i in range(n):
                wv = w_cells[i]
                bv = b_cells[i]
                if _is_zero(wv) and _is_zero(bv):
                    continue
                duty[i] = wv + bv
                if not _is_zero(wv):
                    work_map[i] = wv
                if not _is_zero(bv):
                    breaks[i] = bv
            raw_break = (
                st.contract.break_minutes
                if st.contract.break_minutes is not None
                else getattr(settings, "break_minutes", None) or break_minutes
            )
            is_part_short = st.contract.daily_hours is not None and st.contract.daily_hours <= 6.0
            if (
                (st.contract.break_minutes is not None and st.contract.break_minutes == 0)
                or is_part_short
                or cap_min <= 6 * 60
            ):
                eff_break = 0
            else:
                eff_break = min(
                    max(raw_break, config.MIN_ALLOWABLE_BREAK_MINUTES),
                    config.MAX_ALLOWABLE_BREAK_MINUTES,
                )
            effective_cap_min = min(cap_min, 6 * 60) if eff_break == 0 else cap_min
            max_blocks = (
                st.contract.max_work_blocks_per_day
                if st.contract.max_work_blocks_per_day is not None
                else getattr(settings, "max_work_blocks_per_day", config.DEFAULT_MAX_WORK_BLOCKS)
            )

            if "duty_block" not in drop_groups:
                # 案1: 1職員1日の在勤は1連続ブロック・休息は1回だけにする。
                # allow_split=True（緩和 L3 ``RELAX_HOURS``）ならソフトに戻る。
                _gran = max(1, int(requirements.granularity_min or slots[0].minutes))
                _brk_slots = _break_slots(
                    cap_min, eff_break, _gran, daily_hours=st.contract.daily_hours
                )
                _add_duty_block(
                    ctx,
                    duty,
                    work_map,
                    breaks,
                    f"{sid}_{tag}",
                    n,
                    allow_split=allow_split or config.ALLOW_DUTY_BLOCK_SPLIT,
                    commute_reach=_commute_reach_slots(n),
                    obj=obj,
                    weights=weights,
                    break_slots=_brk_slots,
                    max_work_blocks=max_blocks,
                )

            if "daily_cap" not in drop_groups:
                _hard_le(
                    ctx,
                    _linear(day_items),
                    effective_cap_min,
                    f"dailycap_{sid}_{day.isoformat()}",
                )
            ctx.day_work[(sid, day)] = _linear(day_items)
            w_only = [w_cells[i] for i in range(n) if not _is_zero(w_cells[i])]
            if w_only:
                # PuLP の LpAffineExpression は int による除算持っていないため、
                # 「n で割る」を「1/n 倍する」で書く（時間帯 1 個の問題が落ちるのを防ぐ）。
                # 割る分母は **時間帯総数 n ではなく、実際に勤務変数がある
                # 時間帯数 ``len(w_only)``** でなければならない。
                # 契約時間帯の外側（最早始業より前など）は ``w_cells[i] == 0``
                # になるため、n で割ると 1 個だけ働いた日が ``1/n``（=0.06 程度）
                # のまま取り残り、勤務変数が分数になる。すると
                # ``y`` を「その日働いたか 0/1 のフラグ」として使う
                # 連続勤務日数・週所定出勤日数・休憩時間の各制約が
                # まとめて弱まってしまう（``:func:`_fairness_day_flag`` が
                # ``1.0 / len(items)`` としているのと同じ不整合）。
                _hard_ge(ctx, yvar, _linear([(v, 1) for v in w_only]) * (1.0 / len(w_only)))
                _hard_le(ctx, yvar, _linear([(v, 1) for v in w_only]))

                # 正社員の勤務時間は 4〜8時間の間で調整可能とする
                if (
                    st.contract.employment_type == EmploymentType.SEI
                    and "daily_cap" not in drop_groups
                ):
                    sei_min_h = (
                        st.contract.min_daily_hours
                        if st.contract.min_daily_hours is not None
                        else getattr(
                            settings, "sei_min_daily_hours", config.DEFAULT_SEI_MIN_DAILY_HOURS
                        )
                    )
                    if st.contract.daily_hours is not None:
                        sei_min_h = min(sei_min_h, st.contract.daily_hours)
                    if cap_min < sei_min_h * 60:
                        sei_min_h = cap_min / 60.0
                    fac_sei_max_h = getattr(
                        settings, "sei_max_daily_hours", config.DEFAULT_SEI_MAX_DAILY_HOURS
                    )
                    if st.contract.max_daily_hours is not None:
                        sei_max_h = st.contract.max_daily_hours
                    elif st.contract.overtime_allowed:
                        sei_max_h = max(cap_min / 60.0, fac_sei_max_h)
                    elif st.contract.daily_hours is not None:
                        sei_max_h = min(st.contract.daily_hours, fac_sei_max_h)
                    else:
                        sei_max_h = fac_sei_max_h
                    min_work_min = min(int(round(sei_min_h * 60)), sum(s.minutes for s in slots))
                    max_work_min = int(round(sei_max_h * 60))
                    _hard_ge(ctx, _linear(day_items), min_work_min * yvar, f"seimin_{sid}_{tag}")
                    _hard_le(ctx, _linear(day_items), max_work_min * yvar, f"seimax_{sid}_{tag}")
        ctx.prob += hours == _linear(week_items)


def _qualified_items(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    day: date,
    idx: int,
    standard: StaffingStandard | None,
) -> list[object]:
    """「必要保育士数」を満たす勤務変数を返す。

    保育士・准看護師・``qualified_extra_roles`` を必要保育士数に算入する。
    ``nurse_as_qualified_cap`` が 1 以上の場合（企業主導型保育事業など）、
    看護師・准看護師は上限までのみなし保育士として算入する。
    ``qualified_extra_roles`` と ``nurse_as_qualified_cap`` は排他（StaffingStandard.__post_init__）。
    """
    nurse_cap = standard.nurse_as_qualified_cap if standard is not None else 0
    extra_roles = standard.qualified_extra_roles if standard is not None else frozenset()
    items: list[object] = []
    nurse_items: list[object] = []
    for member in staff:
        var = ctx.work.get((member.staff_id, day, idx))
        if var is None or _is_zero(var):
            continue
        if member.has_role(Role.HOIKUSHI):
            items.append(var)
        elif set(member.roles) & extra_roles:
            items.append(var)  # 比率の分子にそのまま算入（上限なし）
        elif nurse_cap > 0 and member.is_nurse:
            nurse_items.append(var)
    if not nurse_items or nurse_cap <= 0:
        return items
    if nurse_cap >= len(nurse_items):
        return items + nurse_items

    # 上限付きのみなし保育士: used = min(nurse_cap, Σ nurses)。
    # 目的関数上の短fallを埋めるため used は必要分まで最大化される。
    used = pulp.LpVariable(
        f"nurse_used_{day.isoformat()}_{ctx.prob.name}_{idx}",
        lowBound=0,
        upBound=nurse_cap,
        cat=pulp.LpContinuous,
    )
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
                    if soft:
                        gap = pulp.LpVariable(f"gap_{name}", lowBound=0)
                        ctx.prob += gap >= need
                        obj.append(weights.shortfall_penalty * gap)
                        ctx.shortfall[name] = (gap, need, day, slot, label)
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


def _break_slots(
    cap_min: int, break_minutes: int, gran: int, daily_hours: float | None = None
) -> int:
    """必要休憩スロット数を算出する（0 以上、日上限スロット数以下）。

    休憩不要の 6 時間以下は「契約上限」ではなく契約の標準勤務時間
    (``daily_hours``) で判定する。実労働が短い日に休憩を過剰要求しない。
    """
    if break_minutes <= 0:
        return 0
    effective_cap = int(round((daily_hours if daily_hours is not None else cap_min / 60.0) * 60))
    if effective_cap <= 6 * 60:
        return 0
    return min(max(1, -(-break_minutes // gran)), max(0, cap_min // gran))


def _add_breaks(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    obj: list[object],
    weights: ObjectiveWeights,
    break_minutes: int,
    settings: FacilitySettings | None = None,
    allow_deficit: bool = False,
) -> None:
    """勤務日ごとに連続した休憩が取れるようにする（不足分はソフトペナルティ）。"""
    slots = tuple(requirements.slots)
    gran = max(1, int(requirements.granularity_min or slots[0].minutes))
    for st in staff:
        sid = st.staff_id
        raw_break = (
            st.contract.break_minutes
            if st.contract.break_minutes is not None
            else getattr(settings, "break_minutes", None) or break_minutes
        )
        cap_min = daily_cap_minutes(st.contract)
        is_part_short = st.contract.daily_hours is not None and st.contract.daily_hours <= 6.0
        if (
            (st.contract.break_minutes is not None and st.contract.break_minutes == 0)
            or is_part_short
            or cap_min <= 6 * 60
        ):
            eff_break = 0
        else:
            eff_break = min(
                max(raw_break, config.MIN_ALLOWABLE_BREAK_MINUTES),
                config.MAX_ALLOWABLE_BREAK_MINUTES,
            )
        need_slots = _break_slots(cap_min, eff_break, gran, daily_hours=st.contract.daily_hours)
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
            # 不足分は ``break_conflict_penalty`` ではなく
            # ``break_deficit_penalty`` で罰する。労働基準法第34条の休憩義務は
            # 現場運用上の都合（通勤ペナルティなど）より重い扱いにしておく。
            obj.append(weights.break_deficit_penalty * deficit)

            # 「勤務しなかった日の休憩」を禁ずる。これは休憩が労働時間である以上
            # 「出勤していないのに休憩している」状態も矛盾するため。
            # 入れないと時間帯が 1 個だけの日に休憩が置かれ、
            # 乃至その時間帯が無人になる（``test_時間帯1個の境界`` の回帰）。
            if len(items) <= 1:
                ctx.prob += _linear(items) <= yvar
            else:
                _hard_le(ctx, _linear(items), len(items) * yvar, f"brkneedwork_{sid}_{tag}")


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
        c.can_open,
        c.can_close,
        c.can_lead,
        c.is_placeable,
        # 36協定・契約細部：有無で daily_cap が変わるため、同一署名でも
        # 交換不可能な職員が混ざると対称性破り（WLOG）が崩れる。
        c.has_36_agreement,
        c.variable_working_hours,
        c.break_minutes,
        c.min_daily_hours,
        c.max_daily_hours,
        c.max_work_blocks_per_day,
        pref_key,
        # 交換不可能性：スキル・担任クラス・開閉所・リーダー・配置可否。
        # 同一契約でもこれらが異なると入れ替え不可なので対称性破りの対象外とする。
        frozenset(st.skills),
        st.assigned_class_id,
        st.can_open,
        st.can_close,
        st.can_lead,
        st.is_placeable,
    )


def _add_symmetry_breaking(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    prefs: Mapping[str, StaffPreferences] | None,
    fixed: Mapping[tuple[str, date, str], CellState] | None = None,
) -> None:
    """契約・希望が完全に同一の職員グループに勤務時間の大小関係を課す。

    この条件は「同じグループ内では入れ替えても目的関数が変わらない」ことを使う
    WLOG な対称性打断であり、最適解を失わない。職員数が多いほどCBCの探索が
    な探索になり、求解時間を大きく短縮できる。

    ``fixed`` が空でないときは手動確定セルがあるため職員が入れ替え不可となり、
    対称性破りをスキップする。
    """
    if fixed:
        return
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
    fixed: Mapping[tuple[str, date, str], CellState] | None = None,
) -> None:
    """勤務時間の偏り・月間時間・未使用職員・長時間勤務のペナルティ。"""
    days = requirements.all_days()
    _add_symmetry_breaking(ctx, staff, prefs, fixed=fixed)
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
        reachable = len(days) * daily_cap_minutes(st.contract) / 60.0
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
        # 週44時間目安は週ごとの窓で判定する（R2-SOL-02 修正済み）。
        for wi, window in enumerate(weekly_windows(days, config.WEEKLY_WINDOW_DAYS)):
            week_items = [
                (ctx.day_work[(sid, d)], 1)
                for d in window
                if isinstance(ctx.day_work.get((sid, d)), pulp.LpAffineExpression)
            ]
            if not week_items:
                continue
            week_hours = _linear(week_items) * (1.0 / 60.0)  # day_work は「分」単位
            slack = pulp.LpVariable(f"longw_{sid}_{wi}", lowBound=0)
            ctx.prob += week_hours - STATUTORY_WEEKLY_WORK_HOURS <= slack
            obj.append(weights.max_shift_length_penalty * slack)
    for sid, day in ctx.day_work:
        expr = ctx.day_work[(sid, day)]
        if isinstance(expr, int | float):
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
    *,
    pattern_mode: bool = False,
) -> None:
    """現場固有の勤務パターン（早番・日勤・遅番）への整列。

    pattern_mode=True の場合は出勤時にいずれかのパターンへ厳密に一致させる（ハード制約）。
    False の場合は目的関数で境界を引き寄せる（ソフトペナルティ）。
    """
    slots = tuple(requirements.slots)
    if not slots or not patterns:
        return
    penalty = _pattern_alignment_penalty(weights)
    if penalty <= 0 and not pattern_mode:
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
                var = pulp.LpVariable(
                    f"pat_{sid}_{tag}_{k}",
                    0,
                    1,
                    pulp.LpBinary if pattern_mode else pulp.LpContinuous,
                )
                terms: list[object] = []
                for i in indices:
                    cell = duty.get(i, 0)
                    ctx.prob += var >= cell - 1
                    if not _is_zero(cell):
                        terms.append(cell)
                ctx.prob += len(indices) * var <= _linear([(t, 1) for t in terms])
                matched.append(var)
                if penalty > 0:
                    obj.append(-penalty * var)
            if len(matched) > 1:
                ctx.prob += _linear([(v, 1) for v in matched]) <= 1
            if pattern_mode and matched:
                dvar = ctx.day_var.get((sid, day))
                if dvar is not None and not _is_zero(dvar):
                    ctx.prob += pulp.lpSum(matched) == dvar, f"pat_exact_{sid}_{tag}"
                for i, cell in duty.items():
                    if _is_zero(cell):
                        continue
                    covering = [matched[k] for k, indices in enumerate(ranges) if i in indices]
                    if covering:
                        ctx.prob += cell <= pulp.lpSum(covering), f"pat_cover_{sid}_{tag}_{i}"
                    else:
                        ctx.prob += cell == 0, f"pat_no_pattern_{sid}_{tag}_{i}"


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


def _add_pairing_rules(
    ctx: _ModelCtx,
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    rules: Sequence[PairingRule],
    obj: list[object],
) -> None:
    """職員ペアリング制約（相性・OJT指導）をMILPモデルに追加する。

    - require_together: Aが出勤しているスロットはBも必ず出勤 (work_A <= work_B)
    - avoid_together: AとBは同一スロットに同時出勤しない (work_A + work_B <= 1)
    """
    if not rules:
        return

    days = requirements.all_days()
    slots = tuple(requirements.slots)
    staff_ids = {st.staff_id for st in staff}

    for r_idx, rule in enumerate(rules):
        sid_a = rule.staff_a_id
        sid_b = rule.staff_b_id
        if sid_a not in staff_ids or sid_b not in staff_ids:
            continue

        for day in days:
            tag = day.strftime("%m%d")
            for s_idx, _slot in enumerate(slots):
                # 在勤（勤務 + 休憩）ブロック全体を考慮
                w_a = ctx.work.get((sid_a, day, s_idx), 0)
                b_a = ctx.brk.get((sid_a, day, s_idx), 0)
                w_b = ctx.work.get((sid_b, day, s_idx), 0)
                b_b = ctx.brk.get((sid_b, day, s_idx), 0)

                duty_a = w_a if _is_zero(b_a) else (b_a if _is_zero(w_a) else w_a + b_a)
                duty_b = w_b if _is_zero(b_b) else (b_b if _is_zero(w_b) else w_b + b_b)

                if _is_zero(duty_a) and _is_zero(duty_b):
                    continue

                c_name = f"pair_{r_idx}_{tag}_{s_idx}"
                if rule.kind == "require_together":
                    # staff_a が出勤なら staff_b も出勤 (在勤ブロックで一致): duty_a <= duty_b
                    if rule.is_hard:
                        ctx.prob += duty_a <= duty_b, c_name
                    else:
                        slack = pulp.LpVariable(f"slk_{c_name}", 0, 1)
                        ctx.prob += duty_a <= duty_b + slack, c_name
                        obj.append(rule.penalty * slack)

                elif rule.kind == "avoid_together":
                    # staff_a と staff_b は同一時間帯に同席不可 (在勤ブロック排他): duty_a + duty_b <= 1
                    if rule.is_hard:
                        ctx.prob += duty_a + duty_b <= 1, c_name
                    else:
                        slack = pulp.LpVariable(f"slk_{c_name}", 0, 1)
                        ctx.prob += duty_a + duty_b <= 1 + slack, c_name
                        obj.append(rule.penalty * slack)


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
        saturday_days = {day for day in days if day.weekday() == config.WEEKEND_START_WEEKDAY}
        # 対象期間に土曜が無い場合も**項は作る**（値は 0 になる）。
        # 作らないと「重みを上げても反応しない」係数が現れ、
        # 「重みが黙って無視されない」という保証が崩れる。
        # 空の袋で値がずれる問題は :func:`_fairness_spread` 側で
        # ``top == bottom`` として処理している。
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
        if not items:
            # 対象者が 1 人もいない袋では、項を**0 に固定**する。
            #
            # 何も制約を付けると CBC は ``top=0, bottom=cap`` を選び、
            # ``top - bottom = -cap``（= ``-len(window)``）を定数として
            # 目的関数に組み込んでしまう。その結果「土曜の無い計画では
            # 1 袋ごとに目的関数が ``-fairness_saturday_penalty`` ずれる」
            # という、実行値を比較できなくなるズレが生まれる。
            #
            # ``top == bottom`` を明示すれば値は 0 になり、かつ変数は残るため
            # 「重みを上げると必ず反応する」（= 重みが黙って無視されない）という
            # 保証も保てる。項ごと ``continue`` するとこの保証を壊す。
            cap = max(1, len(window))
            top = pulp.LpVariable(f"fmax_{key}_{w}", lowBound=0, upBound=cap)
            bottom = pulp.LpVariable(f"fmin_{key}_{w}", lowBound=0, upBound=cap)
            ctx.prob += top == bottom, f"fmx_{key}_{w}_empty"
            obj.append(weight * (top - bottom))
            continue
        # 上限は「その週の日数」。これを上回る回数にはならないので有界にする。
        # ここを無制限にすると ``top - bottom`` が目的関数を -∞ まで下げてしまい、
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
    relaxation: int = 0,
    drop_groups: frozenset[str] = frozenset(),
    pattern_mode: bool = False,
    pairing_rules: Sequence[PairingRule] | None = None,
    classrooms: Sequence[ClassRoom] | None = None,
    require_leader: bool = False,
    require_opening_closing_skill: bool = False,
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
    :param pattern_mode: ``True`` のとき現場パターン枠への厳密一致をハード制約とする
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
    from shiftai.relaxation import RelaxLevel, normalize_level

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
        allow_split=level >= int(RelaxLevel.RELAX_HOURS),
        break_minutes=break_minutes,
    )
    if "coverage" not in drop_groups:
        if soft_coverage or level >= int(RelaxLevel.SOFT_COVERAGE):
            _add_coverage(ctx, staff, requirements, obj, weights, standard)
        else:
            _add_coverage(ctx, staff, requirements, standard=standard)
    _add_workload(ctx, staff, requirements, obj, weights)
    if level < int(RelaxLevel.RELAX_HOURS) and "break" not in drop_groups:
        _add_breaks(ctx, staff, requirements, obj, weights, break_minutes, settings=settings)
    if level < int(RelaxLevel.RELAX_HOURS) and "rest" not in drop_groups:
        _add_rest(ctx, staff, requirements, obj, weights)
    if "weekly" not in drop_groups:
        _add_consecutive(ctx, staff, requirements, obj, weights)
    _add_preferences(ctx, staff, requirements, prefs, kinds, obj, weights)
    _add_hours_objective(ctx, staff, requirements, obj, weights, prefs, fixed=fixed)
    _add_pattern_alignment(
        ctx, staff, requirements, obj, weights, patterns or (), pattern_mode=pattern_mode
    )
    if "fairness" not in drop_groups:
        _add_fairness(ctx, staff, requirements, obj, weights, standard, patterns or ())
    if pairing_rules:
        _add_pairing_rules(ctx, staff, requirements, pairing_rules, obj)
    if classrooms or require_leader or require_opening_closing_skill:
        from shiftai.solver_core.constraints.classrooms import _add_classroom_and_skill_constraints

        _add_classroom_and_skill_constraints(
            ctx=ctx,
            staff=staff,
            requirements=requirements,
            obj=obj,
            weights=weights,
            standard=standard,
            classrooms=classrooms,
            require_leader=require_leader,
            require_opening_closing_skill=require_opening_closing_skill,
        )
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
    from shiftai.relaxation import relaxed_weight_names

    names = relaxed_weight_names(level)
    if not names:
        return weights
    overrides = {name: 0.0 for name in names}
    return replace(weights, **overrides)
