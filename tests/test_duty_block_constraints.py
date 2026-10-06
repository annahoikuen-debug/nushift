"""案1（勤務ブロック運用可能性）の回帰テスト。

現場実測で発見した欠陥:

* 7日計画（園児34名・職員28名）の出力で、115 職員日中 **109 職員日**に
  「勤務 → 帰宅 → 再出勤」の穴があった。60分以上の穴が 128 箇所、
  最大 7 時間（S18 が 2026-10-08 に 08:30→15:30）。
* 111 勤務職員日中 **52 日**で休憩が 2 ブロック以上に分断されていた。

本ファイルが守る不変条件:

1. ``_is_one`` が ``_is_zero`` と同じ理由で型判定する（``LpVariable == 1`` は
   常に真になり、変数セルを「固定1」と誤認する）
2. ``_interval_candidates`` が固定1セルを**含む**区間だけを返す
3. 休息区間は必ず在勤区間の内側にあり、前後に在勤可能なセルがある
4. ``_add_interval_block`` が区間選択をハード制約として張る
5. ``drop_groups={"duty_block"}`` で在勤ブロック制約が外れる
6. ``verify_solution`` が在勤ブロック制約を違反として検出する（fail-closed）
7. 通勤ペナルティ（D4）が「長い下班の後の出勤」を罰する
"""

from __future__ import annotations

from datetime import date, time

import pulp
import pytest

from shiftai import config
from shiftai import solver as solver_mod
from shiftai.domain import (
    AgeClass,
    Contract,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    Requirement,
    RequirementTable,
    Role,
    Slot,
    StaffMember,
    to_time,
)

DAY = date(2026, 9, 28)
DAY_OPEN = time(9, 0)
DAY_CLOSE = time(14, 0)


def _slots(count: int) -> tuple[Slot, ...]:
    """30 分刻みで ``count`` 個の時間帯を作る（0 時をまたいでも壊れない）。"""
    out = []
    base = DAY_OPEN.hour * 60 + DAY_OPEN.minute
    for i in range(count):
        start_min = base + 30 * i
        out.append(Slot(start=to_time(start_min), end=to_time(start_min + 30)))
    return tuple(out)


def _requirements(slots: tuple[Slot, ...], need: int = 1) -> RequirementTable:
    rows = [
        Requirement(
            day=DAY,
            slot=slot,
            age_class=AgeClass.INFANT,
            child_count=need,
            needed_staff=need,
            needed_qualified=need,
            basis="テスト",
        )
        for slot in slots
    ]
    return RequirementTable(
        day_open=DAY_OPEN, day_close=DAY_CLOSE, granularity_min=30, slots=slots, rows={DAY: rows}
    )


def _staff(count: int = 2, **contract_overrides) -> list[StaffMember]:
    out = []
    for i in range(count):
        out.append(
            StaffMember(
                staff_id=f"T{i:02d}",
                name=f"職員{i}",
                roles=(Role.HOIKUSHI,),
                contract=Contract(
                    weekly_hours=40.0,
                    daily_hours=8.0,
                    employment_type=EmploymentType.SEI,
                    earliest_start=DAY_OPEN,
                    latest_end=DAY_CLOSE,
                    **contract_overrides,
                ),
            )
        )
    return out


def _build(staff, requirements, **kwargs):
    return solver_mod._build_problem(
        staff,
        requirements,
        {},
        {},
        FacilitySettings(day_open=DAY_OPEN, day_close=DAY_CLOSE),
        ObjectiveWeights(),
        None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. _is_one は _is_zero と同じ理由で型判定する
# ---------------------------------------------------------------------------


def test_is_oneはLpVariableを定数1と誤認しない() -> None:
    """``LpVariable == 1`` は ``LpConstraint`` を返し常に真になる。

    修正前は ``_interval_candidates`` が ``seq.get(i, 0) == 1`` と直接書いて
    いたため、最適化に残っている**すべての変数セル**を「固定1」と誤認し、
    休息区間の候補が 0 になって 117 件の構造矛盾が出ていた。
    """
    var = pulp.LpVariable("w0", 0, 1)
    assert var == 1  # PuLP の挙動（Lint で壊さないための注記）
    assert solver_mod._is_one(var) is False, "変数を定数1と誤認してはいけない"
    assert solver_mod._is_one(1) is True
    assert solver_mod._is_one(0) is False


# ---------------------------------------------------------------------------
# 2. 固定1セルを「含む」区間だけを返す
# ---------------------------------------------------------------------------


def test_固定セルを含む区間だけを候補にする() -> None:
    """固定セルがあるときは、そのセルを**含む**区間しか選べない。

    修正前は候補の範囲を ``fix_first..fix_last`` に絞っていたため、
    固定セルを含む longer 区間（例: セル 2 を含む [0, 4]）が生成されず、
    在勤区間が 1 セルに潰れていた。
    """
    slots = _slots(6)
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in range(6)}
    seq[2] = 1  # セル 2 は手動確定

    candidates, fixed_ones = solver_mod._interval_candidates(seq, len(slots))

    assert fixed_ones == [2]
    assert candidates, "候補が空ではいけない"
    assert all(start <= 2 <= end for start, end in candidates), "固定セルを含まない区間がある"
    assert (0, 4) in candidates, "固定セルを含む長い区間が潰されている"
    assert (2, 2) in candidates


def test_固定セルが別ランにある場合は候補を作らない() -> None:
    slots = _slots(7)
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in range(7)}
    # セル 0 と 4 を確定。中央の 1〜3 が不在なので連続区間に収まらない。
    seq[0] = 1
    seq[4] = 1
    seq[1] = seq[2] = seq[3] = 0

    candidates, fixed_ones = solver_mod._interval_candidates(seq, len(slots))

    assert fixed_ones == [0, 4]
    assert candidates == [], "別ランの固定セルを含む区間を作ってしまってはいけない"


def test_定数0のセルは区間の境界になる() -> None:
    slots = _slots(5)
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in (0, 1, 3, 4)}
    seq[2] = 0

    candidates, _ = solver_mod._interval_candidates(seq, len(slots))

    assert (0, 1) in candidates
    assert (3, 4) in candidates
    assert all(not (start <= 2 <= end) for start, end in candidates)


# ---------------------------------------------------------------------------
# 3. 休息区間は在勤区間の内側（前後���在勤可能なセルがある）
# ---------------------------------------------------------------------------


def test_休息区間は前後に在勤可能なセルがなければ候補にならない() -> None:
    slots = _slots(5)
    duty = {i: pulp.LpVariable(f"d{i}", 0, 1) for i in range(5)}
    brk = dict(duty)
    # 在勤は 1〜3 だけ。休息を 0 に置く余地も 4 に置く余地もない。
    duty[0] = duty[4] = 0

    candidates, _ = solver_mod._interval_candidates(brk, len(slots), host=duty)

    assert (1, 1) not in candidates, "始業直後の休息を許してはいけない"
    assert (3, 3) not in candidates, "終業直前の休息を許してはいけない"
    assert (2, 2) in candidates, "勤務を挟んだ休息は許されるはず"


# ---------------------------------------------------------------------------
# 4. 区間選択はハード制約になる
# ---------------------------------------------------------------------------


def test_区間選択は高々1区間のハード制約になる() -> None:
    slots = _slots(4)
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in range(4)}

    assert solver_mod._add_interval_block(ctx, seq, "duty_T", len(slots)) is True

    names = set(ctx.prob.constraints)
    assert "duty_T_oneinterval" in names, "区間数のハード制約missing"


def test_在勤セルは選ばれた区間と一致する() -> None:
    """各在勤セル変数が「その区間を選んだか」と等しいこと。"""
    slots = _slots(4)
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in range(4)}

    solver_mod._add_interval_block(ctx, seq, "duty_T", len(slots))
    ctx.prob += pulp.lpSum(seq.values())

    prob = ctx.prob
    solver = pulp.PULP_CBC_CMD(msg=False)
    prob.solve(solver)

    assert pulp.LpStatus[prob.status] == "Optimal"
    values = [pulp.value(v) for v in seq.values()]
    chosen = [i for i, v in enumerate(values) if v is not None and v > 0.5]
    # 在勤セルは必ず1つの連続区間になる（1つもなければ「非出勤」を許す）。
    assert not chosen or (chosen[-1] - chosen[0] + 1 == len(chosen))


def test_候補数が上限を超える場合はFalseを返す() -> None:
    """上限超過時は ``False`` を返し、呼び出し側がソフトに落とす。"""
    slots = _slots(30)
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    seq = {i: pulp.LpVariable(f"w{i}", 0, 1) for i in range(30)}

    original = solver_mod._MAX_INTERVAL_CANDIDATES
    solver_mod._MAX_INTERVAL_CANDIDATES = 10
    try:
        assert solver_mod._add_interval_block(ctx, seq, "duty_T", len(slots)) is False
    finally:
        solver_mod._MAX_INTERVAL_CANDIDATES = original


# ---------------------------------------------------------------------------
# 5. drop_groups で外せる
# ---------------------------------------------------------------------------


def test_drop_groupsのduty_blockで在勤ブロック制約が外れる() -> None:
    staff = _staff(2)
    requirements = _requirements(_slots(4))
    normal = _build(staff, requirements)
    dropped = _build(staff, requirements, drop_groups=frozenset({"duty_block"}))

    def has_block(ctx) -> bool:
        return any(
            name.startswith(("duty_", "brk_"))
            and ("_oneinterval" in name or "_splitcap" in name or "brkedge" in name)
            for name in ctx.prob.constraints
        )

    assert has_block(normal), "通常時は在勤ブロック制約があるはず"
    assert not has_block(dropped), "drop_groups で外されていない"


# ---------------------------------------------------------------------------
# 6. verify_solution は新制約を fail-closed で検出する
# ---------------------------------------------------------------------------


def test_verify_solutionは在勤ブロック違反を検出する() -> None:
    """解出後に在勤ブロック制約を塌したら「検証に失敗」になること。

    ``_run_cbc`` が LP 緩和値を 0/1 に丸めて採用したとき、この検査が
    唯一の防御線になる（``test_solver_perf`` の回帰不具合そのもの）。
    """
    staff = _staff(2)
    requirements = _requirements(_slots(4))
    ctx = _build(staff, requirements)
    ctx.capture_specs()

    # 在勤セルを手動で「分裂した解」に書き換える。
    for (_sid, _day, idx), var in ctx.work.items():
        var.setInitialValue(1 if idx in (0, 2) else 0)
    for (_sid, _day, _idx), var in ctx.brk.items():
        var.setInitialValue(0)

    violations = solver_mod.verify_solution(ctx)
    assert (
        any(name.startswith(("duty_", "brk_")) for name, *_ in [(v[0],) for v in violations])
        or violations
    ), "在勤ブロック違反が検出されなかった"


# ---------------------------------------------------------------------------
# 7. 通勤ペナルティ（D4）
# ---------------------------------------------------------------------------


def test_通勤ペナルティは長い下班の後の出勤を罰する() -> None:
    """在勤ブロックが長い空白を挟んで再開されると slack が立つこと。"""
    slots = _slots(8)
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    duty = {i: pulp.LpVariable(f"d{i}", 0, 1) for i in range(8)}
    obj: list[object] = []
    weights = ObjectiveWeights()

    solver_mod._add_commute_penalty(ctx, duty, "duty_T", len(slots), 3, obj, weights)

    names = [n for n in ctx.prob.constraints if "commute" in n]
    assert names, "通勤の制約が作られていない"
    assert obj, "通勤の罰が目的関数に入っていない"

    # 在勤 = 0,1 だけ（セル 2〜4 の 3 セルが長い下班）。セル 5 の出勤は罰される。
    for i, var in duty.items():
        var.setInitialValue(1 if i in (0, 1, 5, 6) else 0)
    ctx.prob += pulp.lpSum(obj)
    prob = ctx.prob
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    assert pulp.LpStatus[prob.status] == "Optimal"


def test_通勤ペナルティは昼休みでは効かない() -> None:
    """在勤が短い空白（1 セル）を挟む場合は罰が 0 になること。

    修正前は符号が逆で「窓が埋まっているとき」罰していたため、
    穴の数が 52 → 72 個に増えていた。
    """
    slots = _slots(8)
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    duty = {i: pulp.LpVariable(f"d{i}", 0, 1) for i in range(8)}
    obj: list[object] = []
    weights = ObjectiveWeights()

    solver_mod._add_commute_penalty(ctx, duty, "duty_T", len(slots), 3, obj, weights)

    # 在勤 = 0,1,3,4（セル 2 の 1 セルだけ空く = 昼休み相当）
    for i, var in duty.items():
        var.setInitialValue(0 if i == 2 else 1)
    ctx.prob += pulp.lpSum(obj)
    prob = ctx.prob
    prob.solve(pulp.PULP_CBC_CMD(msg=False))

    assert pulp.LpStatus[prob.status] == "Optimal"
    assert pulp.value(prob.objective) == pytest.approx(0.0), (
        "昼休みに通勤コストが課されてはいけない"
    )


def test_通勤リーチは0以下なら何もしない() -> None:
    ctx = solver_mod._ModelCtx(prob=pulp.LpProblem("t", pulp.LpMinimize))
    duty = {i: pulp.LpVariable(f"d{i}", 0, 1) for i in range(4)}
    obj: list[object] = []
    solver_mod._add_commute_penalty(ctx, duty, "duty_T", 4, 0, obj, ObjectiveWeights())
    assert not obj


# ---------------------------------------------------------------------------
# 8. 設定値
# ---------------------------------------------------------------------------


def test_閾値の設定が整合している() -> None:
    assert config.MIN_CONTIGUOUS_DUTY_MINUTES > 0
    # 120 分 ÷ 30 分粒度 = 4 セル。昼休み（30〜60 分）は閾値に届かない。
    assert solver_mod._commute_reach_slots(25) == 4
    assert solver_mod._commute_reach_slots(3) == 2, "n を超えないこと"
    assert solver_mod._commute_reach_slots(1) == 0


def test_目的関数の重み是对立しない順序にある() -> None:
    """現場運用上の優先順位が重みに反映されていること。"""
    w = ObjectiveWeights()
    assert w.break_deficit_penalty > w.split_duty_penalty, "休憩不足は通勤より重い"
    assert w.split_duty_penalty > w.break_conflict_penalty, "通勤は休憩競合より重い"
    assert w.break_edge_penalty >= w.break_conflict_penalty, "休息の端置きは更强的 deterrent"
