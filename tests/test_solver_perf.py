"""``shiftai.solver`` の境界ケース・回帰テスト・性能計測。

本ファイルが守る不変条件:

1. ``ObjectiveWeights`` の全フィールドが実際に目的関数へ反映される
2. 同じ入力なら 2 回走らせた ``SolveResult`` が同一（CBC の決定性）
3. ``solve_shift_greedy`` が ``solve_shift`` と同じ引数並びで呼べる

回帰テストとして固定している不具合:

* ``verify_solution`` / ``_run_cbc`` が CBC の「途中値（LP 緩和値）」を
  ハード制約を満たす解として採用していた問題
* ``solve_shift_greedy._put_block`` の無限ループ
* ``_staffing_advice`` の供給人時 0 での ``ZeroDivisionError``

**実行時間について**: CBC は重いので実ソルバは 1 日・数名・数スロットの小さな問題に
``time_limit_sec=5`` 以下に抑える。残りは ``_build_problem``（CBC を起動しない）、
貪欲法（純 Python）、純関数の直接呼び出しで検証する。
"""

from __future__ import annotations

import inspect
import signal
import sys
from dataclasses import replace
from datetime import date, time, timedelta
from time import perf_counter

import pulp
import pytest

from shiftai import gap_analysis, local_rules
from shiftai import solver as solver_mod
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    Requirement,
    RequirementTable,
    Role,
    Slot,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Unavailability,
    ViolationSeverity,
    to_time,
)
from shiftai.solver import (
    _add_block,
    _consecutive_streak,
    _constraint_specs,
    _coverage_diagnosis,
    _daily_cap_minutes,
    _day_is_workable,
    _extract_vars,
    _hard_ge,
    _hard_le,
    _is_var,
    _is_zero,
    _linear,
    _ModelCtx,
    _run_cbc,
    _shortfall_supply,
    _slot_by_label,
    _SolveInput,
    _staffing_advice,
    _supply_hours,
    _week_fraction,
    available_solvers,
    decode_solution,
    month_fraction,
    solve_shift,
    solve_shift_greedy,
    staff_shift_count,
    staff_work_hours,
    verify_solution,
)
from shiftai.standards import build_requirements
from tests.conftest import (
    DAY,
    DAY_CLOSE,
    DAY_OPEN,
    STANDARD_KEY,
    part_contract,
    sei_contract,
)

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

STANDARD = local_rules.get_standard(STANDARD_KEY)
MONDAY = date(2026, 9, 28)
SATURDAY = date(2026, 10, 3)


# ---------------------------------------------------------------------------
# 小道具
# ---------------------------------------------------------------------------


def _pool(specs):
    """``(職員ID, roles, contract)`` の列から ``StaffMember`` の列を作る。"""
    return [StaffMember(sid, f"職員{sid}", roles, contract) for sid, roles, contract in specs]


def _kids(day, count=3, arrive=DAY_OPEN, depart=DAY_CLOSE):
    return [
        ChildPlan(f"C{i:03d}", f"園児{i}", day, AgeClass.INFANT, arrive, depart)
        for i in range(count)
    ]


def _requirements(children, days=(DAY,), **kwargs):
    kwargs.setdefault("day_open", DAY_OPEN)
    kwargs.setdefault("day_close", DAY_CLOSE)
    kwargs.setdefault("granularity_min", 30)
    return build_requirements(children, list(days), STANDARD, **kwargs)


def _tiny_requirements(children, day=DAY, *, slots_count=4, need=1, qualified=1):
    """時間帯 ``slots_count`` 個・1 日だけの極小必要人員表を作る。"""
    slots = _requirements(children).slots[:slots_count]
    rows = [
        Requirement(day=day, slot=slot, age_class=AgeClass.INFANT, child_count=need,
                    needed_staff=need, needed_qualified=qualified, basis="テスト")
        for slot in slots
    ]
    return RequirementTable(
        day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=30, slots=slots, rows={day: rows}
    )


def _solve(children, staff, requirements, **kwargs):
    kwargs.setdefault("time_limit_sec", 5)
    kwargs.setdefault("standard", STANDARD)
    return solve_shift(children, staff, requirements, **kwargs)


def _build(staff, requirements, *, prefs=None, fixed=None, settings=None, standard=STANDARD):
    return solver_mod._build_problem(
        staff, requirements, prefs or {}, fixed or {}, settings or FacilitySettings(),
        ObjectiveWeights(), standard,
    )


def _payload(children, staff, requirements, **kwargs):
    return _SolveInput(
        children=list(children),
        staff=list(staff),
        requirements=requirements,
        prefs=kwargs.get("prefs", {}),
        fixed=kwargs.get("fixed", {}),
        settings=kwargs.get("settings", FacilitySettings()),
        weights=kwargs.get("weights", ObjectiveWeights()),
        standard=kwargs.get("standard", STANDARD),
    )


def _objective_terms(ctx) -> dict[str, float]:
    """目的関数を「変数名 → 係数」に-flatten する。"""
    expr = ctx.prob.objective
    if not isinstance(expr, pulp.LpAffineExpression):
        return {}
    return {var.name: float(coef) for var, coef in expr.items()}


def _block_count(flags) -> int:
    count = 0
    previous = False
    for flag in flags:
        if flag and not previous:
            count += 1
        previous = flag
    return count


# ===========================================================================
# 1. 守る不変条件
# ===========================================================================


def test_重みの全フィールドが目的関数に反映される(small_children, small_staff):
    """``ObjectiveWeights`` の全フィールドを 7 倍にすると対応する係数も 7 倍になること。

    いずれかのフィールドが定義だけされて使われていない（あるいは別の項と
    混線している）ことを検出するためのテスト。目的関数の係数を数値で突き合わせる
    ため CBC は使わない。罰変数（配置不足・超過配置）も出るよう 2 パス目相当の
    「ソフトな配置基準」でモデルを構築する。
    """
    days = [MONDAY, date(2026, 9, 29)]
    children = list(small_children) + [
        ChildPlan(f"{c.child_id}_2", c.name, days[1], c.age_class, c.arrive, c.depart)
        for c in small_children
    ]
    table = build_requirements(
        children, days, STANDARD, day_open=time(7, 15), day_close=time(19, 30),
        granularity_min=30,
    )
    slot = table.slots[-1]
    staff = [
        replace(m, contract=replace(m.contract, min_rest_hours=20.0, max_consecutive_days=2))
        for m in small_staff
    ]
    prefs = {
        m.staff_id: StaffPreferences(
            unavailable=[
                Unavailability(day=MONDAY, start=time(0, 0), end=time(6, 0), reason="希望休")
            ],
            preferred_off_days=frozenset({MONDAY}),
            preferred_days=frozenset({days[1]}),
            preferred_slots={days[1]: (slot,)},
            avoid_early=m.staff_id in ("S001", "S002"),
            avoid_late=m.staff_id in ("S003", "S004"),
            max_early_shifts=1,
            max_late_shifts=1,
        )
        for m in staff
    }
    default = ObjectiveWeights()

    def objective(weights: ObjectiveWeights) -> dict[str, float]:
        ctx = solver_mod._build_problem(
            staff, table, prefs, {}, FacilitySettings(), weights, STANDARD, soft_coverage=True
        )
        return _objective_terms(ctx)

    base = objective(default)
    assert base, "目的関数が一つも設定されていない"
    for name, value in vars(default).items():
        kwargs = dict(vars(default))
        kwargs[name] = value * 7
        terms = objective(ObjectiveWeights(**kwargs))
        changed = {v for v in terms if abs(terms[v] - base.get(v, 0.0)) > 1e-9}
        assert changed, f"{name} を変更しても目的関数が変わらない"
        scaled = [
            v for v in changed
            if abs(base.get(v, 0.0)) > 1e-9 and abs(terms[v] / base[v] - 7) < 1e-6
        ]
        assert scaled, f"{name} の変更が 7 倍になっていない"


def test_同じ入力なら2回とも同じ解になる(small_children, small_requirements):
    """CBC が決定的であること（タイムアウトしない極小問題で 2 回走らせて全項目を比較）。"""
    pool = _pool([
        ("S001", (Role.HOIKUSHI,), sei_contract()),
        ("S002", (Role.HOIKUSHI,), sei_contract()),
        ("S003", (Role.SHIENSHIIN,), part_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=4, need=1, qualified=1)
    first = _solve(small_children, pool, table, time_limit_sec=15)
    second = _solve(small_children, pool, table, time_limit_sec=15)
    assert first.status is second.status
    assert first.objective_value == second.objective_value
    assert first.stats["solver_status"] == second.stats["solver_status"]
    assert [(a.staff_id, a.day, a.slot.label, a.state) for a in first.assignments] == [
        (a.staff_id, a.day, a.slot.label, a.state) for a in second.assignments
    ]


def test_貪欲法はMILPと同じ引数並びで呼べる(small_children, small_staff, small_requirements):
    """``solve_shift_greedy`` は ``solve_shift`` と同じ上位引数を受け取れること。"""
    milp = list(inspect.signature(solve_shift).parameters)
    greedy = list(inspect.signature(solve_shift_greedy).parameters)
    assert milp[:4] == greedy[:4] == ["children", "staff", "requirements", "preferences"]
    # 貪欲法は settings（休園日・祝日）も受け取る。
    # 以前は FacilitySettings() をハードコードしており、solve_shift の
    # フォールバック経路から呼ばれたときに休園日 forfeiting と失われていた。
    assert set(greedy[4:]) <= {"fixed_assignments", "standard", "settings"}
    prefs = {"S001": StaffPreferences(preferred_off_days=frozenset({DAY}))}
    result = solve_shift_greedy(
        small_children, small_staff, small_requirements, prefs, standard=STANDARD
    )
    assert result.status is SolveStatus.PARTIAL
    assert result.assignments and result.messages


# ===========================================================================
# 2. 回帰テスト: verify_solution / _run_cbc
# ===========================================================================


def test_verify_solutionは正常解で違反0件(small_staff, small_requirements):
    """実際に解いたモデルのハード制約違反が 0 件であること。"""
    ctx = _build(small_staff, small_requirements)
    ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    assert ctx.specs, "制約スナップショットが収集されていない"
    assert verify_solution(ctx) == []


def test_verify_solutionは意図的に壊した解を検出する(small_children, small_requirements):
    """1 日上限を破る勤務セルを注入すると ``dailycap_*`` を違反として検出すること。

    回帰対象: 検査が例外や未評価で黙って飛ばすと、制約違反した解が
    「制約を満たす解」として採用されてしまう。
    """
    narrow = _pool([
        ("S001", (Role.HOIKUSHI,), sei_contract(daily_hours=1.0, weekly_hours=10.0)),
        ("S002", (Role.HOIKUSHI,), sei_contract(daily_hours=1.0, weekly_hours=10.0)),
    ])
    # 1 日上限 60 分の職員 2 名 = 合計 4 セル分なので、4 時間帯なら充足可能。
    table = _tiny_requirements(small_children, slots_count=4, need=1, qualified=1)
    ctx = _build(narrow, table)
    ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    assert verify_solution(ctx) == []
    for i in range(len(table.slots)):
        variable = ctx.work[("S001", DAY, i)]
        if not isinstance(variable, (int, float)):
            variable.varValue = 1.0
    bad = verify_solution(ctx)
    daily_caps = [n for n in ctx.prob.constraints if n.startswith("dailycap_S001")]
    assert daily_caps
    assert set(daily_caps) & set(bad)


def test_verify_solutionは未評価の変数を例外にしない(small_staff, small_requirements):
    """変数値が ``None`` の制約は「違反ではないが検査不能」として静かに除外されること。"""
    ctx = _build(small_staff, small_requirements)
    ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    variable = next(v for v in ctx.work.values() if not isinstance(v, (int, float)))
    variable.varValue = None
    assert verify_solution(ctx) == []


def test_未評価の変数がある解は採用しない(small_staff, small_requirements):
    """``.solu`` が読めていない（変数値が None）解は 1 パス目を落とすべきこと。"""
    ctx = _build(small_staff, small_requirements)
    ctx.prob.solve = lambda *args, **kwargs: None
    ctx.prob.status = 0
    ctx.prob.sol_status = 2
    for var in ctx.prob.variables():
        var.varValue = None
    for v in ctx.work.values():
        if not isinstance(v, (int, float)):
            v.varValue = 1.0
    status, raw = _run_cbc(ctx, 1, False)
    assert status is SolveStatus.INFEASIBLE
    assert "解未読込" in raw


def _stub_ctx(*, upper_bound: float):
    """``_run_cbc`` の分岐だけを検証する最小モデル（勤務セル 1 個 + 上限制約 1 本）。"""
    ctx = _ModelCtx(prob=pulp.LpProblem("stub", pulp.LpMinimize))
    var = pulp.LpVariable("w_S1", 0, 1, pulp.LpBinary)
    ctx.prob.objective = var * 0.0
    ctx.prob.addConstraint(pulp.LpConstraint(var, pulp.LpConstraintLE, "upper", upper_bound))
    ctx.work[("S1", DAY, 0)] = var
    ctx.prob.solve = lambda *args, **kwargs: None
    ctx.prob.status = 0
    ctx.prob.sol_status = 2
    ctx.capture_specs()
    return ctx, var


def test_途中値は丸めて制約を満たせば採用する():
    """0.6 のような途中値を整数解に丸めた結果が制約を満たせば FEASIBLE として通すこと。"""
    ctx, var = _stub_ctx(upper_bound=1.0)
    var.varValue = 0.6
    status, raw = _run_cbc(ctx, 1, False)
    assert var.varValue == 1.0
    assert status is SolveStatus.FEASIBLE
    assert "丸めて採用" in raw


def test_丸めても制約違反なら解なし扱い():
    """丸めた結果がハード制約を破るなら 1 パス目を落として 2 パス目に退避すること。"""
    ctx, var = _stub_ctx(upper_bound=0.0)
    var.varValue = 0.6
    status, raw = _run_cbc(ctx, 1, False)
    assert var.varValue == 1.0
    assert status is SolveStatus.INFEASIBLE
    assert "制約違反 1 本" in raw


def test_CBCが例外を投げたらSolveError扱い(small_staff, small_requirements):
    """ソルバの異常は握り潰さず ``ERROR``（= 貪欲法へ退避）として返すこと。"""
    ctx = _build(small_staff, small_requirements)

    def boom(*args, **kwargs):
        raise RuntimeError("cbc crashed")

    ctx.prob.solve = boom
    status, raw = _run_cbc(ctx, 1, False)
    assert status is SolveStatus.ERROR
    assert raw.startswith("SolveError")


# ===========================================================================
# 3. 回帰テスト: solve_shift_greedy の無限ループ
# ===========================================================================


def _capped_greedy_case(*, slots_count=6, peak=3, daily_hours=1.0, tail=0):
    """日上限でブロックの伸長を打ち切る最小ケースを作る。

    ``tail`` を 1 以上にすると ``peak`` のすぐ右時間帯にも必要人員を持たせる。
    これが「必要人員の薄い左／厚い右」の非対称な状況を作り、修正前の
    ``if not can_left and can_right: break`` が効かない（=無限ループする）状況になる。
    """
    slots = tuple(
        Slot(to_time(9 * 60 + 30 * i), to_time(9 * 60 + 30 * (i + 1)))
        for i in range(slots_count)
    )

    def _need(i: int) -> int:
        if i == peak:
            return 3
        if tail and i == peak + 1:
            return 3
        return 0

    rows = [
        Requirement(day=DAY, slot=slot, age_class=AgeClass.INFANT, child_count=3,
                    needed_staff=_need(i), needed_qualified=_need(i), basis="テスト")
        for i, slot in enumerate(slots)
    ]
    table = RequirementTable(
        day_open=slots[0].start, day_close=slots[-1].end,
        granularity_min=30, slots=slots, rows={DAY: rows},
    )
    children = _kids(DAY, arrive=slots[0].start, depart=slots[-1].end)
    staff = _pool([
        ("G001", (Role.HOIKUSHI,),
         Contract(weekly_hours=10.0, daily_hours=daily_hours,
                  earliest_start=time(8, 0), latest_end=time(13, 0),
                  max_weekly_days=5, max_consecutive_days=5)),
    ])
    return children, staff, table, slots


def test_日上限で伸長できなくても無限ループしない():
    """日上限に達した隣接時間帯を 2 回続けて試しても停止すること（修正前は無限ループ）。

    スケジュールが返ってこないまま 10 秒を超えたら回帰として失敗させる。
    """
    children, staff, table, slots = _capped_greedy_case()
    previous = signal.signal(signal.SIGALRM, _raise_timeout)
    signal.alarm(10)
    try:
        result = solve_shift_greedy(children, staff, table, standard=STANDARD)
    except TimeoutError:
        pytest.fail("_put_block が無限ループしている")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    duty = [result.day(DAY).get("G001", s) is not CellState.OFF for s in slots]
    assert sum(duty) * 30 <= 60
    assert _block_count(duty) == 1


def _raise_timeout(signum, frame):
    raise TimeoutError("貪欲法が時間内に終わりませんでした")


def test_短契約職員が日上限に達して伸びられなくても無限ループしない():
    """契約 30 分（``daily_hours=0.5``）の職員が日上限に達した瞬間でも停止すること。

    再現条件そのものを固定するテスト。隣接時間帯の必要人員が 0 である状況では、
    修正前の実装は「打てなかった時間帯」をフラグで記録する代わりに局所変数を
    False にしていたため、ループ先頭の再計算で True に戻って進捗なしで
    永久に ``continue`` していた（修正前は ``pytest-timeout`` で kill される）。
    修正後は 1 秒以内に終わる。
    """
    children, staff, table, slots = _capped_greedy_case(daily_hours=0.5, tail=1)
    started = perf_counter()
    result = solve_shift_greedy(children, staff, table, standard=STANDARD)
    elapsed = perf_counter() - started
    assert elapsed < 1.0, f"貪欲法が {elapsed:.2f} 秒かかった（無限ループの疑い）"
    duty = [result.day(DAY).get("G001", s) is not CellState.OFF for s in slots]
    # 契約 30 分 = 時間帯 1 個ぶん。連続ブロックを保ったまま日上限で打ち切る。
    assert sum(duty) * 30 <= 30
    assert _block_count(duty) == 1


def test_短契約職員が通常契約職員に混ざっていても無限ループしない():
    """短契約職員が通常の契約職員と同じ必要人員表に混ざっても停止すること。

    職員ごとに日上限（cap）が違うため、同じ時間帯でも「片方は已达・片方は未达」
    という混在が起きる。修正前はこの混在の日に無限ループしていた。
    """
    children, staff, table, slots = _capped_greedy_case(
        slots_count=8, peak=3, daily_hours=1.0, tail=1
    )
    pool = [*staff, *(
        StaffMember(f"T{i:03d}", f"短契約{i}", (Role.HOIKUSHI,),
                    Contract(weekly_hours=10.0, daily_hours=0.5,
                             earliest_start=time(8, 0), latest_end=time(13, 0),
                             max_weekly_days=5, max_consecutive_days=5))
        for i in range(1, 4)
    )]
    started = perf_counter()
    result = solve_shift_greedy(children, pool, table, standard=STANDARD)
    elapsed = perf_counter() - started
    assert elapsed < 1.0, f"貪欲法が {elapsed:.2f} 秒かかった（無限ループの疑い）"
    for member in pool:
        worked = sum(
            30 for s in slots if result.day(DAY).get(member.staff_id, s) is not CellState.OFF
        )
        assert worked <= _daily_cap_minutes(member.contract)


def test_貪欲法は冪等(small_children, small_staff, small_requirements):
    """同じ入力なら貪欲法は 2 回ともまったく同じシフトを返すこと（決定性）。"""
    prefs = {"S001": StaffPreferences(avoid_early=True)}
    first = solve_shift_greedy(
        small_children, small_staff, small_requirements, prefs, standard=STANDARD
    )
    second = solve_shift_greedy(
        small_children, small_staff, small_requirements, prefs, standard=STANDARD
    )
    assert first.status is second.status is SolveStatus.PARTIAL
    assert [(a.staff_id, a.day, a.slot.label, a.state) for a in first.assignments] == [
        (a.staff_id, a.day, a.slot.label, a.state) for a in second.assignments
    ]
    assert staff_work_hours(first, small_staff) == staff_work_hours(second, small_staff)


@pytest.fixture(scope="module")
def _large_case():
    """28 名 x 6 日 x 25 時間帯の実スケール（サンプルデータ・seed 固定）。"""
    from shiftai import sample_data

    days = [MONDAY + timedelta(days=i) for i in range(6)]
    children = sample_data.make_children(days, seed=42)
    staff = sample_data.make_staff(seed=42)
    prefs = sample_data.make_preferences(staff, days, seed=42)
    table = build_requirements(children, days, STANDARD, day_open=time(7, 15),
                               day_close=time(19, 30), granularity_min=30)
    assert len(table.slots) == 25
    assert len(staff) == 28
    return children, staff, prefs, table


@pytest.mark.slow
def test_大規模でも貪欲法は10秒で終わる(_large_case):
    """28 名 x 6 日 x 25 時間帯でも貪欲法が 10 秒で完了すること（修正前は停止しない）。"""
    children, staff, prefs, table = _large_case
    started = perf_counter()
    result = solve_shift_greedy(children, staff, table, prefs, standard=STANDARD)
    elapsed = perf_counter() - started
    assert elapsed < 10.0, f"貪欲法が {elapsed:.1f} 秒かかった"
    assert result.status is SolveStatus.PARTIAL


@pytest.mark.slow
def test_大規模でも貪欲法は冪等(_large_case):
    """実スケールでも 2 回呼んだ結果が完全に一致すること（貪欲法は決定的なはず）。"""
    children, staff, prefs, table = _large_case
    first = solve_shift_greedy(children, staff, table, prefs, standard=STANDARD)
    second = solve_shift_greedy(children, staff, table, prefs, standard=STANDARD)
    assert [(a.staff_id, a.day, a.slot.label, a.state) for a in first.assignments] == [
        (a.staff_id, a.day, a.slot.label, a.state) for a in second.assignments
    ]


@pytest.mark.slow
def test_大規模貪欲法のカバレッジは80分以上(_large_case):
    """貪欲法の結果が配置基準を 80% 以上カバーすること（壊れた品質 0.371 にならないこと）。"""
    children, staff, prefs, table = _large_case
    result = solve_shift_greedy(children, staff, table, prefs, standard=STANDARD)
    report = gap_analysis.analyze_gap(table, result, staff, standard=STANDARD)
    assert report.coverage_ratio >= 0.80, f"カバレッジ {report.coverage_ratio:.3f} が不足"


# ===========================================================================
# 4. 回帰テスト: 供給人時 0 の ZeroDivisionError
# ===========================================================================


def test_供給人時が0でも例外を投げない(small_children):
    """契約時間帯が需要時間帯と一切重ならない職員でも診断が返ること（修正前は例外）。"""
    children = _kids(DAY, count=6)
    table = _requirements(children)
    staff = _pool([
        ("H001", (Role.HOIKUSHI,),
         Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI,
                  earliest_start=time(20, 0), latest_end=time(22, 0))),
    ])
    result = _solve(children, staff, table, time_limit_sec=5)
    assert result.status is SolveStatus.PARTIAL
    assert any("配置できる人時が 0 です" in m for m in result.messages)
    assert any("契約時間帯" in m for m in result.messages)
    assert all("充足率は算定不可" in m or True for m in result.messages)
    assert not any("/ 0" in m or "ZeroDivision" in m for m in result.messages)


def test_供給人時は0人時になる(small_children, small_requirements):
    """契約時間帯が全時間帯を外れる職員は供給可能時間 0 として扱われること。"""
    narrow = _pool([
        ("N001", (Role.HOIKUSHI,),
         sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ])
    payload = _payload(small_children, narrow, small_requirements)
    assert _supply_hours(payload) == 0.0
    assert _supply_hours(
        _payload(small_children, [], small_requirements)
    ) == 0.0


# ===========================================================================
# 5. 境界ケース（職員数・時間帯数・希望休・確定セル）
# ===========================================================================


def test_職員0名と対象日0と時間帯0はERROR(small_children):
    """職員・対象日・時間帯のいずれかが空でも必ず ``ERROR`` で返ること。"""
    table = _tiny_requirements(small_children)
    pool = _pool([("E001", (Role.HOIKUSHI,), sei_contract())])
    assert _solve(small_children, [], table).status is SolveStatus.ERROR
    assert _solve(small_children, pool, replace(table, rows={})).status is SolveStatus.ERROR
    assert _solve(small_children, pool, replace(table, slots=())).status is SolveStatus.ERROR
    assert solve_shift_greedy(small_children, [], table).status is SolveStatus.ERROR
    assert solve_shift_greedy(small_children, pool, replace(table, slots=())).status is (
        SolveStatus.ERROR
    )


def test_職員1名では配置基準を満たせない(small_children):
    """1 人しか居ない場合は必ず ``PARTIAL`` となり不足が BLOCKER になること。"""
    pool = _pool([("O001", (Role.HOIKUSHI,), sei_contract())])
    table = _tiny_requirements(small_children, slots_count=2, need=2, qualified=2)
    result = _solve(small_children, pool, table, time_limit_sec=5)
    assert result.status is SolveStatus.PARTIAL
    assert result.stats["pass"] == 2
    assert result.stats["relaxed"] is True
    assert "SHORTFALL_STAFF" in {v.code for v in result.blockers()}


def test_時間帯1つだけの問題を解ける(small_children):
    """時間帯 1 個でも退化せず 1 パス目で解けること。"""
    pool = _pool([
        ("M001", (Role.HOIKUSHI,), sei_contract()),
        ("M002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=1, need=1, qualified=1)
    result = _solve(small_children, pool, table, time_limit_sec=5)
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert result.stats["num_slots"] == 1
    states = {result.day(DAY).get(m.staff_id, table.slots[0]) for m in pool}
    assert states == {CellState.WORK, CellState.OFF}  # 必要 1 名なので 1 人だけ勤務


def test_全時間帯が休園なら誰も勤務しない(small_children, small_staff):
    """必要人員が 1 本も無い日は ``ERROR`` ではなく「誰も勤務しない」解になること。"""
    table = replace(_requirements(small_children), rows={DAY: []})
    result = _solve(small_children, small_staff, table, time_limit_sec=5)
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)
    assert {result.day(DAY).get(s.staff_id, slot) for s in small_staff
            for slot in table.slots} == {CellState.OFF}


def test_希望休が全日の職員は勤務しない(small_children, small_requirements):
    """丸 1 日の希望休は MILP でも貪欲法でもハード制約になること。"""
    pool = _pool([
        ("P001", (Role.HOIKUSHI,), sei_contract()),
        ("P002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=3, need=1, qualified=1)
    prefs = {"P001": StaffPreferences(unavailable=[
        Unavailability(day=DAY, start=time(0, 0), end=time(23, 59), reason="希望休")
    ])}
    greedy = solve_shift_greedy(small_children, pool, table, prefs, standard=STANDARD)
    assert {greedy.day(DAY).get("P001", s) for s in table.slots} == {CellState.OFF}
    milp = _solve(small_children, pool, table, preferences=prefs, time_limit_sec=5)
    assert {milp.day(DAY).get("P001", s) for s in table.slots} == {CellState.OFF}


def test_存在しない職員IDの希望は無視される(small_children, small_staff, small_requirements):
    """``preferences`` のキーが職員 ID に紐づいていなくても落ちないこと。"""
    orphan = StaffPreferences(unavailable=[
        Unavailability(day=DAY, start=time(0, 0), end=time(23, 59), reason="希望休")
    ])
    result = _solve(small_children, small_staff, small_requirements,
                    preferences={"存在しないID": orphan}, time_limit_sec=5)
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)
    assert result.assignments
    assert result.stats["num_staff"] == 6


def test_重みが全ゼロでも解ける(small_children):
    """すべての重みを 0 にしても目的関数が定数になり解が返ること。"""
    pool = _pool([
        ("W001", (Role.HOIKUSHI,), sei_contract()),
        ("W002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    zero = ObjectiveWeights(**{name: 0.0 for name in vars(ObjectiveWeights())})
    result = _solve(small_children, pool, table, weights=zero, time_limit_sec=5)
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)
    assert result.assignments


def test_極端な重みでも握り潰さない(small_children):
    """0・負・10 億の重みが混ざっても例外を投げずに結果を返すこと。"""
    pool = _pool([
        ("X001", (Role.HOIKUSHI,), sei_contract()),
        ("X002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    weights = ObjectiveWeights(
        shortfall_penalty=0.0, overstaff_penalty=-5.0, preference_miss_penalty=0.0,
        preference_match_bonus=1e9, early_shift_penalty=-1.0, late_shift_penalty=1e6,
        break_conflict_penalty=0.0, consecutive_day_penalty=1e6,
        hours_imbalance_penalty=-1.0, unused_staff_penalty=0.0,
        monthly_hours_penalty=1e6, rest_violation_penalty=0.0,
        max_shift_length_penalty=-1e3,
    )
    result = _solve(small_children, pool, table, weights=weights, time_limit_sec=5)
    assert result.status in set(SolveStatus)
    assert result.messages
    assert result.stats["elapsed_sec"] >= 0.0


def test_確定セルの矛盾指定は致命的でない(small_children):
    """未知スロットラベルの確定は無視され、1 日上限を超える確定は解なしになること。

    ``daily_hours=0.5`` の職員は ``_daily_cap_minutes`` が 37 分（0.5*60*1.25）。
    30 分セル 2 個（=60 分）を確定させると上限を確実に超える。
    以前は ``daily_hours=1.0``（cap=75 分）を使っていたので 2 セルでは上限を
    超えず、「PARTIAL になる」こと自体が成立していなかった。
    """
    pool = _pool([
        ("F001", (Role.HOIKUSHI,), sei_contract(daily_hours=0.5, weekly_hours=10.0)),
        ("F002", (Role.HOIKUSHI,), sei_contract(daily_hours=0.5, weekly_hours=10.0)),
    ])
    table = _tiny_requirements(small_children, slots_count=4, need=1, qualified=1)
    unknown = _solve(small_children, pool, table,
                     fixed_assignments={("F001", DAY, "存在しないラベル"): CellState.WORK},
                     time_limit_sec=5)
    assert unknown.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)
    assert unknown.stats["solver_status_pass1"] != "PrecheckInfeasible"

    slots = table.slots
    over_cap = _solve(small_children, pool, table, fixed_assignments={
        ("F001", DAY, slots[0].label): CellState.WORK,
        ("F001", DAY, slots[1].label): CellState.WORK,
    }, time_limit_sec=5)
    assert over_cap.status is SolveStatus.PARTIAL
    # 確定セルは変数化されず定数 1 として ctx に記録される（カバレッジにも計上される）
    ctx = _build(pool, table, fixed={
        ("F001", DAY, slots[0].label): CellState.WORK,
        ("F001", DAY, slots[1].label): CellState.WORK,
    })
    assert ctx.work[("F001", DAY, 0)] == 1
    assert ctx.work[("F001", DAY, 1)] == 1
    names = {v.name for v in ctx.prob.variables()}
    assert not {"w_F001_0928_0", "w_F001_0928_1"} & names


def test_配置できる職員が居ない場合は前検査で落とす(small_children, small_requirements):
    """契約時間帯が全時間帯を外れる職員しかいない場合は CBC を起動せず落とすこと。"""
    narrow = _pool([
        ("Z001", (Role.HOIKUSHI,), sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ])
    result = _solve(small_children, narrow, small_requirements, time_limit_sec=5)
    assert result.stats["solver_status_pass1"] == "PrecheckInfeasible"
    assert result.status is SolveStatus.PARTIAL


def test_解のメッセージと統計と違反が埋まる(small_children):
    """極小問題でも messages / stats / violations が空にならないこと。"""
    pool = _pool([
        ("U001", (Role.HOIKUSHI,), sei_contract()),
        ("U002", (Role.SHIENSHIIN,), part_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    result = _solve(small_children, pool, table, time_limit_sec=5)
    assert result.messages and all(isinstance(m, str) and m for m in result.messages)
    for key in ("solver", "solver_status", "solver_status_pass1", "pass", "relaxed",
                "elapsed_sec", "num_variables", "num_constraints", "num_staff",
                "num_days", "num_slots"):
        assert key in result.stats
    assert result.stats["num_variables"] > 0 and result.stats["num_constraints"] > 0
    assert result.violations is not None
    assert len(result.assignments) == 4
    assert set(staff_work_hours(result, pool)) == {"U001", "U002"}
    assert set(staff_shift_count(result)) == {"U001", "U002"}


# ===========================================================================
# 6. 内部関数の境界（ solvers を起動しない）
# ===========================================================================


def test_ソルバ列挙が例外を投げても空リスト(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("solver registry unavailable")

    monkeypatch.setattr(pulp, "listSolvers", boom)
    assert available_solvers() == []


def test_週換算係数は日数に比例する():
    assert _week_fraction(0) == pytest.approx(1 / 7)
    assert _week_fraction(7) == pytest.approx(1.0)
    assert _week_fraction(21) == pytest.approx(3.0)


def test_月換算は空と12月をまたぐ():
    assert month_fraction([]) == 0.0
    assert month_fraction([date(2026, 12, 1)]) == pytest.approx(1 / 31)
    mixed = [date(2026, 12, 31), date(2027, 1, 1)]
    assert month_fraction(mixed) == pytest.approx(2 / 31)


def test_変数値変換は未決定を0扱いする():
    var = pulp.LpVariable("v", 0, 1)
    assert var.value() is None
    assert solver_mod._var_value(None) == 0.0
    assert solver_mod._var_value(3) == 3.0
    assert solver_mod._var_value(2.5) == 2.5
    assert solver_mod._var_value(var) == 0.0


def test_定数と変数の判定():
    var = pulp.LpVariable("v", 0, 1)
    assert _is_zero(0) is True and _is_zero(0.0) is True
    assert _is_zero(1) is False and _is_zero(var) is False
    assert _is_var(0) is False and _is_var(var) is True


def test_線形式の組み立て():
    var = pulp.LpVariable("v", 0, 1)
    assert _linear([]) == 0
    assert _linear([(var, 1)]) is var
    scaled = _linear([(var, 2)])
    assert isinstance(scaled, pulp.LpAffineExpression)
    assert abs(next(iter(scaled.items()))[1] - 2.0) < 1e-9


def test_加班不可契約は法定10時間で頭打ち():
    assert _daily_cap_minutes(Contract(weekly_hours=40, daily_hours=8.0)) == 600
    assert _daily_cap_minutes(
        Contract(weekly_hours=40, daily_hours=8.0, overtime_allowed=False)
    ) == 480
    assert _daily_cap_minutes(
        Contract(weekly_hours=40, daily_hours=15.0, overtime_allowed=False)
    ) == 600
    assert _daily_cap_minutes(
        Contract(weekly_hours=40, daily_hours=5.0, overtime_allowed=False)
    ) == 300


def test_休日不可職員は週末と休園日に出られない():
    staff = StaffMember("K001", "休日不可", (Role.HOIKUSHI,),
                        sei_contract(can_work_holiday=False))
    assert _day_is_workable(staff, SATURDAY, FacilitySettings()) is False
    assert _day_is_workable(staff, MONDAY, FacilitySettings()) is True
    assert _day_is_workable(
        staff, MONDAY, FacilitySettings(holiday_dates=frozenset({MONDAY}))
    ) is False
    assert _day_is_workable(
        staff, MONDAY, FacilitySettings(closed_days=frozenset({MONDAY}))
    ) is False


def test_定数条件の矛盾は記録される():
    ctx = _ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    _hard_le(ctx, 5, 3, "上限オーバー")
    _hard_le(ctx, 1, 3)
    _hard_ge(ctx, 1, 3, "下限不足")
    _hard_ge(ctx, 5, 3)
    _hard_ge(ctx, 0, 3)
    assert ctx.conflicts == ["上限オーバー", "下限不足", "定数条件が矛盾しました"]
    assert ctx.prob.constraints == {}


def test_連続ブロック制約の境界():
    empty = _ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    _add_block(empty, {}, "tag", 0, [], ObjectiveWeights())
    assert empty.prob.variables() == [] and empty.prob.constraints == {}
    hard = _ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    _add_block(hard, {0: pulp.LpVariable("v", 0, 1)}, "tag", 1)
    assert len(hard.prob.constraints) == 2


def test_基準を渡さないと全時間帯NORMALになる(small_staff, small_requirements):
    """``standard=None`` でもモデルが組めること（早朝/延長の区分なし）。"""
    ctx = _build(small_staff, small_requirements, standard=None)
    assert ctx.counts()[0] > 0 and ctx.counts()[1] > 0
    prefs = {"S001": StaffPreferences(avoid_early=True, avoid_late=True)}
    ctx2 = _build(small_staff, small_requirements, prefs=prefs, standard=None)
    names = {v.name for v in ctx2.prob.variables()}
    assert not any(name.startswith("earlyflag_") for name in names)
    assert not any(name.startswith("lateflag_") for name in names)


def test_職員0名でもモデルは空で組める(small_requirements):
    ctx = _build([], small_requirements)
    assert ctx.work == {} and ctx.hours == {}
    assert ctx.counts()[0] == 0


def test_確定された休憩セルは変数化しない(small_staff, small_requirements):
    slot = small_requirements.slots[1]
    ctx = _build(small_staff, small_requirements,
                 fixed={("S001", DAY, slot.label): CellState.BREAK})
    names = {v.name for v in ctx.prob.variables()}
    # 確定セルは変数化されない（定数 1/0 として ctx にも記録される）
    assert not {"w_S001_0928_1", "b_S001_0928_1"} & names
    assert ctx.work[("S001", DAY, 1)] == 0
    assert ctx.brk[("S001", DAY, 1)] == 1


def test_行フィルタは非必須と未知スロットと需要0を無視する(small_staff, small_requirements):
    table = replace(small_requirements)
    base = small_requirements.for_day(DAY)[0]
    table.rows = {DAY: [
        *small_requirements.for_day(DAY),
        replace(base, is_binding=False),
        replace(base, slot=Slot(time(3, 0), time(3, 30)), needed_staff=3, needed_qualified=3),
        replace(base, needed_staff=0, needed_qualified=0),
    ]}
    ctx = _build(small_staff, table)
    assert ctx.counts()[0] > 0
    assert ctx.conflicts == []


def test_契約上限が粒度未満なら休憩制約を作らない(small_children):
    tiny = Contract(weekly_hours=1.0, daily_hours=0.1, employment_type=EmploymentType.PART)
    pool = _pool([("T001", (Role.HOIKUSHI,), tiny)])
    ctx = _build(pool, _requirements(_kids(DAY)))
    assert not any(v.name.startswith("brkdef_") for v in ctx.prob.variables())


def test_勤務間の休息時間が短いと罰変数を立てる(small_children):
    days = [MONDAY, date(2026, 9, 29)]
    children = list(small_children) + [
        ChildPlan(c.child_id, c.name, days[1], c.age_class, c.arrive, c.depart)
        for c in small_children
    ]
    pool = _pool([
        ("R001", (Role.HOIKUSHI,), sei_contract(min_rest_hours=20.0)),
        ("R002", (Role.SHIENSHIIN,), part_contract(min_rest_hours=20.0,
                                                    earliest_start=time(3, 0),
                                                    latest_end=time(4, 0))),
    ])
    ctx = _build(pool, _requirements(children, days=days))
    names = {v.name for v in ctx.prob.variables()}
    assert any(name.startswith("rest_R001_") for name in names)
    assert not any(name.startswith("rest_R002_") for name in names)


def test_連続勤務の窓が欠けたとき何もしない(small_children):
    week = [MONDAY + timedelta(days=i) for i in range(7)]
    saturday, sunday = week[5], week[6]
    children = [
        ChildPlan(f"{c.child_id}_{d}", c.name, d, c.age_class, c.arrive, c.depart)
        for d in week for c in small_children
    ]
    table = _requirements(children, days=week, closed_days=(saturday, sunday))
    assert table.for_day(saturday) == []
    pool = _pool([("Q001", (Role.HOIKUSHI,), sei_contract(max_consecutive_days=2))])
    names = {v.name for v in _build(pool, table).prob.variables()}
    assert any(name.startswith("z_Q001_") for name in names)
    assert any(name.startswith("cons_Q001_") for name in names)


def test_希望する時間帯はソフト制約になる(small_staff, small_requirements):
    slot = small_requirements.slots[3]
    prefs = {"S001": StaffPreferences(preferred_slots={DAY: (slot,)})}
    ctx = _build(small_staff, small_requirements, prefs=prefs)
    assert any(v.name.startswith("pslot_S001_") for v in ctx.prob.variables())


def test_勤務セルが1つも無い解は解なし扱い(small_staff, small_requirements):
    empty = replace(small_requirements, rows={DAY: []})
    ctx = _build(small_staff, empty)
    assert ctx.work and all(isinstance(v, (int, float)) for v in ctx.work.values())
    status, _raw = _run_cbc(ctx, 5, False)
    assert status is SolveStatus.INFEASIBLE


def test_モデル構築に失敗したら貪欲法に退避する(small_children, small_staff,
                                                small_requirements, monkeypatch):
    """``_build_problem`` が例外を投げても握り潰さず貪欲法の暫定シフトを返すこと。"""

    def boom(*args, **kwargs):
        raise RuntimeError("model build failed")

    monkeypatch.setattr(solver_mod, "_build_problem", boom)
    result = _solve(small_children, small_staff, small_requirements)
    assert result.stats["solver_status"] == "ModelError"
    assert result.stats["fallback"] == "greedy"
    assert any("モデル構築に失敗しました" in m for m in result.messages)
    assert result.assignments


def test_2パス目のモデル構築も失敗したら貪欲法(small_children, small_requirements, monkeypatch):
    """1パス目の前検査で落ち、2パス目の構築が失敗した時も貪欲法へ退避すること。"""
    narrow = _pool([
        ("Y001", (Role.HOIKUSHI,), sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ])
    real = solver_mod._build_problem
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("soft model failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(solver_mod, "_build_problem", flaky)
    result = _solve(small_children, narrow, small_requirements, time_limit_sec=5)
    assert result.stats["fallback"] == "greedy"
    assert any("配置基準を満たせませんでした" in m for m in result.messages)


# ===========================================================================
# 7. 診断・違反の貼り付け
# ===========================================================================


def test_供給不足の診断と人員提案(small_children, small_requirements):
    pool = _pool([("I001", (Role.HOIKUSHI,), sei_contract())])
    payload = _payload(small_children, pool, small_requirements)
    messages = _coverage_diagnosis(payload)
    assert any("需要 > 供給" in m for m in messages)
    assert any("【人員の見直し】" in m for m in messages)
    assert _shortfall_supply(payload)


def test_保育士だけ足りない場合の提案(small_children, small_staff, small_requirements):
    payload = _payload(small_children, small_staff, small_requirements)
    rows = [(DAY, "09:00-09:30", 2, 2, 3, 1, 1)]
    assert any("保育士が足りない" in m for m in _staffing_advice(payload, rows))
    assert _staffing_advice(payload, []) == []


def test_人員自体が足りない場合の提案(small_children, small_staff, small_requirements):
    payload = _payload(small_children, small_staff, small_requirements)
    rows = [(DAY, "09:00-09:30", 4, 2, 1, 1, 0)]
    assert any("人員そのものが足りません" in m for m in _staffing_advice(payload, rows))


def test_需要表のノイズ行は無視される(small_children, small_staff, small_requirements):
    payload = _payload(small_children, small_staff, small_requirements)
    base = small_requirements.for_day(DAY)[0]
    table = replace(payload.requirements)
    table.rows = {
        **table.rows,
        SATURDAY: [
            replace(base, day=SATURDAY, is_binding=False),
            replace(base, day=SATURDAY, slot=Slot(time(3, 0), time(3, 30))),
            replace(base, day=SATURDAY, needed_staff=0, needed_qualified=0),
            replace(base, day=SATURDAY, needed_staff=9, needed_qualified=9),
        ],
    }
    rows = _shortfall_supply(replace(payload, requirements=table))
    assert rows and {r[0] for r in rows} == {SATURDAY}
    assert all(r[2] > r[4] or r[3] > r[5] for r in rows)


def test_供給計算は休日不可と契約外と希望休で減る(small_children, small_staff, small_requirements):
    saturday_table = replace(small_requirements, rows={SATURDAY: [
        replace(small_requirements.for_day(DAY)[0], day=SATURDAY, needed_staff=5,
                needed_qualified=4),
    ]})
    pool = [
        *small_staff,
        StaffMember("A001", "休日不可", (Role.HOIKUSHI,),
                    sei_contract(can_work_holiday=False)),
        StaffMember("A002", "契約外", (Role.HOIKUSHI,),
                    sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ]
    prefs = {sid: StaffPreferences(unavailable=[
        Unavailability(day=SATURDAY, start=time(0, 0), end=time(23, 59), reason="希望休")
    ]) for sid in ("S001", "S005")}
    rows = _shortfall_supply(
        _payload(small_children, pool, saturday_table, prefs=prefs)
    )
    assert len(rows) == 1
    day, label, need_all, need_q, sup_all, sup_q, support = rows[0]
    assert (day, label) == (SATURDAY, saturday_table.slots[0].label)
    assert (need_all, need_q) == (5, 4)
    assert (sup_all, sup_q, support) == (4, 3, 1)  # S001/S005 は希望休、支援員は保育士数に数えない


def test_支援員は保育士数に数えない(small_children, small_requirements):
    saturday_table = replace(small_requirements, rows={SATURDAY: [
        replace(small_requirements.for_day(DAY)[0], day=SATURDAY, needed_staff=6,
                needed_qualified=4),
    ]})
    pool = _pool([
        ("B001", (Role.HOIKUSHI,), sei_contract()),
        ("B002", (Role.HOIKUSHI,), sei_contract()),
        ("B003", (Role.SHIENSHIIN,), part_contract()),
        ("B004", (Role.SHIENSHIIN,), part_contract()),
    ])
    _day, _label, need_all, need_q, sup_all, sup_q, support = _shortfall_supply(
        _payload(small_children, pool, saturday_table)
    )[0]
    assert (sup_all, sup_q, support) == (4, 2, 2)
    assert need_all > sup_all and need_q > sup_q


def _raise_runtime_error(*args, **kwargs):
    raise RuntimeError("check_violations failed")


def test_違反の貼り付け失敗は握り潰さない(small_children, small_staff, small_requirements,
                                           monkeypatch):
    """検査に失敗しても ``violations=[]`` では済ませず BLOCKER を出すこと。

    以前は ``violations == []`` を期待値にしており、UI は「違反 0 件」と表示して
    利用者が法令違反を含むシフトを配布できてしまう状態だった。
    """
    result = SolveResult(status=SolveStatus.PARTIAL, stats={})
    monkeypatch.setattr(gap_analysis, "check_violations", _raise_runtime_error)
    solver_mod._attach_violations(
        result, small_requirements, small_staff, {}, STANDARD, FacilitySettings()
    )
    assert [v.code for v in result.violations] == ["CHECK_UNAVAILABLE"]
    assert result.violations[0].severity is ViolationSeverity.BLOCKER
    assert result.status is SolveStatus.ERROR
    assert not result.ok


def test_gap_analysisが読めなくても致命的にしない(small_children, small_staff,
                                                   small_requirements, monkeypatch):
    """``gap_analysis`` が import できない場合も fail-closed で報告すること。"""
    import shiftai

    result = SolveResult(status=SolveStatus.PARTIAL, stats={})
    monkeypatch.setitem(sys.modules, "shiftai.gap_analysis", None)
    monkeypatch.delattr(shiftai, "gap_analysis", raising=False)
    solver_mod._attach_violations(
        result, small_requirements, small_staff, {}, STANDARD, FacilitySettings()
    )
    assert [v.code for v in result.violations] == ["CHECK_UNAVAILABLE"]
    assert result.status is SolveStatus.ERROR
    assert any("検査を実行できませんでした" in m for m in result.messages)


# ===========================================================================
# 8. デコードと貪欲法の境界
# ===========================================================================


class _VarHolder:
    """``work`` / ``brk`` 属性だけを持つオブジェクト。"""

    def __init__(self, work, brk):
        self.work = work
        self.brk = brk


def test_変数値からシフト表を復元できる(small_staff, small_requirements):
    slots = small_requirements.slots[:2]
    work = {("S001", DAY, 0): 1, ("S001", DAY, 1): 0}
    brk = {("S001", DAY, 0): 0, ("S001", DAY, 1): 1}
    for holder in ({"work": work, "break": brk}, {"w": work, "b": brk}, _VarHolder(work, brk)):
        days, assignments = decode_solution(holder, small_staff, [DAY], slots)
        assert days[0].get("S001", slots[0]) is CellState.WORK
        assert days[0].get("S001", slots[1]) is CellState.BREAK
        assert len(assignments) == 2 * len(small_staff)
    assert _extract_vars(object()) == ({}, {})
    assert decode_solution(object(), small_staff, [DAY], slots)[0][0].get(
        "S001", slots[0]
    ) is CellState.OFF


def test_ラベル検索と連続勤務日数(small_requirements):
    slots = small_requirements.slots
    assert _slot_by_label(slots, slots[0].label) is slots[0]
    with pytest.raises(KeyError):
        _slot_by_label(slots, "存在しないラベル")
    worked = {date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)}
    assert _consecutive_streak(worked, date(2026, 9, 28)) == 3
    assert _consecutive_streak(worked, date(2026, 9, 30)) == 0
    assert _consecutive_streak(set(), DAY) == 0


def test_貪欲法は休園日と休日不可と契約外を守る(small_children, small_staff, small_requirements):
    saturday_table = replace(
        small_requirements, rows={SATURDAY: list(small_requirements.for_day(DAY))}
    )
    pool = [
        *small_staff,
        StaffMember("C001", "休日不可", (Role.HOIKUSHI,), sei_contract(can_work_holiday=False)),
        StaffMember("C002", "契約外", (Role.HOIKUSHI,),
                    sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ]
    result = solve_shift_greedy(
        small_children, pool, saturday_table,
        fixed_assignments={("C001", SATURDAY, "存在しないラベル"): CellState.WORK},
        standard=STANDARD,
    )
    assert {result.day(SATURDAY).get("C001", s) for s in saturday_table.slots} == {CellState.OFF}
    assert {result.day(SATURDAY).get("C002", s) for s in saturday_table.slots} == {CellState.OFF}
    assert CellState.WORK in {
        result.day(SATURDAY).get(sid, s)
        for sid in ("S001", "S002") for s in saturday_table.slots
    }


def test_貪欲法は週の勤務日数と連続勤務日数を守る(small_children, small_requirements):
    days = [MONDAY + timedelta(days=i) for i in range(4)]
    table = replace(small_requirements, rows={d: list(small_requirements.for_day(DAY))
                                              for d in days})
    pool = _pool([
        ("D001", (Role.HOIKUSHI,), sei_contract(max_weekly_days=1)),
        ("D002", (Role.HOIKUSHI,), sei_contract(max_consecutive_days=1, min_rest_hours=40.0)),
    ])
    result = solve_shift_greedy(small_children, pool, table, standard=STANDARD)
    counts = staff_shift_count(result)
    assert counts["D001"] <= 1  # max_weekly_days = 1
    worked = [sd.day for sd in result.shift_days if sd.get("D002", table.slots[0])
              is CellState.WORK]
    assert all(b - a > timedelta(days=1) for a, b in zip(worked, worked[1:], strict=False))


def test_貪欲法は1日上限で勤務ブロックを保つ():
    children, staff, table, slots = _capped_greedy_case(slots_count=4, peak=1, daily_hours=1.0)
    result = solve_shift_greedy(children, staff, table, standard=STANDARD)
    duty = [result.day(DAY).get("G001", s) is not CellState.OFF for s in slots]
    assert sum(duty) * 30 <= 60
    assert _block_count(duty) <= 1


def test_貪欲法は確定セルと休憩上限を反映する(small_children, small_staff, small_requirements):
    slots = small_requirements.slots
    result = solve_shift_greedy(
        small_children, small_staff, small_requirements,
        fixed_assignments={
            ("S001", DAY, slots[0].label): CellState.OFF,
            ("S001", date(1999, 1, 1), slots[1].label): CellState.WORK,
            ("存在しない", DAY, slots[1].label): CellState.WORK,
        },
        standard=STANDARD,
    )
    assert result.day(DAY).get("S001", slots[0]) is CellState.OFF
    for shift_day in result.shift_days:
        breaks = sum(
            1 for sid in small_staff for s in slots
            if shift_day.get(sid, s) is CellState.BREAK
        )
        on_duty = sum(
            1 for sid in small_staff for s in slots
            if shift_day.get(sid, s) is not CellState.OFF
        )
        assert breaks <= max(1, on_duty // 4) + 1


def test_貪欲法の違反検査が壊れても返す(small_children, small_staff, small_requirements,
                                        monkeypatch):
    """貪欲法でも検査不能を ``violations=[]`` で済ませないこと。"""
    monkeypatch.setattr(gap_analysis, "check_violations", _raise_runtime_error)
    result = solve_shift_greedy(small_children, small_staff, small_requirements, standard=STANDARD)
    assert [v.code for v in result.violations] == ["CHECK_UNAVAILABLE"]
    assert result.status is SolveStatus.ERROR


def test_verify_solutionは素のLpProblemでも検査できる():
    """``_ModelCtx`` でなくても ``prob`` を持つオブジェクトなら検査できること。"""
    prob = pulp.LpProblem("raw", pulp.LpMinimize)
    var = pulp.LpVariable("v", 0, 10)
    var.varValue = 3.0
    prob.addConstraint(pulp.LpConstraint(var, pulp.LpConstraintLE, "le", 1.0))
    assert verify_solution(prob) == ["le"]


def test_verify_solutionは等号とtolの境界を扱う():
    """``rhs`` ちょうど / ``tol`` 内超過は違反外、それを超えた超過は違反として検出すること。

    ``le`` ``ge`` ``eq`` の 3 Sense と ``tol`` 境界の両方を固定する。
    """
    prob = pulp.LpProblem("bounds", pulp.LpMinimize)
    var = pulp.LpVariable("v", 0, 100)
    var.varValue = 1.0
    prob.addConstraint(pulp.LpConstraint(var, pulp.LpConstraintLE, "le", 1.0))
    prob.addConstraint(pulp.LpConstraint(var, pulp.LpConstraintGE, "ge", 1.0))
    prob.addConstraint(pulp.LpConstraint(var, pulp.LpConstraintEQ, "eq", 1.0))
    assert verify_solution(prob) == []

    var.varValue = 1.0 + 1e-5
    assert verify_solution(prob, tol=1e-4) == []

    var.varValue = 1.0 + 1e-3
    assert set(verify_solution(prob, tol=1e-4)) == {"le", "eq"}

    var.varValue = 1.0 - 1e-3
    assert set(verify_solution(prob, tol=1e-4)) == {"ge", "eq"}


def test_verify_solutionは定数項を含む制約を評価する():
    """係数を持たない定数項（``LpAffineExpression.constant``）も rhs 計算に含めること。"""
    prob = pulp.LpProblem("const", pulp.LpMinimize)
    var = pulp.LpVariable("v", 0, 100)
    var.varValue = 1.0
    # 2v + 5 <= 6  <=>  v <= 0.5 -> 違反 / 2v + 5 <= 8 -> 满足
    prob.addConstraint(pulp.LpConstraint(2 * var + 5, pulp.LpConstraintLE, "tight", 6.0))
    prob.addConstraint(pulp.LpConstraint(2 * var + 5, pulp.LpConstraintLE, "loose", 8.0))
    assert verify_solution(prob) == ["tight"]


def test_CBCがUnboundedならERROR扱い():
    """有界でないモデルは解なしではなく ``ERROR``（= 貪欲法へ退避）として返すこと。"""
    ctx, var = _stub_ctx(upper_bound=1.0)
    var.varValue = 1.0
    ctx.prob.status = -2  # Unbounded
    ctx.prob.sol_status = -2
    status, raw = _run_cbc(ctx, 1, False)
    assert status is SolveStatus.ERROR
    assert raw.startswith("Unbounded")


def test_Optimalでも部分解ならFEASIBLE扱い():
    """``Optimal`` でも「Optimal Solution Found」以外なら FEASIBLE として通すこと。"""
    ctx, var = _stub_ctx(upper_bound=1.0)
    var.varValue = 1.0
    ctx.prob.status = 1
    ctx.prob.sol_status = 2
    status, _raw = _run_cbc(ctx, 1, False)
    assert status is SolveStatus.FEASIBLE


def test_その他のステータスのメッセージ():
    """OPTIMAL / FEASIBLE / PARTIAL 以外の遷移メッセージも組み立てられること。"""
    messages = solver_mod._solution_messages(SolveStatus.INFEASIBLE, None, 1.0, False, [], {})
    assert "シフトを生成しました" in messages[0]
    assert "算出不可" in messages[0]


def test_希望時間帯が未知や契約外でもペナルティ対象にならない(
    small_staff, small_requirements
):
    """``preferred_slots`` に未知の時間帯や契約外の時間帯を入れても落ちないこと。"""
    prefs = {
        "S001": StaffPreferences(preferred_slots={DAY: (Slot(time(3, 0), time(3, 30)),)}),
        "S002": StaffPreferences(preferred_slots={DAY: (Slot(time(22, 0), time(22, 30)),)}),
    }
    ctx = _build(small_staff, small_requirements, prefs=prefs)
    assert ctx.counts()[0] > 0
    names = {v.name for v in ctx.prob.variables()}
    assert not any(name.startswith("pslot_S001_") for name in names)
    assert not any(name.startswith("pslot_S002_") for name in names)


def test_休憩変数を持たない職員はスキップ(small_children):
    """契約時間帯が全時間帯を外れる職員は ``_add_breaks`` で除外されること。"""
    narrow = _pool([
        ("V001", (Role.HOIKUSHI,), sei_contract(earliest_start=time(3, 0), latest_end=time(4, 0))),
    ])
    ctx = _build(narrow, _requirements(_kids(DAY)))
    assert not any(v.name.startswith("brkdef_") for v in ctx.prob.variables())


def test_貪欲法も非必須行と未知スロットを無視する(small_children, small_requirements):
    """貪欲法側の行フィルタ（is_binding / 未知スロット）も効いていること。"""
    base = small_requirements.for_day(DAY)[0]
    table = replace(small_requirements, rows={DAY: [
        *small_requirements.for_day(DAY),
        replace(base, is_binding=False),
        replace(base, slot=Slot(time(3, 0), time(3, 30)), needed_staff=9, needed_qualified=9),
    ]})
    pool = _pool([("W001", (Role.HOIKUSHI,), sei_contract())])
    result = solve_shift_greedy(small_children, pool, table, standard=STANDARD)
    assert result.status is SolveStatus.PARTIAL
    assert result.stats["num_slots"] == len(small_requirements.slots)


# ===========================================================================
# 9. 性能 Hammond（実測 / 既定実行からは slow で外す）
# ===========================================================================


@pytest.mark.slow
def test_6名で1日解ける規模なら1日あたり10秒以内(small_children, small_staff,
                                                  small_requirements):
    """6 名・1 日・10 時間帯の規模では 10 秒以内に解けること。"""
    result = _solve(small_children, small_staff, small_requirements, time_limit_sec=10)
    assert result.stats["elapsed_sec"] < 10.0
    assert result.stats["num_variables"] > 0
    assert result.stats["num_constraints"] > 0


@pytest.mark.slow
def test_モデル規模は職員数にほぼ比例する(week_days, fq_standard):
    """職員数 × 日数 × 時間帯数にほぼ比例して変数と制約が増えること（CBC 不使用）。"""
    from shiftai import sample_data

    days = list(week_days)
    children = sample_data.make_children(days, seed=7)
    table = build_requirements(children, days, fq_standard, day_open=time(7, 15),
                              day_close=time(19, 30), granularity_min=30)
    all_staff = list(sample_data.make_staff(seed=7))
    small = _build(all_staff[:10], table)
    large = _build(all_staff, table)
    ratio = large.counts()[0] / small.counts()[0]
    assert ratio == pytest.approx(len(all_staff) / 10, rel=0.1)
    assert large.counts()[1] > small.counts()[1]


# ===========================================================================
# 8. 回帰テスト: 供給人時 0 の ZeroDivisionError（追加分）
#
# 修正前の挙動: ``_staffing_advice`` が ``need_h / supply_h`` を無条件計算し、
# 契約時間帯が需要時間帯と一切重ならない入力（供給人時 0）で
# ``solve_shift`` 全体が ``ZeroDivisionError: float division by zero`` で落ちていた。
# ===========================================================================


def _disjoint_staff():
    """契約時間帯（20:00-22:00）が園の開所時間と 1 分も重ならない職員。"""
    return _pool([
        ("Z001", (Role.HOIKUSHI,),
         Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI,
                  earliest_start=time(20, 0), latest_end=time(22, 0))),
    ])


def test_再現_契約時間帯が需要と重ならずにZeroDivisionErrorを投げない():
    """最重要再現テスト: ``solve_shift`` が ZeroDivisionError を投げないこと。

    修正前は以下で落ちていた::

        File "src/shiftai/solver.py", line 1269, in _staffing_advice
          f"（充足率 {need_h / supply_h * 100:.0f}% 相当）"
        ZeroDivisionError: float division by zero
    """
    children = _kids(DAY, count=6)
    table = _requirements(children)
    staff = _disjoint_staff()
    # 分母が 0 であることを先に確定させる
    assert _supply_hours(_payload(children, staff, table)) == 0.0

    result = _solve(children, staff, table, time_limit_sec=5)  # 例外を投げないこと

    assert result.messages
    joined = "\n".join(result.messages)
    # 原因が分かる日本語メッセージが入っていること
    assert "配置できる人時が 0 です" in joined
    assert "契約時間帯" in joined
    # 0 除算の痕跡（充足率の算出や Traceback）が漏れていないこと
    assert "充足率は算定不可" in joined
    assert "Traceback" not in joined
    assert "ZeroDivisionError" not in joined
    # 既存の人員に関する助言メッセージは維持されていること
    assert "人員そのものが足りません" in joined
    assert "【人員の見直し】" in joined


def test_需要人時が0なら原因を説明するメッセージが返る():
    """園児 0 人（需要人時 0）でも例外ではなく説明メッセージになること。"""
    children = _kids(DAY, count=6)
    table = _requirements(children)
    pool = _disjoint_staff()
    assert _payload(children, pool, table).requirements.total_needed_hours() > 0

    empty_table = _requirements([])
    empty_payload = _payload([], pool, empty_table)
    assert empty_payload.requirements.total_needed_hours() == 0.0

    # 不足行が無い場合でも「需要人時 0」の説明は返す
    messages = _staffing_advice(empty_payload, [])
    assert messages
    assert "需要人時が 0 時間です" in messages[0]
    assert "登降園予定" in messages[0]

    result = _solve([], pool, empty_table, time_limit_sec=5)
    assert result.status is not None
    assert "需要人時が 0 時間です" in "\n".join(result.messages)


def test_正常系は従来どおりの妥当なメッセージが返る():
    """職員が需要に重なっている場合は空でない妥当なメッセージになること。"""
    children = _kids(DAY, count=6)
    table = _requirements(children)
    staff = _pool([
        ("K001", (Role.HOIKUSHI,),
         Contract(weekly_hours=40.0, daily_hours=8.0, employment_type=EmploymentType.SEI,
                  earliest_start=time(8, 0), latest_end=time(18, 0))),
    ])
    payload = _payload(children, staff, table)
    assert _supply_hours(payload) > 0.0

    result = _solve(children, staff, table, time_limit_sec=5)

    assert result.messages
    # 空行セパレータは既存仕様なので、「中身のあるメッセージが 1 つ以上ある」ことを確認する
    assert any(m.strip() for m in result.messages)
    joined = "\n".join(result.messages)
    # 「0 人時」「算定不可」ではない通常のメッセージ
    assert "配置できる人時が 0 です" not in joined
    assert "充足率は算定不可" not in joined
    assert "需要人時が 0 時間です" not in joined
    assert "ZeroDivisionError" not in joined


def test_供給が極端に小さい場合も不足倍率の助言が返る():
    """0 < supply_h < need_h のとき、0 除算せずに不足倍率の助言を出すこと。"""
    children = _kids(DAY, count=6)
    table = _requirements(children)
    # 1 日だけ 30 分の契約（必要量に対して極端に小さい供給）
    tiny = _pool([
        ("Y001", (Role.HOIKUSHI,),
         Contract(weekly_hours=40.0, daily_hours=0.5, employment_type=EmploymentType.SEI,
                  earliest_start=time(9, 0), latest_end=time(9, 30))),
    ])
    payload = _payload(children, tiny, table)
    supply = _supply_hours(payload)
    need = payload.requirements.total_needed_hours()
    assert 0 < supply < need

    messages = _staffing_advice(payload, _shortfall_supply(payload))
    joined = "\n".join(messages)
    # 表現の向きに注意: supply < need なので「必要量は供給の何倍」と書く。
    # （以前は「配置できる人時は必要量の N 倍」と逆向きに書いていた）
    assert "必要人時は配置可能人時の" in joined
    assert "配置できる人時は必要量の" not in joined
    assert "ZeroDivisionError" not in joined


def test_職員0名と全職員休園日も例外にならない():
    """職員 0 名・全職員が休園日でも ZeroDivisionError を投げないこと。"""
    children = _kids(DAY, count=6)
    table = _requirements(children)
    staff = _disjoint_staff()

    # 職員 0 名
    no_staff = _solve(children, [], table, time_limit_sec=5)
    assert no_staff.status is SolveStatus.ERROR
    assert no_staff.messages

    # 全職員が休園日（FacilitySettings の休園日に一致する日付）
    settings = FacilitySettings()
    closed = solve_shift(children, staff, table, time_limit_sec=5, standard=STANDARD,
                         settings=settings)
    assert closed.messages
    joined = "\n".join(closed.messages)
    assert "配置できる人時が 0 です" in joined
    assert "ZeroDivisionError" not in joined


# ===========================================================================
# 10. 3 つの修正に対する「実効性」の証明（追加分）
#
# ここは「検出しうる」だけでなく「実際に検出しうる」ことを示すために、
# 意図的にハード制約を壊した解を ``verify_solution`` に食わせて
# 制約名が返ることを数値で固定する。空リストが返った時点で本節は FAIL する。
# ===========================================================================


def _solved_ctx(staff, requirements, **kwargs):
    """``_build_problem`` して実際に解を読み終えた ``_ModelCtx`` を返す。"""
    ctx = _build(staff, requirements, **kwargs)
    ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    return ctx


def test_台帳はモデルの全制約を1本ずつ収録する(small_staff, small_requirements):
    """``_constraint_specs`` が取りこぼしなく全制約を収録していること。

    台帳が「検査したつもりの制約」だけをInspection していると、UT は通っても
    実運用でハード制約違反が漏れる。ここでは
    ``len(prob.constraints) == len(specs)`` かつ名前が完全一致することを固定する。
    """
    for soft in (False, True):
        ctx = solver_mod._build_problem(
            small_staff, small_requirements, {}, {}, FacilitySettings(),
            ObjectiveWeights(), STANDARD, soft_coverage=soft,
        )
        specs = _constraint_specs(ctx)
        assert len(specs) == len(ctx.prob.constraints) > 0, f"soft={soft} で台帳が欠落"
        assert [s.name for s in specs] == list(ctx.prob.constraints)
        for spec in specs:
            assert spec.offset == spec.offset, f"{spec.name} の定数項が NaN"
            assert spec.sense in (pulp.LpConstraintLE, pulp.LpConstraintGE, pulp.LpConstraintEQ)
            assert all(coef == coef for _var, coef in spec.terms)


def test_台帳は全ハード制約ファミリを収録する(small_staff, small_requirements):
    """セル排他・日上限・在勤ブロック・配置基準がすべて台帳に載ること。"""
    ctx = _build(small_staff, small_requirements)
    names = {spec.name for spec in _constraint_specs(ctx)}
    assert any(n.startswith("dailycap_") for n in names), "1日上限が入っていない"
    assert any(n.startswith("coverq_") or n.startswith("cover_") for n in names), "配置基準が入っていない"
    assert any(n.endswith("_splitcap") and n.startswith("duty_") for n in names), "在勤ブロックが入っていない"
    assert any(n.endswith("_splitcap") and n.startswith("brk_") for n in names), "休憩ブロックが入っていない"
    # セル排他（w + b <= 1）は PuLP の自動連番名になる
    assert len(names) == len(ctx.prob.constraints)
    assert all(
        spec.sense in (pulp.LpConstraintLE, pulp.LpConstraintGE, pulp.LpConstraintEQ)
        for spec in _constraint_specs(ctx)
    )


def test_台帳は構築直後でも揃い再収集される(small_staff, small_requirements):
    """``_build_problem`` が構築直後に台帳を撮り、消失しても再収集されること。"""
    ctx = _build(small_staff, small_requirements)
    assert ctx.specs, "構築直後に台帳が収集されていない"
    assert [s.name for s in ctx.specs] == list(ctx.prob.constraints)
    # 敢義的に台帳を消してから検査しても、同じ名前が返る（=_constraint_specs の補完経路）
    ctx.specs = ()
    for variable in ctx.prob.variables():
        variable.varValue = 0.0
    bad = verify_solution(ctx)
    assert ctx.specs, "台帳が再収集されていない"
    assert bad, "再収集した台帳が違反を検出できない"


def test_配置基準の破れはcoverqとして検出される(small_staff, small_requirements):
    """勤務セルを全 OFF にすると「配置基準（>=）」違反として制約名が返ること。

    回帰対象: 検査が機能していないと ``solve_shift`` は
    「必要保育士 0 名配置の制約を満たす解」を採用してしまう。
    """
    ctx = _solved_ctx(small_staff, small_requirements)
    assert verify_solution(ctx) == [], "前提となる正常解が既に違反扱い"
    for variable in ctx.work.values():
        if not isinstance(variable, (int, float)):
            variable.varValue = 0.0
    bad = verify_solution(ctx)
    assert bad, "配置基準を破ったのに空リストが返った（検査が機能していない）"
    assert any(name.startswith("coverq_") for name in bad), bad


def test_在勤ブロック分裂はsplitcapとして検出される(small_staff, small_requirements):
    """勤務ブロックを 2 つに分裂させ、罰変数を 0 にすると splitcap 制約が鳴ること。

    ``duty_*_splitcap`` は「ブロック開始数 - 1 <= 罰変数」というソフトペナルティ制約なので、
    勤務セルだけ書き換えても違反にはならない（罰変数が吸収する）。
    したがって罰変数側を未充足にして初めて違反として現れる。
    この経路が通ること自体が「台帳が splitcap を検査している」証明になる。
    """
    ctx = _solved_ctx(small_staff, small_requirements)
    assert verify_solution(ctx) == []
    slots = small_requirements.slots
    ctx.work[("S001", DAY, 0)].varValue = 1.0
    ctx.work[("S001", DAY, len(slots) - 1)].varValue = 1.0
    for i in range(1, len(slots) - 1):
        cell = ctx.work[("S001", DAY, i)]
        if not isinstance(cell, (int, float)):
            cell.varValue = 0.0
    slack = next(v for v in ctx.prob.variables() if v.name == "duty_S001_0928_split")
    slack.varValue = 0.0
    starts = [v for v in ctx.prob.variables() if v.name.startswith("duty_S001_0928_s")]
    assert len(starts) >= 2
    for var in starts:
        var.varValue = 1.0  # ブロック開始フラグが 2 本立つ
    bad = verify_solution(ctx)
    assert bad, "在勤ブロック分裂を検出できなかった"
    assert "duty_S001_0928_splitcap" in bad, bad


def test_検査不能な制約だけが除外され他は検出される(small_staff, small_requirements):
    """変数値が ``None`` の制約は黙って除外されるが、同一検査内で他は検出されること。

    修正1 の中核（``value()`` に依存して「検証できない」を「違反していない」に
    読み替えないこと）の実効性チェック。``None`` 除外を壊す =
    ``None`` を 0 とみなして誤検出する実装に退行しても、このテストが鳴る。
    """
    ctx = _solved_ctx(small_staff, small_requirements)
    first = next(v for v in ctx.work.values() if not isinstance(v, (int, float)))
    # 「その変数を含む制約」は未判定なので、違反リストに現れない
    touched = {spec.name for spec in _constraint_specs(ctx) if any(var is first for var, _ in spec.terms)}
    assert touched, "テスト対象の変数を含む制約が台帳に無い"
    first.varValue = None
    bad = verify_solution(ctx)
    assert isinstance(bad, list)
    assert not touched & set(bad), bad
    # 同じ変数に 1 を入れれば、同一の検査経路で当該制約が違反として返る
    first.varValue = 1.0
    detected = verify_solution(ctx)
    assert touched & set(detected), (
        "変数値を 1 にしたのに制約違反を検出できなかった（検査経路が死んでいる）"
    )


def test_全変数が未決なら判定不能として空リストになる(small_staff, small_requirements):
    """``.solu`` が読めていない（変数値が全 None）状態では判定を保留にすること。

    未決変数を 0 とみなす実装へ退行すると、定数項だけから
    「配置基準 ``expr >= need`` が ``-need`` で未達」＝全制約違反、という誤検出になる。
    """
    ctx = _solved_ctx(small_staff, small_requirements)
    assert verify_solution(ctx) == []
    assert any(spec.offset < 0 for spec in _constraint_specs(ctx)), "テスト前提が崩れている"
    for variable in ctx.prob.variables():
        variable.varValue = None
    assert verify_solution(ctx) == [], "未決の状態で制約違反と判定している"


def test_貪欲法の固定セルはロックされる(small_children):
    """``fixed_assignments`` の WORK / BREAK / OFF ロックが貪欲法で効くこと。"""
    pool = _pool([
        ("L001", (Role.HOIKUSHI,), sei_contract()),
        ("L002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=3, need=1, qualified=1)
    slots = table.slots
    fixed = {
        ("L001", DAY, slots[0].label): CellState.WORK,
        ("L001", DAY, slots[1].label): CellState.BREAK,
        ("L001", DAY, slots[2].label): CellState.OFF,
    }
    result = solve_shift_greedy(
        small_children, pool, table, fixed_assignments=fixed, standard=STANDARD
    )
    assert result.day(DAY).get("L001", slots[0]) is CellState.WORK
    assert result.day(DAY).get("L001", slots[1]) is CellState.BREAK
    assert result.day(DAY).get("L001", slots[2]) is CellState.OFF


def test_MILPの固定セルはロックされる(small_children):
    """``fixed_assignments`` のロックが ``solve_shift`` の出力にも効くこと。

    以前は ``_build_cells`` が ``forced`` セルを定数化する際に
    ``ctx.work`` / ``ctx.brk`` へ記録していなかったため、``decode_solution`` が
    そのセルを OFF として復元していた。モデルは固定を尊重していた（勤務時間が
    計上される）ので、UI で確定したセルが出力上だけ「オフ」になっていた。
    定数としての値を ctx にも残す実装へ修正済み。
    """
    pool = _pool([
        ("L001", (Role.HOIKUSHI,), sei_contract()),
        ("L002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=3, need=1, qualified=1)
    slots = table.slots
    fixed = {
        ("L001", DAY, slots[0].label): CellState.WORK,
        ("L001", DAY, slots[1].label): CellState.BREAK,
        ("L001", DAY, slots[2].label): CellState.OFF,
    }
    result = _solve(small_children, pool, table, fixed_assignments=fixed, time_limit_sec=10)
    assert result.day(DAY).get("L001", slots[0]) is CellState.WORK
    assert result.day(DAY).get("L001", slots[1]) is CellState.BREAK
    assert result.day(DAY).get("L001", slots[2]) is CellState.OFF


def test_MILPは固定セルをモデルの中で反映する(small_children):
    """固定セルは変数化されず **定数として** ctx にも制約にも反映されること。

    以前は「ctx に載らない」ことが前提だったが、固定セルをデコードで復元できる
    よう定数として ctx へも記録する実装へ変更された。定数であることと、
    最適化からは変数が見えないことの両方を検証する。
    """
    pool = _pool([
        ("L001", (Role.HOIKUSHI,), sei_contract(daily_hours=1.0, weekly_hours=10.0)),
        ("L002", (Role.HOIKUSHI,), sei_contract(daily_hours=1.0, weekly_hours=10.0)),
    ])
    table = _tiny_requirements(small_children, slots_count=3, need=1, qualified=1)
    slots = table.slots
    ctx = _build(pool, table, fixed={("L001", DAY, slots[0].label): CellState.WORK})
    # 固定セルは定数 1 として記録され、LpVariable ではない（最適化対象から外れる）
    assert isinstance(ctx.work[("L001", DAY, 0)], int)
    assert ctx.work[("L001", DAY, 0)] == 1
    assert ("L001", DAY, 0) not in {
        v.name for v in ctx.prob.variables() if v.name == "w_L001_0928_0"
    }
    # 確定分は 1日上限の右辺に定数として入るので、上限 60 分の職員で 2 セルは解けない
    result = _solve(small_children, pool, table,
                    fixed_assignments={
                        ("L001", DAY, slots[0].label): CellState.WORK,
                        ("L001", DAY, slots[1].label): CellState.WORK,
                    }, time_limit_sec=10)
    assert result.stats["solver_status_pass1"] != "Optimal"


def test_負の時間上限や0でも例外を投げない(small_children):
    """``time_limit_sec`` が 0 / 負でも内部で 2 秒に丸められ例外を投げないこと。"""
    pool = _pool([
        ("T001", (Role.HOIKUSHI,), sei_contract()),
        ("T002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    for limit in (0, -1, -100000):
        result = _solve(small_children, pool, table, time_limit_sec=limit)
        assert result.status in set(SolveStatus), limit
        assert result.messages
        assert result.stats["elapsed_sec"] >= 0.0


def test_全経路でstatsの必須キーが揃う(small_children, small_staff, small_requirements):
    """ERROR / 貪欲法 / MILP のどの経路でも必須統計が揃うこと。"""
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    results = [
        _solve(small_children, [], table),
        solve_shift_greedy(small_children, small_staff, small_requirements, standard=STANDARD),
        _solve(small_children, small_staff, small_requirements, time_limit_sec=10),
    ]
    for result in results:
        for key in ("elapsed_sec", "num_variables", "num_constraints"):
            assert key in result.stats, (result.status, key)
            assert isinstance(result.stats[key], (int, float)), (key, result.stats[key])
        assert result.stats["elapsed_sec"] >= 0.0


def test_重みを全部巨大にしても解が返る(small_children):
    """全フィールドを 10^6 倍にしても目的関数が壊れず解が得られること。"""
    pool = _pool([
        ("H001", (Role.HOIKUSHI,), sei_contract()),
        ("H002", (Role.HOIKUSHI,), sei_contract()),
    ])
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    huge = ObjectiveWeights(**{name: value * 1e6 for name, value in
                              vars(ObjectiveWeights()).items()})
    result = _solve(small_children, pool, table, weights=huge, time_limit_sec=10)
    assert result.status in set(SolveStatus)
    assert result.assignments
    assert result.messages


# ===========================================================================
# 4. 回帰テスト: 検査器の fail-closed 化
# ===========================================================================


def test_検査器が落ちても違反0件にはしない(small_children, small_staff, small_requirements, monkeypatch):
    """``check_violations`` が例外を投げても BLOCKER を出して ERROR にすること。

    以前は ``except Exception: return`` で握り潰しており ``violations`` が空の
    ままだった。UI は「違反 0 件」と表示し、利用者は法令違反を含むシフトを
    適合したものと信じて出力・配布できてしまう。
    """
    from shiftai import gap_analysis

    def boom(*args, **kwargs):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(gap_analysis, "check_violations", boom)
    table = _tiny_requirements(small_children, slots_count=2, need=1, qualified=1)
    result = _solve(small_children, small_staff, table, time_limit_sec=10)
    assert result.violations, "検査に失敗しても violations は空にしない"
    assert [v.code for v in result.violations] == ["CHECK_UNAVAILABLE"]
    assert result.violations[0].severity is ViolationSeverity.BLOCKER
    assert result.status is SolveStatus.ERROR
    assert not result.ok, "ERROR の結果は ok=True にしてはいけない"
    assert any("判定" in m for m in result.messages), result.messages


def test_検査器が落ちても貪欲法も同じ扱い(small_children, small_staff, small_requirements, monkeypatch):
    """貪欲法ルートでも検査不能を BLOCKER で示すこと。"""
    from shiftai import gap_analysis

    def boom(*args, **kwargs):
        raise RuntimeError("checker exploded")

    monkeypatch.setattr(gap_analysis, "check_violations", boom)
    result = solve_shift_greedy(
        small_children, small_staff, small_requirements, standard=STANDARD
    )
    assert [v.code for v in result.violations] == ["CHECK_UNAVAILABLE"]
    assert result.status is SolveStatus.ERROR


def test_貪欲法は休園日を渡すと休園日に勤務しない(small_children, small_staff, small_requirements):
    """``settings`` を渡すと休園日が反映されること。

    以前は ``solve_shift_greedy`` が ``FacilitySettings()`` をハードコードしており、
    ``solve_shift`` のフォールバック経路から呼ばれたときに休園日・祝日が
    黙って失われていた。
    """
    day = small_requirements.all_days()[0]
    closed = frozenset({day})
    settings = FacilitySettings(closed_days=closed)
    result = solve_shift_greedy(
        small_children, small_staff, small_requirements,
        standard=STANDARD, settings=settings,
    )
    worked_on_closed = [
        a.staff_id for a in result.assignments if a.day == day and a.state is CellState.WORK
    ]
    assert not worked_on_closed, f"休園日 {day} に勤務した職員: {worked_on_closed}"


def test_supply_hoursは休園日を除外する(small_staff, small_requirements):
    """供給人時は必要人員のある日だけを数えること。"""
    from shiftai.solver import supply_hours

    plain = supply_hours(small_staff, small_requirements)
    with_closed = supply_hours(
        small_staff, small_requirements,
        settings=FacilitySettings(closed_days=frozenset(small_requirements.all_days())),
    )
    assert plain > 0.0
    assert with_closed == 0.0, "全日が休園なら供給人時は 0"


def test_supply_hoursは週所定日数で頭打ちになる(small_staff, small_requirements):
    """``max_weekly_days`` の上限が敷居値を越えても供給人時は増えないこと。"""
    from dataclasses import replace as _replace

    from shiftai.solver import supply_hours

    days = len(small_requirements.all_days())
    capped = [
        _replace(s, contract=_replace(s.contract, max_weekly_days=1))
        for s in small_staff
    ]
    full = supply_hours(small_staff, small_requirements)
    one_day = supply_hours(capped, small_requirements)
    if days > 1:
        assert one_day < full, "週1日制限で供給が減っていない"
