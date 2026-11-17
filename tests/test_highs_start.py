"""MIP スタート（HiGHSWithStart）の回帰テスト。highspy 未導入環境では skip。"""

import pytest

pulp = pytest.importorskip("pulp")
if not pytest.importorskip("shiftai.solver").is_highs_available():
    pytest.skip("HiGHS が利用できません", allow_module_level=True)


def test_highs_with_start_reaches_optimal():
    """初期解を渡した小問題で HiGHS が最適解に到達すること。"""
    from shiftai.highs_start import HiGHSWithStart

    prob = pulp.LpProblem("t", pulp.LpMinimize)
    xs = [pulp.LpVariable(f"x{i}", 0, 1, cat=pulp.LpInteger) for i in range(30)]
    prob += pulp.lpSum((i % 7 + 1) * x for i, x in enumerate(xs))
    prob += pulp.lpSum(xs) >= 12
    prob += xs[0] + xs[1] + xs[2] <= 1
    for x in xs:
        x.setInitialValue(0)
    for x in xs[:12]:
        x.setInitialValue(1)

    prob.solve(HiGHSWithStart(msg=False, timeLimit=10, gapRel=0.03))
    assert pulp.LpStatus[prob.status] == "Optimal"
    assert pulp.value(prob.objective) == 22.0


def test_collect_highs_info_records_gap():
    """HiGHS 実行後に mip_gap/mip_dual_bound/mip_node_count が記録されること。"""
    from shiftai.highs_start import HiGHSWithStart
    from shiftai.solver import _collect_highs_info

    prob = pulp.LpProblem("t", pulp.LpMinimize)
    xs = [pulp.LpVariable(f"x{i}", 0, 1, cat=pulp.LpInteger) for i in range(10)]
    prob += pulp.lpSum((i + 1) * x for i, x in enumerate(xs))
    prob += pulp.lpSum(xs) >= 3
    prob.solve(HiGHSWithStart(msg=False, timeLimit=10))

    stats: dict = {}
    _collect_highs_info(prob, stats)
    assert "mip_gap" in stats
    assert "mip_dual_bound" in stats
    assert "mip_node_count" in stats
