"""HiGHS に初期解（MIP スタート）を渡すソルバーサブクラス。

``pulp.HiGHS`` は ``actualSolve`` 内で ``callSolver`` を呼ぶ。
``callSolver`` の直前は ``lp.solverModel``（``highspy.Highs`` インスタンス）
が構築済みで、各変数には ``var.index`` と ``var.varValue`` が設定されている。
このタイミングで ``setSolution`` を呼ぶと HiGHS が初期解として採用する。

実測（highspy 1.16.0 / pulp 2.9.0）: ``setSolution(len, indices, values)`` は
``HighsStatus.kOk`` を返し、小問題では 0 ノードで最適解に到達した。
``setInitialValue`` は ``varValue`` に格納されるため、``apply_warm_start``
が設定した値がそのまま初期解になる。
"""
from __future__ import annotations

import pulp


class HiGHSWithStart(pulp.HiGHS):
    """``callSolver`` 直前に ``varValue`` を MIP スタートとして HiGHS へ渡す。"""

    def callSolver(self, lp: pulp.LpProblem) -> None:
        try:
            idx: list[int] = []
            val: list[float] = []
            for var in lp.variables():
                v = var.varValue
                if v is not None:
                    idx.append(var.index)
                    val.append(float(v))
            if idx:
                lp.solverModel.setSolution(len(idx), idx, val)
        except Exception:
            # 初期解の投入は最適化。失敗しても通常通り solve する。
            pass
        super().callSolver(lp)
