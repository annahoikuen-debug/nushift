"""優先2（勤務パターンの整列）と優先3（緩和モード・原因診断）の回帰テスト。"""

from __future__ import annotations

from datetime import date, time

import pytest

from shiftai import diagnostics, local_rules, solver
from shiftai.domain import CellState, FacilitySettings, SolveStatus
from shiftai.relaxation import (
    MAX_RELAX_LEVEL,
    RELAX_LEVELS,
    RelaxLevel,
    normalize_level,
    relax_level_index,
    relaxation_options,
    relaxed_constraint_labels,
    relaxed_weight_names,
)
from shiftai.shift_patterns import (
    DEFAULT_PATTERNS,
    ShiftPattern,
    default_patterns,
    describe_pattern,
    match_pattern,
    nearest_pattern_window,
    normalize_patterns,
    parse_pattern_spec,
    pattern_cost_per_slot,
)

DAYS = [date(2026, 9, 28), date(2026, 9, 29)]
OPEN = time(7, 15)
CLOSE = time(19, 30)


@pytest.fixture
def requirements(small_children):
    from shiftai.standards import build_requirements

    return build_requirements(
        small_children,
        DAYS,
        local_rules.get_standard("全国基準（厚労省）"),
        day_open=OPEN,
        day_close=CLOSE,
    )


# ---------------------------------------------------------------------------
# パターン定義
# ---------------------------------------------------------------------------


def test_既定の3枠は開始時刻順() -> None:
    assert [p.key for p in DEFAULT_PATTERNS] == ["early", "day", "late"]
    starts = [p.start_minutes for p in DEFAULT_PATTERNS]
    assert starts == sorted(starts)


def test_開所閉所から自動生成できる() -> None:
    pats = default_patterns(OPEN, CLOSE)
    assert len(pats) == 3
    assert pats[0].start == OPEN
    assert pats[-1].end == CLOSE
    assert all(p.minutes > 0 for p in pats)


def test_枠の長さは営業時間で頭打ちになる() -> None:
    """9:00〜14:00 の園に 9 時間枠を作らない（どの勤務とも一致しなくなるため）。"""
    pats = default_patterns(time(9, 0), time(14, 0))
    assert pats
    open_m = 9 * 60
    close_m = 14 * 60
    for p in pats:
        assert p.start_minutes >= open_m
        assert p.end_minutes <= close_m
        assert p.minutes <= close_m - open_m


def test_終了が始業と同じパターンは捨てられる() -> None:
    broken = ShiftPattern("x", "壊れ", time(9, 0), time(9, 0))
    good = DEFAULT_PATTERNS[0]
    assert normalize_patterns([broken, good]) == (good,)
    assert normalize_patterns([]) == ()
    assert normalize_patterns(None) == ()


def test_定義文字列を解釈できる() -> None:
    pat = parse_pattern_spec("早番=07:30-16:30")
    assert pat.label == "早番"
    assert pat.start == time(7, 30)
    assert pat.end == time(16, 30)
    bare = parse_pattern_spec("09:00-18:00")
    assert bare.start == time(9, 0)


@pytest.mark.parametrize("spec", ["", "09:00", "abc-def", "09:00-xx"])
def test_壊れた定義文字列は例外になる(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_pattern_spec(spec)


def test_厳密一致のパターンだけを返す() -> None:
    early = DEFAULT_PATTERNS[0]
    assert match_pattern(early.start_minutes, early.end_minutes, DEFAULT_PATTERNS) == early
    assert match_pattern(early.start_minutes + 5, early.end_minutes, DEFAULT_PATTERNS) is None


def test_一番近いパターンを返す() -> None:
    found = nearest_pattern_window(525, 1035, DEFAULT_PATTERNS)
    assert found is not None
    assert found.key == "day"


def test_ずれの説明文を作る() -> None:
    early = DEFAULT_PATTERNS[0]
    text = describe_pattern(early.start_minutes, early.end_minutes, DEFAULT_PATTERNS)
    assert "早番" in text
    off = describe_pattern(530, 1040, DEFAULT_PATTERNS)
    assert "分" in off


def test_境界のずれは0になる(requirements) -> None:
    slots = tuple(requirements.slots)
    early = DEFAULT_PATTERNS[0]
    matching = [s for s in slots if s.start_minutes == early.start_minutes]
    assert matching, "テスト用の時間帯にパターンの境界が存在しません"
    starts, ends = pattern_cost_per_slot(slots, DEFAULT_PATTERNS)
    assert min(starts) == 0.0
    assert min(ends) == 0.0


# ---------------------------------------------------------------------------
# 緩和モード
# ---------------------------------------------------------------------------


def test_緩和段階は0から4まで() -> None:
    assert [int(s.level) for s in RELAX_LEVELS] == list(range(MAX_RELAX_LEVEL + 1))
    assert RelaxLevel.STRICT == 0
    assert RelaxLevel.IGNORE_UNAVAILABLE == MAX_RELAX_LEVEL


def test_厳格では緩めない() -> None:
    assert relaxed_weight_names(0) == ()
    assert relaxed_constraint_labels(0) == ()
    assert relaxation_options()[0].startswith("L0 ")
    assert relax_level_index(0) == 0
    assert relax_level_index(MAX_RELAX_LEVEL) == MAX_RELAX_LEVEL
    assert relax_level_index(99) == MAX_RELAX_LEVEL


def test_段階をまたいで緩めた制約が増える() -> None:
    assert relaxed_weight_names(1) == ()
    assert "consecutive_day_penalty" in relaxed_weight_names(2)
    assert "rest_violation_penalty" in relaxed_weight_names(3)
    assert "希望休・不在時間帯" in relaxed_constraint_labels(4)


def test_段階番号は範囲内に丸められる() -> None:
    assert normalize_level(None) == 0
    assert normalize_level(-5) == 0
    assert normalize_level(99) == MAX_RELAX_LEVEL
    assert normalize_level("2") == 2


@pytest.mark.slow
def test_緩解モードでは配置基準が罰変数になる(
    requirements, small_children, small_staff, small_preferences
) -> None:
    strict = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
    )
    relaxed = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
        relaxation=int(RelaxLevel.SOFT_COVERAGE),
    )
    assert strict.status is not SolveStatus.ERROR
    assert relaxed.status is not SolveStatus.ERROR
    assert relaxed.stats["relaxation"] == int(RelaxLevel.SOFT_COVERAGE)
    assert relaxed.stats["relaxed_constraints"]
    # 1 パス目を飛ばすので、その旨が記録される
    assert "Skipped" in str(relaxed.stats["solver_status_pass1"])


# ---------------------------------------------------------------------------
# ソルバ側のパターン整列
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_パターンを渡すと整列率が上がる(
    requirements, small_children, small_staff, small_preferences
) -> None:
    plain = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
    )
    aligned = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
        patterns=DEFAULT_PATTERNS,
    )
    assert aligned.stats["patterns"] == [p.label for p in DEFAULT_PATTERNS]
    plain_counts = solver.pattern_breakdown(plain, DEFAULT_PATTERNS)
    aligned_counts = solver.pattern_breakdown(aligned, DEFAULT_PATTERNS)
    assert plain_counts is not None
    assert sum(aligned_counts.values()) >= sum(plain_counts.values())


@pytest.mark.slow
def test_スナップは配置基準を壊さない(
    requirements, small_children, small_staff, small_preferences
) -> None:
    pats = (
        ShiftPattern("a", "枠A", time(9, 0), time(13, 0)),
        ShiftPattern("b", "枠B", time(9, 30), time(13, 30)),
        ShiftPattern("c", "枠C", time(10, 0), time(14, 0)),
    )
    result = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
        patterns=pats,
    )
    snapped, changes = solver.snap_to_patterns(
        result,
        pats,
        requirements=requirements,
        preferences=small_preferences,
        staff=small_staff,
    )
    assert changes, "スナップの確認対象がない"
    moved = [c for c in changes if c.applied]
    for change in moved:
        assert change.pattern_label in {p.label for p in pats}
    # 適用しても必要人员在時間帯で下回らない
    for day in DAYS:
        for slot in requirements.slots:
            need = sum(
                r.needed_staff for r in requirements.for_day(day) if r.slot == slot and r.is_binding
            )
            if need <= 0:
                continue
            got = sum(
                1
                for a in snapped.assignments
                if a.day == day and a.slot == slot and a.state is CellState.WORK
            )
            assert got >= need, f"{day} {slot.label} の配置が不足しています"


@pytest.mark.slow
def test_空パターンのときは何もしない(
    requirements, small_children, small_staff, small_preferences
) -> None:
    result = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
    )
    same, changes = solver.snap_to_patterns(result, ())
    assert same is result
    assert changes == ()


@pytest.mark.slow
def test_勤務枠の説明を出せる(requirements, small_children, small_staff, small_preferences) -> None:
    result = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
        patterns=DEFAULT_PATTERNS,
    )
    day = result.shift_days[0].day
    sid = result.shift_days[0].assignments.keys().__iter__().__next__()
    text = solver.describe_shift_pattern(result, sid, day, DEFAULT_PATTERNS)
    assert text == "オフ" or ":" in text


# ---------------------------------------------------------------------------
# 診断
# ---------------------------------------------------------------------------


def test_供給が足りていれば構造診断では何も言わない(
    requirements, small_staff, small_preferences
) -> None:
    report = diagnostics.diagnose(requirements, small_staff, small_preferences, FacilitySettings())
    assert report.rows == ()
    assert report.structural_conflicts == ()
    assert report.headline()


def test_供給が足りないとき不足行を返す(requirements, small_staff, small_preferences) -> None:
    """職員 1 人だけの供給で広い必要人員は埋まらない。"""
    one = small_staff[:1]
    report = diagnostics.diagnose(requirements, one, {})
    assert report.rows, "不足が検出されませんでした"
    assert report.total_gap > 0
    assert report.worst is not None
    labels = {r.code for r in report.reasons}
    assert "STAFF_SHORT" in labels or "HOURS_SHORT" in labels
    assert "保育標準時間" in report.headline() or "需要" in report.headline()


def test_不足行はDataFrameになる(requirements, small_staff) -> None:
    report = diagnostics.diagnose(requirements, small_staff[:1], {})
    frame = diagnostics.shortfall_dataframe(report)
    assert not frame.empty
    assert "不足人員" in frame.columns
    assert (frame["必要人員"] >= frame["供給人員"]).all()


def test_比率が0除算にならない(requirements, small_staff, small_preferences) -> None:
    report = diagnostics.diagnose(requirements, small_staff, small_preferences)
    assert report.ratio is None or report.ratio >= 0.0


def test_削除フィルタで矛盾の当事者を残す() -> None:
    """「contract を外せば解ける」なら contract だけが当事者として残る。"""
    groups = diagnostics.CONSTRAINT_GROUPS

    def feasible_without(key: str) -> bool:
        return key == "contract"

    core = diagnostics.find_conflict_core(feasible_without, groups)
    assert [g.key for g in core] == ["contract"]


def test_外しても解けないものは当事者から外れる() -> None:
    """外しても解けないグループは「矛盾の当事者ではない」と判断される。"""
    core = diagnostics.find_conflict_core(lambda key: False, diagnostics.CONSTRAINT_GROUPS)
    # 最後の 1 つは「外すと検証対象がなくなる」ため残る
    assert len(core) == 1
    assert core[0] is diagnostics.CONSTRAINT_GROUPS[-1]


def test_全部外せるなら全部が残る() -> None:
    core = diagnostics.find_conflict_core(lambda key: True, diagnostics.CONSTRAINT_GROUPS[:3])
    assert [g.key for g in core] == [g.key for g in diagnostics.CONSTRAINT_GROUPS[:3]]


def test_配置基準はIISの対象に含めない() -> None:
    """配置基準を外すと必ず解けるので、IIS には含めない（Slack 側で診断する）。"""
    assert "coverage" not in {g.key for g in diagnostics.CONSTRAINT_GROUPS}


def test_制約グループのキーがソルバのdrop先と一致する() -> None:
    """IIS で外すキーがすべて ``solve_shift(drop_groups=...)`` に対応していること。"""
    import inspect

    from shiftai import solver as solver_module

    source = inspect.getsource(solver_module._build_problem) + inspect.getsource(
        solver_module._build_cells
    )
    for group in diagnostics.CONSTRAINT_GROUPS:
        assert f'"{group.key}"' in source, f"{group.key} を drop_groups に対応付けていません"


@pytest.mark.slow
def test_緩和ラダーは段階を順に試す(
    requirements, small_children, small_staff, small_preferences
) -> None:
    ladder = diagnostics.relaxation_ladder(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        time_limit_sec=10,
        max_level=1,
    )
    assert len(ladder.outcomes) >= 1
    assert ladder.outcomes[0].level == 0
    first = ladder.first_solved
    assert first is not None
    assert ladder.messages()


@pytest.mark.slow
def test_drop_groupsで制約を外すと解ありと判定される(
    requirements, small_children, small_staff, small_preferences
) -> None:
    """「勤務セルが無くても解あり」と判定する。

    配置基準を外すと「全員オフ」が最適解になるが、IIS の判定では
    これは実行可能なのである。
    """
    dropped = solver.solve_shift(
        small_children,
        small_staff,
        requirements,
        small_preferences,
        None,
        time_limit_sec=20,
        drop_groups=frozenset({"contract"}),
    )
    assert dropped.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    assert dropped.stats["solver_status"]
    # 診断用の実行なので割当は出力されない（デコードを伴わない）
    assert dropped.assignments == []


@pytest.mark.slow
def test_供給行の公開関数が使える(requirements, small_staff, small_preferences) -> None:
    rows = solver.shortfall_rows(small_staff, requirements, small_preferences)
    assert isinstance(rows, tuple)
    for row in rows:
        assert row.gap > 0
        assert row.gap == row.gap_staff + row.gap_qualified
        assert row.weekday
        assert ":" in row.describe()
