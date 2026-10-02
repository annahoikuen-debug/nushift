"""資格要件（保育士比率・看護師のみなし）的実装の検証。

**なぜこのテストが要るか**

監査の時点で、本ツールは ``StaffingStandard.min_qualified_ratio`` を
**一切計算に使っていなかった**（定義と表示のみ）。実測で 0.0 / 0.5 / 1.0 を
切り替えても必要保育士数が変化しなかった。

また ``StaffMember.is_qualified`` が保育士しか見ないため、
企業主導型保育事業（実施要綱 第3の2(4)②）が認める
**「保健師・看護師・准看護師は1人に限り保育士とみなせる」**を
まったく表現できていなかった。看護師は「0 名」扱いで、
必要保育士数を満たせないと誤判定していた。

さらに調理員は ``is_placeable=True`` のままで、保育室の必要人員を
埋めていた（調理員は保育従事者と別枠の必置職員）。

ここでは (1) 比率が実際に効くこと、(2) 看護師が上限まで数えられること、
(3) 調理員が配置人数に混ざらないこと、の3点を固定する。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, time

import pytest

from shiftai import gap_analysis, live_validation, local_rules, solver, standards
from shiftai.domain import (
    AgeClass,
    CellState,
    ChildPlan,
    Contract,
    Role,
    ShiftAssignment,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    qualified_count,
    qualified_ids,
)
from shiftai.domain import FacilitySettings as _FacilitySettings
from shiftai.domain import ObjectiveWeights as _ObjectiveWeights

DAY = date(2026, 10, 1)
OPEN = time(7, 15)
CLOSE = time(19, 30)

FACILITY_KEY = "企業主導型保育事業（単独枠）"
LICENSED_KEY = "全国基準（厚労省）"


def member(sid: str, *roles: Role, weekly: float = 40.0, daily: float = 8.0) -> StaffMember:
    return StaffMember(
        staff_id=sid,
        name=sid,
        roles=tuple(roles),
        contract=Contract(weekly_hours=weekly, daily_hours=daily),
    )


@pytest.fixture
def facility() -> StaffingStandard:
    return local_rules.get_standard(FACILITY_KEY)


@pytest.fixture
def licensed() -> StaffingStandard:
    return local_rules.get_standard(LICENSED_KEY)


# ---------------------------------------------------------------------------
# 1. 保育士比率が実際に効くこと（監査の指摘: 死んでいたフィールド）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.0, 0),
        (0.5, 2),
        (0.75, 3),
        (1.0, 3),
    ],
)
def test_保育士比率が必要保育士数を動かす(
    ratio: float, expected: int, licensed: StaffingStandard
) -> None:
    """ratio を変えると同じ必要人員でも必要保育士数が変わる。

    修正前は ``min_qualified_ratio`` が未使用で、常に「必要人員＝必要保育士数」だった。
    """
    standard = replace(licensed, qualified_mode="ratio", min_qualified_ratio=ratio)
    kids = [
        ChildPlan(f"c{i}", f"c{i}", DAY, AgeClass.INFANT, time(9, 0), time(12, 0)) for i in range(7)
    ]
    table = standards.build_requirements(kids, [DAY], standard, day_open=OPEN, day_close=CLOSE)
    rows = [r for r in table.for_day(DAY) if r.slot.label == "10:00-10:30"]
    need_all = sum(r.needed_staff for r in rows)
    need_q = sum(r.needed_qualified for r in rows)
    assert need_all == 3
    assert need_q == expected


def test_比率は必要人員を超えない(facility: StaffingStandard) -> None:
    for needed in range(0, 40):
        got = facility.slot_qualified_for(needed)
        assert 0 <= got <= needed
        assert got >= min(facility.min_qualified_floor, needed)


def test_保育事業者型はより多くの保育士を求める() -> None:
    single = local_rules.get_standard("企業主導型保育事業（単独枠）")
    shared = local_rules.get_standard("企業主導型保育事業（保育事業者型・20名以上）")
    for needed in range(1, 30):
        assert shared.slot_qualified_for(needed) >= single.slot_qualified_for(needed)


# ---------------------------------------------------------------------------
# 2. 看護師のみなし保育士（1人に限り）
# ---------------------------------------------------------------------------


def test_看護師は認可外では保育士として数えられる(
    facility: StaffingStandard, licensed: StaffingStandard
) -> None:
    nurse = member("N1", Role.KANGSHI)
    assert nurse.is_qualified is False, "is_qualified は保育士のみという前提は変わらない"
    assert nurse.is_qualified_under(facility) is True
    assert nurse.is_qualified_under(licensed) is False


def test_看護師は上限までしか数えられない(facility: StaffingStandard) -> None:
    pool = [member("N1", Role.KANGSHI), member("N2", Role.KANGSHI)]
    assert qualified_count(pool, facility) == 1
    pool.append(member("H1", Role.HOIKUSHI))
    assert qualified_count(pool, facility) == 2


def test_看護師の上限は認可保育所では無効() -> None:
    pool = [member("N1", Role.KANGSHI), member("N2", Role.KANGSHI)]
    assert qualified_count(pool, local_rules.get_standard(LICENSED_KEY)) == 0
    assert qualified_count(pool, None) == 0


def test_看護師の上限が0なら数えられない(facility: StaffingStandard) -> None:
    no_cap = replace(facility, nurse_as_qualified_cap=0)
    pool = [member("N1", Role.KANGSHI)]
    assert qualified_count(pool, no_cap) == 0


def test_看護師の採用は決定的である() -> None:
    """誰を数えるかが職員IDの昇順で決まること。"""
    standard = local_rules.get_standard(FACILITY_KEY)
    pool = [member("N2", Role.KANGSHI), member("N1", Role.KANGSHI)]
    assert qualified_ids(pool, standard) == {"N1"}
    assert qualified_ids(pool, None) == set()


def test_准看護師も看護師として数えられる(facility: StaffingStandard) -> None:
    assert qualified_count([member("N1", Role.KANGSHI)], facility) == 1


# ---------------------------------------------------------------------------
# 3. 調理員などは保育基準の配置人数に混ざらない
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.CHUUBOU, Role.EIYOU, Role.YAKUARIN])
def test_調理員らは配置対象から外れる(role: Role) -> None:
    assert member("K1", role).is_placeable is False
    assert member("K1", role).is_qualified_under(None) is False


def test_保育士と子育て支援員は配置対象である() -> None:
    assert member("H1", Role.HOIKUSHI).is_placeable is True
    assert member("S1", Role.SHIENSHIIN).is_placeable is True
    assert member("K1", Role.KANGSHI).is_placeable is True


def test_調理員が副資格に保育士があれば配置対象になる() -> None:
    """調理員と保育士の併記は「保育室で補助できる」とみなす。"""
    both = member("K1", Role.CHUUBOU, Role.HOIKUSHI)
    assert both.is_placeable is True


def test_園長は保育士が副資格なら配置対象() -> None:
    assert member("E1", Role.ENJOGAKUIN).is_placeable is False
    assert member("E1", Role.ENJOGAKUIN, Role.HOIKUSHI).is_placeable is True


# ---------------------------------------------------------------------------
# 4. 実際の判定経路（ソルバ・ギャップ分析・ライブ検証）で看護師が効くこと
# ---------------------------------------------------------------------------


def _requirements(standard: StaffingStandard, infants: int, day: date):
    kids = [
        ChildPlan(f"c{i}", f"c{i}", day, AgeClass.INFANT, time(9, 0), time(12, 0))
        for i in range(infants)
    ]
    return standards.build_requirements(kids, [day], standard, day_open=OPEN, day_close=CLOSE)


def _grid(requirements, staff: list[StaffMember], slot) -> dict[str, list[CellState]]:
    """対象時間帯だけ勤務しているような表を作る（他時間帯は休む）。"""
    out: dict[str, list[CellState]] = {}
    for member in staff:
        row = [CellState.OFF] * len(requirements.slots)
        row[requirements.slots.index(slot)] = CellState.WORK
        out[member.staff_id] = row
    return out


def _fake_result(staff: list[StaffMember], slot):
    """``gap_analysis.analyze_gap`` が読むだけの最小限の SolveResult を作る。"""
    return SolveResult(
        status=SolveStatus.OPTIMAL,
        shift_days=[],
        assignments=[
            ShiftAssignment(staff_id=m.staff_id, day=DAY, slot=slot, state=CellState.WORK)
            for m in staff
        ],
        violations=[],
        messages=(),
        stats={},
    )


def test_shortfall_rowsが看護師を供給に数える(facility: StaffingStandard) -> None:
    """供給判定で看護師が数えられること（数えられないと不足と誤判定する）。"""
    requirements = _requirements(facility, 3, DAY)
    slot = next(r.slot for r in requirements.for_day(DAY))
    assert requirements.needed_staff(DAY, slot) > 0

    support = [member("S1", Role.SHIENSHIIN), member("S2", Role.SHIENSHIIN)]
    with_nurse = support + [member("N1", Role.KANGSHI)]

    without = solver.shortfall_rows(support, requirements, standard=facility)
    assert any(g.gap_qualified > 0 for g in without), "支援員だけでは不足になるはず"

    withn = solver.shortfall_rows(with_nurse, requirements, standard=facility)
    assert not withn, "看護師1名を追加すれば不足は解消するはず"


def test_shortfall_rowsは認可保育所では看護師を数えない(licensed: StaffingStandard) -> None:
    requirements = _requirements(licensed, 3, DAY)
    pool = [member("N1", Role.KANGSHI), member("N2", Role.KANGSHI)]
    gaps = solver.shortfall_rows(pool, requirements, standard=licensed)
    assert gaps, "看護師だけでは必要人数を満たせないはず"
    assert all(g.supply_qualified == 0 for g in gaps)
    assert all(g.gap_qualified > 0 for g in gaps)


def test_gap_analysisが看護師を保育士として数える(facility: StaffingStandard) -> None:
    """``analyze_gap`` の「配置保育士数」が看護師を含むこと。"""
    requirements = _requirements(facility, 3, DAY)
    slot = next(r.slot for r in requirements.for_day(DAY))
    assert requirements.needed_qualified(DAY, slot) == 1

    staff = [member("N1", Role.KANGSHI), member("S1", Role.SHIENSHIIN)]
    result = _fake_result(staff, slot)
    report = gap_analysis.analyze_gap(requirements, result, staff, standard=facility)
    target = [g for g in report.gaps if g.slot == slot]
    assert target, "対象時間帯の判定が出ていない"
    assert target[0].actual_qualified == 1, (
        "看護師が保育士として数えられていない（実配置が 0 名と誤判定される）"
    )
    assert target[0].is_qualified_shortfall is False


def test_gap_analysisは認可保育所では看護師を数えない(licensed: StaffingStandard) -> None:
    requirements = _requirements(licensed, 3, DAY)
    slot = next(r.slot for r in requirements.for_day(DAY))
    staff = [member("N1", Role.KANGSHI), member("S1", Role.SHIENSHIIN)]
    result = _fake_result(staff, slot)
    report = gap_analysis.analyze_gap(requirements, result, staff, standard=licensed)
    target = [g for g in report.gaps if g.slot == slot]
    assert target[0].actual_qualified == 0
    assert target[0].is_qualified_shortfall is True


def test_live_validationが看護師を数える(facility: StaffingStandard) -> None:
    requirements = _requirements(facility, 3, DAY)
    slot = next(r.slot for r in requirements.for_day(DAY))
    assert requirements.needed_qualified(DAY, slot) == 1
    staff = [member("N1", Role.KANGSHI), member("S1", Role.SHIENSHIIN)]
    grid = _grid(requirements, staff, slot)

    report = live_validation.validate_day(
        DAY, requirements, staff, requirements.slots, grid, standard=facility
    )
    target = [
        i for i in report.errors if i.code == "SHORTFALL_QUALIFIED" and i.slot_label == slot.label
    ]
    assert not target, "看護師1名で必要保育士数を満たせるのに不足と判定した"

    as_licensed = live_validation.validate_day(
        DAY,
        requirements,
        staff,
        requirements.slots,
        grid,
        standard=local_rules.get_standard(LICENSED_KEY),
    )
    assert any(
        i.code == "SHORTFALL_QUALIFIED" and i.slot_label == slot.label for i in as_licensed.errors
    ), "認可保育所では看護師は保育士にならない"


def test_調理員は必要人員を埋めない(facility: StaffingStandard) -> None:
    """調理員だけを配置に入れても必要人員は埋まらないこと。"""
    requirements = _requirements(facility, 3, DAY)
    staff = [member("K1", Role.CHUUBOU), member("K2", Role.CHUUBOU)]
    gaps = solver.shortfall_rows(staff, requirements, standard=facility)
    assert gaps, "調理員だけでは何一つ埋まらないはず"
    assert all(g.supply_staff == 0 for g in gaps)


# ---------------------------------------------------------------------------
# 5. MILP: 看護師が上限を超えるときだけ 0/1 変数を追加すること
# ---------------------------------------------------------------------------


def _model_variables(facility: StaffingStandard, staff: list[StaffMember], infants: int):
    """``build_requirements`` まで組んだ MILP の変数名を返す（CBC 不要）。"""
    import pulp

    requirements = _requirements(facility, infants, DAY)
    ctx = solver._build_problem(
        staff,
        requirements,
        {},
        {},
        _FacilitySettings(day_open=OPEN, day_close=CLOSE, granularity_min=30),
        _ObjectiveWeights(),
        facility,
    )
    assert isinstance(ctx.prob, pulp.LpProblem)
    return requirements, [v.name for v in ctx.prob.variables()]


def test_看護師が1名以下なら変数を追加しない(facility: StaffingStandard) -> None:
    """典型的な園（看護師1名）ではモデル規模が変わらないこと。

    下の docs/02 §2.7 の 14,039 variables はこの前提で記載されている。
    """
    staff = [member("H1", Role.HOIKUSHI), member("N1", Role.KANGSHI)]
    _requirements, names = _model_variables(facility, staff, 3)
    assert not [n for n in names if n.startswith("nurse_used_")]


def test_看護師が上限を超えると0_1変数を追加する(facility: StaffingStandard) -> None:
    """看護師2名なら「1 人に限り」の制約を表す変数が要る。"""
    staff = [
        member("H1", Role.HOIKUSHI),
        member("N1", Role.KANGSHI),
        member("N2", Role.KANGSHI),
    ]
    _requirements, names = _model_variables(facility, staff, 3)
    added = [n for n in names if n.startswith("nurse_used_")]
    assert added, "看護師の上限を表す変数が無い（上限が制約になっていない）"


def test_認可外以外の基準では看護師で変数は増えない(licensed: StaffingStandard) -> None:
    staff = [
        member("H1", Role.HOIKUSHI),
        member("N1", Role.KANGSHI),
        member("N2", Role.KANGSHI),
    ]
    _requirements, names = _model_variables(licensed, staff, 3)
    assert not [n for n in names if n.startswith("nurse_used_")]


@pytest.mark.slow
def test_看護師2名でも1人分しか数えない(facility: StaffingStandard) -> None:
    """実際に解いた結果が「看護師 2 名で必要保育士数を満たした」と判定しないこと。"""
    from shiftai.domain import CellState

    requirements = _requirements(facility, 12, DAY)
    staff = [
        member("N1", Role.KANGSHI),
        member("N2", Role.KANGSHI),
        member("N3", Role.KANGSHI),
    ]
    result = solver.solve_shift([], staff, requirements, time_limit_sec=30, standard=facility)
    day = result.shift_days[0] if result.shift_days else None
    assert day is not None, "解が得られなかった"
    report = gap_analysis.analyze_gap(requirements, result, staff, standard=facility)
    for gap in report.gaps:
        nurses = sum(
            1 for m in staff if m.is_nurse and day.get(m.staff_id, gap.slot) is CellState.WORK
        )
        # 看護師だけを配置した時間帯で、保育士換算が 2 名を超えていないこと
        assert gap.actual_qualified <= facility.nurse_as_qualified_cap, (
            f"{gap.slot.label}: 看護師 {nurses} 名を数えて {gap.actual_qualified} 名となった"
        )
