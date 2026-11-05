"""Continuous work interval (block) constraints for MILP solver."""

from __future__ import annotations

from collections.abc import Mapping

import pulp

from shiftai import config
from shiftai.domain import ObjectiveWeights
from shiftai.solver_core.context import (
    _MAX_INTERVAL_CANDIDATES,
    _hard_le,
    _is_one,
    _is_zero,
    _linear,
    _ModelCtx,
    _split_penalty,
)


def _add_block(
    ctx: _ModelCtx,
    seq: Mapping[int, object],
    tag: str,
    n: int,
    obj: list[object] | None = None,
    weights: ObjectiveWeights | None = None,
) -> list[object]:
    """列 seq が「高々1つの連続ブロック」に近づくよう制約する。

    変数 v_i（0/1）に対し「ブロック開始フラグ」s_i を 1 個ずつ置き
    ``s_i >= v_i - v_{i-1}``（v_{-1} = 0）を課す。s_i は最小値（= ブロックの
    開始数）を取るので、2つ目以降のブロックだけを slack 変数で表現できる。

    :param obj: ``None`` を渡すと**ハード制約**（``Σ s_i <= 1``）になり、
        分割すると構造的に不可能になる。``obj`` を渡すと従来のソフトペナルティ。
    :returns: 作った ``s_i`` 変数の列（ハード判定の検証に使う）。

    .. note::
       修正前は「ハードにすると昼休みで勤務が分かれて配置基準を満たせない」
       として常にソフトにしていた。実際には在勤列（勤務＋休息）は昼休みを
       またいで連続しているため、:func:`_add_duty_block` で在勤列にハード制約を
       張れば昼休みを許したまま「帰宅と再出勤」だけを排除できる。
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
    if not terms:
        if fixed_sum > 1:
            if obj is None or weights is None:
                ctx.conflicts.append(f"{tag}: 勤務が {fixed_sum} ブロックに分かれています")
            else:
                gap = pulp.LpVariable(f"{tag}_split", lowBound=0)
                ctx.prob += gap >= fixed_sum - 1
                obj.append(_split_penalty(weights) * gap)
        return []
    if obj is None or weights is None:
        _hard_le(ctx, _linear(terms) + fixed_sum, 1, f"{tag}_oneblock")
        return starts
    slack = pulp.LpVariable(f"{tag}_split", lowBound=0)
    _hard_le(ctx, _linear(terms) + fixed_sum - 1, slack, f"{tag}_splitcap")
    obj.append(_split_penalty(weights) * slack)
    return starts


def _interval_candidates(
    seq: Mapping[int, object],
    n: int,
    host: Mapping[int, object] | None = None,
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
    candidates, fixed_ones = _interval_candidates(seq, n, host)
    if not candidates:
        if fixed_ones:
            ctx.conflicts.append(f"{tag}: 固定セルを含む在勤区間がありません")
        return True
    if len(candidates) > _MAX_INTERVAL_CANDIDATES:
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
    for i in range(1, n):
        start = duty.get(i, 0)
        if _is_zero(start):
            continue
        prev = duty.get(i - 1, 0)
        if _is_zero(start) and _is_zero(prev):
            continue
        window = [duty.get(j, 0) for j in range(max(0, i - reach), i)]
        window_vars = [v for v in window if not _is_zero(v)]
        if not window_vars:
            continue
        slack = pulp.LpVariable(f"{tag}_commute{i}", lowBound=0)
        # 窓が在勤で埋まっているのに duty_i が立つと slack が 1 立つ
        # （= 在勤ブロックを長い下班で分けた shortfall）。
        _hard_le(ctx, start - pulp.lpSum(window_vars) - slack, 0, f"{tag}_commute_{i}")
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
) -> None:
    """1職員1日の勤務を運用可能な形に制約する（案1の中核）。

    園の実運用では出退勤は1日1回である。実測では 115 職員日中
    109 職員日に「勤務 → 帰宅 → 再出勤」の穴が混入していたため、現場で
    そのシフトを使えなかった。ここでは次の3点を課す。

    D1 **在勤（勤務＋休息）は高々1つの連続ブロック**
        昼休みは在勤列では連続しているので許される。「帰宅を挟む穴」だけが
        弾かれる。``,allow_split=True`` のときは従来のソフトペナルティに戻る。

    D2 **休息は高々1つの連続ブロック**
        1日複数回の休息は現場では取得できない。

    D3 **休息には前後に勤務が必要**
        始業直後の休息／終業直前の休息は業務上ありえないため、
        休息セルの両隣が在勤であることを個別に課す。

    :param duty: ``i -> w_i + b_i``（在勤）。
    :param work: ``i -> w_i``（勤務）。
    :param brk: ``i -> b_i``（休息）。
    :param allow_split: ``True`` なら D1/D2 をソフトペナルティに戻す。
        ``relaxation=RELAX_HOURS`` で自動的に有効になる。
    :param commute_reach: 在勤ブロックの開始に遡及して在勤を要求する
        セル数（通勤とみなす下班の長さ）。D4 の使う。
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
    elif not _add_interval_block(ctx, brk, f"brk_{tag}", n, host=duty):
        _add_block(ctx, brk, f"brk_{tag}", n, obj, weights)
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


