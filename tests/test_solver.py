"""シフト最適化（``shiftai.solver``）のテスト。

**小さい問題（1 日・6 名・10 スロット）で**、PuLP/CBC を動かして
「ハード制約が本当に守られているか」だけを確かめる。大きい問題は
``tests/test_e2e.py`` と本ファイルの ``slow`` テストに集約する。
"""

from __future__ import annotations

from datetime import date, time

import pytest

from shiftai import local_rules
from shiftai.domain import (
    CellState,
    ChildPlan,
    FacilitySettings,
    ObjectiveWeights,
    Role,
    SolveStatus,
    StaffMember,
    StaffPreferences,
    Unavailability,
)
from shiftai.solver import (
    available_solvers,
    month_fraction,
    solve_shift,
    solve_shift_greedy,
    staff_shift_count,
    staff_work_hours,
    verify_solution,
)
from shiftai.standards import build_requirements
from tests.conftest import DAY, DAY_CLOSE, DAY_OPEN, STANDARD_KEY, part_contract, sei_contract

pytestmark = [pytest.mark.timeout(300), pytest.mark.filterwarnings("ignore::DeprecationWarning")]

STANDARD = local_rules.get_standard(STANDARD_KEY)

#: ``gap_analysis.check_violations`` が返しうる ``Violation.code`` の一覧。
#: ``gap_analysis`` 側に ``VIOLATION_CODE_LABELS`` が無いため、テスト側で契約として持つ。
KNOWN_VIOLATION_CODES: frozenset[str] = frozenset(
    {
        "SHORTFALL_STAFF",
        "SHORTFALL_QUALIFIED",
        "DAILY_HOURS_EXCEEDED",
        "OVERTIME_NOT_ALLOWED",
        "OVERTIME_REQUIRES_36AGREEMENT",
        "WORK_ON_UNAVAILABLE",
        "WORK_ON_CLOSED_DAY",
        "OUTSIDE_CONTRACT_HOURS",
        "BREAK_INSUFFICIENT",
        "BREAK_FRAGMENTED",
        "SPLIT_SHIFT",
        "MONTHLY_HOURS_EXCEEDED",
        "MONTHLY_HOURS_SHORT",
        "WEEKLY_HOURS_EXCEEDED",
        "CONSECUTIVE_DAYS",
        "WEEKLY_DAYS_EXCEEDED",
        "REST_HOURS_SHORT",
        "AVOID_EARLY_CONFLICT",
        "AVOID_LATE_CONFLICT",
        "BREAK_OVERLAP",
        "HOURS_IMBALANCE",
        "NOT_PLACED_ROLE",
    }
)


def _solve(children, staff, requirements, **kwargs):
    """テスト用の既定オプションで ``solve_shift`` を呼ぶ。"""
    kwargs.setdefault("time_limit_sec", 20)
    kwargs.setdefault("standard", STANDARD)
    return solve_shift(children, staff, requirements, **kwargs)


def _pool(specs):
    return [StaffMember(sid, f"職員{sid}", roles, contract) for sid, roles, contract in specs]


BASE_POOL = [(f"S{i + 1:03d}", (Role.HOIKUSHI,), sei_contract()) for i in range(4)]
BASE_POOL += [(f"S{i + 5:03d}", (Role.SHIENSHIIN,), part_contract()) for i in range(2)]


def _requirements(children, days=(DAY,), **kwargs):
    return build_requirements(
        children,
        list(days),
        STANDARD,
        day_open=DAY_OPEN,
        day_close=DAY_CLOSE,
        granularity_min=30,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 環境・小さな 正常系
# ---------------------------------------------------------------------------


def test_available_solvers():
    """CBC が同梱されていること（``PULP_CBC_CMD`` が選べる）。"""
    assert "PULP_CBC_CMD" in available_solvers()


def test_充足可能ならOPTIMALかFEASIBLE(solved_day):
    """人員が足りていれば最適解か実行可能解が返ること。"""
    assert solved_day.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert solved_day.ok is True


def test_結果に統計が入っている(solved_day):
    """``stats`` に所要時間・変数数・制約数・求解パスが入っていること。"""
    stats = solved_day.stats
    for key in (
        "solver",
        "solver_status",
        "elapsed_sec",
        "num_variables",
        "num_constraints",
        "num_staff",
        "num_days",
        "num_slots",
        "pass",
    ):
        assert key in stats
    assert stats["elapsed_sec"] >= 0.0
    assert stats["num_staff"] == 6
    assert stats["num_days"] == 1
    assert stats["num_slots"] == 10
    assert stats["num_variables"] > 0
    assert stats["num_constraints"] > 0
    assert stats["solver"] == "PULP_CBC_CMD"


def test_結果は日ごとのシフト表を持つ(solved_day, small_staff, small_requirements):
    """全日・全職員・全時間帯のセルが定義されていること（欠損セルが無い）。"""
    assert len(solved_day.shift_days) == 1
    shift_day = solved_day.day(DAY)
    assert shift_day is not None
    for member in small_staff:
        for slot in small_requirements.slots:
            assert shift_day.get(member.staff_id, slot) in set(CellState)


def test_空の入力はERROR(small_requirements):
    """職員・対象日・時間帯が空なら ``ERROR`` になり、握り潰さないこと。"""
    result = solve_shift([], [], small_requirements)
    assert result.status is SolveStatus.ERROR
    assert result.ok is False
    assert result.messages


def test_メッセージが日本語で入っている(solved_day):
    """UI にそのまま出せる日本語メッセージが 1 条以上あること。"""
    assert solved_day.messages
    assert any(isinstance(m, str) and m for m in solved_day.messages)


# ---------------------------------------------------------------------------
# ハード制約
# ---------------------------------------------------------------------------


def test_希望休はハード制約(small_children, small_staff, small_requirements):
    """丸 1 日の希望休Asked職員は、その日の全セルが ``OFF`` になること。"""
    prefs = {
        "S002": StaffPreferences(
            unavailable=[
                Unavailability(day=DAY, start=time(0, 0), end=time(23, 59), reason="希望休")
            ]
        )
    }
    result = _solve(small_children, small_staff, small_requirements, preferences=prefs)
    shift_day = result.day(DAY)
    states = {shift_day.get("S002", s) for s in small_requirements.slots}
    assert states == {CellState.OFF}


def test_休園日は全職員OFF(small_children, small_staff):
    """``closed_days`` を含む日は全職員が ``OFF`` になること。"""
    closed = frozenset({DAY})
    requirements = _requirements(small_children, closed_days={DAY})
    assert requirements.rows[DAY] == []
    result = _solve(
        small_children,
        small_staff,
        requirements,
        settings=FacilitySettings(closed_days=closed),
    )
    shift_day = result.day(DAY)
    for member in small_staff:
        for slot in requirements.slots:
            assert shift_day.get(member.staff_id, slot) is CellState.OFF


def test_休園日と開所日を分ける(small_children, small_staff, week_days):
    """休園日以外の日には通常どおり勤務が入ること（休園日の扱いが過度に wide でない）。"""
    open_day = week_days[0]
    closed_day = week_days[1]
    kids = small_children + [
        ChildPlan(c.child_id, c.name, closed_day, c.age_class, c.arrive, c.depart)
        for c in small_children
    ]
    requirements = _requirements(kids, days=week_days[:2], closed_days={closed_day})
    result = _solve(
        kids,
        small_staff,
        requirements,
        settings=FacilitySettings(closed_days=frozenset({closed_day})),
    )
    closed_states = {
        result.day(closed_day).get(m.staff_id, s) for m in small_staff for s in requirements.slots
    }
    assert closed_states == {CellState.OFF}
    open_states = {
        result.day(open_day).get(m.staff_id, s) for m in small_staff for s in requirements.slots
    }
    assert CellState.WORK in open_states


def test_契約時間帯の外はOFF(small_children):
    """``earliest_start``/``latest_end`` を越えるセルには勤務が入らないこと。"""
    narrow = sei_contract(earliest_start=time(10, 0), latest_end=time(13, 0))
    pool = _pool([(f"N{i + 1:03d}", (Role.HOIKUSHI,), narrow) for i in range(6)])
    requirements = _requirements(small_children)
    result = _solve(small_children, pool, requirements)
    shift_day = result.day(DAY)
    for member in pool:
        for slot in requirements.slots:
            state = shift_day.get(member.staff_id, slot)
            if state is CellState.OFF:
                continue
            assert slot.start >= narrow.earliest_start
            assert slot.end <= narrow.latest_end


def _blocks(flags: list[bool]) -> int:
    """真の連続区間（ラン）の個数。"""
    count = 0
    previous = False
    for flag in flags:
        if flag and not previous:
            count += 1
        previous = flag
    return count


def test_在勤は連続ブロックになる(solved_day, small_requirements):
    """W-B-W は許されるが W-B-W-B-W（勤務の分裂）は作られないこと。"""
    shift_day = solved_day.day(DAY)
    for staff_id, row in shift_day.assignments.items():
        states = [row.get(s.label, CellState.OFF) for s in small_requirements.slots]
        duty = [s is not CellState.OFF for s in states]
        assert _blocks(duty) <= 1, staff_id


def test_休憩は1日1ブロック(solved_day, small_requirements):
    """在同一日、1 職員あたりの休憩は高々 1 連続ブロックであること。"""
    shift_day = solved_day.day(DAY)
    for staff_id, row in shift_day.assignments.items():
        states = [row.get(s.label, CellState.OFF) for s in small_requirements.slots]
        assert _blocks([s is CellState.BREAK for s in states]) <= 1, staff_id


def test_1日の勤務時間が契約内(solved_day, small_staff, small_requirements):
    """1 日の勤務時間が日契約時間を超えないこと。"""
    hours = staff_work_hours(solved_day, small_staff)
    for member in small_staff:
        assert hours[member.staff_id] <= member.contract.daily_hours + 1e-6


def test_verify_solutionは解けた制約違反0件(small_staff, small_requirements):
    """実際に解いた MILP モデルのハード制約違反が 0 件であること。"""
    import pulp

    from shiftai.solver import _build_problem

    ctx = _build_problem(
        small_staff, small_requirements, {}, {}, FacilitySettings(), ObjectiveWeights(), STANDARD
    )
    ctx.prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    assert verify_solution(ctx) == []


# ---------------------------------------------------------------------------
# verify_solution の責務分担（回帰防止）
#
# ``verify_solution`` は「数値的に確定したハード制約違反」だけを列挙し、
# 変数値が ``None`` の制約は**違反として数えない**。
# 「解が読み込まれたか」は ``_unassigned_variables`` が担当し、
# ``_run_cbc`` が ``verify_solution`` を呼ぶ前に ``INFEASIBLE`` を返す。
#
# この 2 段構えは意図的なもの。どちらかを崩すと:
#   * ``None`` を 0 とみなす → 配置基準 ``expr >= need`` が ``-need`` で未達となり
#     全制約が偽陽性になる（``test_全変数が未決なら判定不能として空リストになる``
#     が鳴る）
#   * ``None`` を違反とみなす → 判定不能な状態から 300 件超の偽の違反が出る
#     （``test_solver_perf.py::test_検査不能な制約だけが除外され他は検出される``
#     と衝突する）
#
# 修正前: ``verify_solution`` の docstring が「『検査できない』を『違反していない』に
# 読み替えると CBC の途中解が常に採用される」と書きながら、実際の保護は
# ``_run_cbc`` の ``_unassigned_variables`` 側にあった。docstring が
# 「この関数自身が守る」ように読めたので、以下で 2 段構えを固定する。
# ---------------------------------------------------------------------------


def test_判定不能な制約は違反に数えない() -> None:
    """変数値が ``None`` の制約を ``verify_solution`` は違反として列挙しないこと。"""
    import pulp

    from shiftai.solver import _ConstraintSpec

    unassigned = pulp.LpVariable("unassigned", 0, 1, pulp.LpBinary)
    spec = _ConstraintSpec(
        name="unresolvable",
        sense=pulp.LpConstraintGE,
        offset=0.0,
        terms=((unassigned, 1.0),),
    )
    assert spec.value() is None, "未決変数の制約で value() が None を返さない"
    assert spec.is_violated(1e-4) is False, (
        "判定不能な制約を『違反』と判定している（判定不能な状態から偽の違反が大量に並ぶ）"
    )


def test_判定不能は_run_cbcが先に弾く(small_staff, small_requirements) -> None:
    """``verify_solution`` が見逃す「解が読めていない」状態は ``_run_cbc`` が落とすこと。

    ``verify_solution`` 単体は判定を保留する（返り値は空）ため、
    守りが機能するのは ``_run_cbc`` の側。``.solu`` が読めていない状況を
    擬似的に作り（``prob.solve`` を無効化して全変数を ``None`` にし、
    勤務セルだけ 1.0 を入れる＝``.solu`` が途中値だけ書いた状態）、
    ``_run_cbc`` が ``INFEASIBLE`` を返すことを確認する。
    """
    from shiftai.solver import _build_problem, _run_cbc, _unassigned_variables

    ctx = _build_problem(
        small_staff, small_requirements, {}, {}, FacilitySettings(), ObjectiveWeights(), STANDARD
    )
    # 解けていない状態で verify_solution 自体は判定を保留する
    assert verify_solution(ctx) == [], "未解決の状態で違反を列挙している"
    assert _unassigned_variables(ctx) > 0, "未決変数が検出されていない"

    # …ただし _run_cbc はそれを捕捉して INFEASIBLE にする
    ctx.prob.solve = lambda *args, **kwargs: None  # type: ignore[method-assign]
    ctx.prob.status = 0
    ctx.prob.sol_status = 2
    for variable in ctx.prob.variables():
        variable.varValue = None
    for value in ctx.work.values():
        if not isinstance(value, int | float):
            value.varValue = 1.0

    status, raw = _run_cbc(ctx, 1, False)
    assert status is SolveStatus.INFEASIBLE, (
        f"解が読めていないのに {status.value} を返した（途中解が採用される）: {raw}"
    )
    assert "解未読込" in raw, f"INFEASIBLE の理由が『解未読込』ではない: {raw}"


# ---------------------------------------------------------------------------
# fixed_assignments（UI での手動確定）
# ---------------------------------------------------------------------------


def test_固定セル_offは確定される(small_children, small_staff, small_requirements):
    """``fixed_assignments`` に ``OFF`` を入れたセルは必ず ``OFF`` で戻ること。"""
    slot = small_requirements.slots[2]
    fixed = {("S001", DAY, slot.label): CellState.OFF}
    result = _solve(small_children, small_staff, small_requirements, fixed_assignments=fixed)
    assert result.day(DAY).get("S001", slot) is CellState.OFF


def test_固定セル_workはモデルでロックされる(small_children, small_staff, small_requirements):
    """``fixed_assignments`` に ``WORK`` を入れたセルは MILP 上を変数化せず固定されること。

    回帰メモ: 以前は確定セルを ``ctx.work`` に記録していなかったため、
    ``decode_solution`` が ``OFF`` として復元し、確定済みの ``WORK`` が
    出力上だけ消えていた。定数 1 として ctx にも記録する実装へ修正済み。
    """
    from shiftai.solver import _build_problem

    slot = small_requirements.slots[2]
    fixed = {("S001", DAY, slot.label): CellState.WORK}
    ctx = _build_problem(
        small_staff, small_requirements, {}, fixed, FacilitySettings(), ObjectiveWeights(), STANDARD
    )
    # 定数として記録され、最適化からは変数が見えない
    assert ctx.work[("S001", DAY, 2)] == 1
    assert ctx.brk[("S001", DAY, 2)] == 0
    variables = {v.name for v in ctx.prob.variables()}
    assert not any("w_S001_0928_2" == name for name in variables)
    work_minutes = sum(r.slot.minutes for r in small_requirements.for_day(DAY) if r.slot == slot)
    assert work_minutes > 0


# ---------------------------------------------------------------------------
# 人員不足 → PARTIAL
# ---------------------------------------------------------------------------


def test_職員が足りないとPARTIAL(small_children, small_requirements):
    """職員 1 名では配置基準を満たせないので ``PARTIAL`` にフォールバックすること。"""
    pool = _pool([("T001", (Role.HOIKUSHI,), sei_contract())])
    result = _solve(small_children, pool, small_requirements)
    assert result.status is SolveStatus.PARTIAL
    assert result.ok is True


def test_PARTIALは診断メッセージを返す(small_children, small_requirements):
    """人員不足時は「なぜ足りなかったか」の診断が ``messages`` に入ること。"""
    pool = _pool([("T001", (Role.HOIKUSHI,), sei_contract())])
    result = _solve(small_children, pool, small_requirements)
    assert result.messages
    assert any("不足" in m or "配置基準" in m for m in result.messages)
    assert any(m.startswith("  ") or "：" in m for m in result.messages)


def test_PARTIALでも不足がBLOCKERになる(small_children, small_requirements):
    """配置基準を満たせていない箇所は ``BLOCKER`` として列挙されること。"""
    pool = _pool([("T001", (Role.HOIKUSHI,), sei_contract())])
    result = _solve(small_children, pool, small_requirements)
    codes = {v.code for v in result.blockers()}
    assert "SHORTFALL_STAFF" in codes
    assert result.stats.get("relaxed") is True


def test_貪欲法も必ずPARTIAL(small_children, small_staff, small_requirements):
    """貪欲法（最終保険）は必ず ``PARTIAL`` を返し、瞬時に終わること。"""
    result = solve_shift_greedy(small_children, small_staff, small_requirements, standard=STANDARD)
    assert result.status is SolveStatus.PARTIAL
    assert result.ok is True
    assert result.assignments


def test_貪欲法も希望休と契約を守る(small_children, small_requirements):
    """貪欲法でも希望休と契約時間帯を守る（最終保険としての品質）。"""
    narrow = sei_contract(earliest_start=time(10, 0), latest_end=time(13, 0))
    pool = _pool([(f"N{i + 1:03d}", (Role.HOIKUSHI,), narrow) for i in range(3)])
    prefs = {
        "N002": StaffPreferences(
            unavailable=[
                Unavailability(day=DAY, start=time(0, 0), end=time(23, 59), reason="希望休")
            ]
        )
    }
    result = solve_shift_greedy(small_children, pool, small_requirements, prefs, standard=STANDARD)
    shift_day = result.day(DAY)
    assert all(shift_day.get("N002", s) is CellState.OFF for s in small_requirements.slots)
    for member in pool:
        for slot in small_requirements.slots:
            if shift_day.get(member.staff_id, slot) is not CellState.OFF:
                assert slot.start >= narrow.earliest_start
                assert slot.end <= narrow.latest_end


# ---------------------------------------------------------------------------
# 集計ユーティリティ
# ---------------------------------------------------------------------------


def test_staff_work_hoursは全職員を含む(solved_day, small_staff):
    """戻り値のキーが職員 1 人も欠けていないこと。"""
    hours = staff_work_hours(solved_day, small_staff)
    assert set(hours) == {m.staff_id for m in small_staff}
    assert all(isinstance(v, float) for v in hours.values())
    assert all(v >= 0 for v in hours.values())


def test_staff_work_hoursは休憩を含まない(solved_day, small_staff, small_requirements):
    """勤務分数だけを加算していること（休憩が除外されている）。"""
    hours = staff_work_hours(solved_day, small_staff)
    for assignment in solved_day.assignments:
        if assignment.state is not CellState.WORK:
            continue
        assert hours[assignment.staff_id] >= assignment.slot.hours - 1e-9
    breaks = sum(1 for a in solved_day.assignments if a.state is CellState.BREAK)
    assert breaks * 0.5 <= sum(hours.values())


def test_staff_shift_countは全職員を含む(solved_day, small_staff):
    """勤務日数のキーが職員 1 人も欠けていないこと。"""
    counts = staff_shift_count(solved_day)
    assert set(counts) == {m.staff_id for m in small_staff}
    assert all(isinstance(v, int) for v in counts.values())
    assert all(0 <= v <= 1 for v in counts.values())


def test_staff_shift_countは0人の職員も0件(small_children, small_requirements):
    """誰も選ばれなかった職員は 0 件になること。"""
    pool = _pool(BASE_POOL)
    result = solve_shift_greedy(small_children, pool, small_requirements, standard=STANDARD)
    counts = staff_shift_count(result)
    assert set(counts) == {m.staff_id for m in pool}
    assert 0 in counts.values()


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------


def test_month_fraction():
    """7 日なら約 0.23 か月、31 日なら約 1 か月になること。"""
    from datetime import timedelta

    week = [date(2026, 9, 28) + timedelta(days=i) for i in range(7)]
    assert month_fraction(week) == pytest.approx(7 / 30.0, abs=0.02)
    month = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]
    assert month_fraction(month) == pytest.approx(1.0, abs=0.05)


def test_objective_weightsは正の既定値を持つ():
    """目的関数の重みが全て正であること（0 だと項が消える）。"""
    weights = ObjectiveWeights()
    for name, value in vars(weights).items():
        assert value > 0, name


def test_時間制限0でも動く(small_children, small_staff, small_requirements):
    """``time_limit_sec`` が 0 でも 2 秒に切り上げて動作すること。"""
    result = _solve(small_children, small_staff, small_requirements, time_limit_sec=0)
    # 上限 0 秒は内部で 2 秒に丸められ、求解可能な結果になること
    assert result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE, SolveStatus.PARTIAL)
    assert result.stats["elapsed_sec"] >= 0.0


# ---------------------------------------------------------------------------
# 統合（1 週間サンプル・slow）
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_配置基準を満たす1週間シフト(solved_week, week_inputs):
    """1 週間サンプルで配置基準の不足 0 件・充足率 1.0・BLOCKER は BREAK_INSUFFICIENT のみであること。"""
    children, staff, prefs, table = week_inputs
    from shiftai.gap_analysis import analyze_gap

    assert len(staff) == 28
    assert solved_week.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    report = analyze_gap(table, solved_week, staff, standard=local_rules.get_standard("福岡市"))
    assert report.total_shortfall_slots == 0
    assert report.coverage_ratio == pytest.approx(1.0, abs=0.01)
    # 案4: BREAK_INSUFFICIENT is now BLOCKER when statutory minutes unmet
    blockers = solved_week.blockers()
    break_insufficient_blockers = [v for v in blockers if v.code == "BREAK_INSUFFICIENT"]
    assert len(break_insufficient_blockers) >= 1, "BREAK_INSUFFICIENT should be BLOCKER when statutory minutes unmet"
    # Other BLOCKERs should not exist
    other_blockers = [v for v in blockers if v.code != "BREAK_INSUFFICIENT"]
    assert len(other_blockers) == 0, f"Unexpected BLOCKERs: {other_blockers}"
    assert report.total_required_hours == pytest.approx(521.0)
    assert solved_week.stats["num_slots"] == 25
    assert solved_week.stats["num_days"] == 7


@pytest.mark.slow
def test_1週間シフトは希望休と契約を守る(solved_week, week_inputs):
    """1 週間サンプルでも希望休職員は勤務せず、契約時間帯外にも出勤しないこと。"""
    children, staff, prefs, table = week_inputs
    for member in staff:
        entry = prefs.get(member.staff_id)
        off_days = entry.unavailable_days() if entry else set()
        for shift_day in solved_week.shift_days:
            if shift_day.day in off_days:
                states = {shift_day.get(member.staff_id, slot) for slot in table.slots}
                assert states == {CellState.OFF}, (member.staff_id, shift_day.day)
            for slot in table.slots:
                if shift_day.get(member.staff_id, slot) is not CellState.OFF:
                    assert slot.start >= member.contract.earliest_start
                    assert slot.end <= member.contract.latest_end
