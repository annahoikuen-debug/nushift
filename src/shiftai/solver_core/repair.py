"""当日突発欠勤リアルタイムリペア（動的穴埋め）。

実装本体は :func:`shiftai.solver.repair_shift_absence` に統合された
（旧 ``solver_core`` 並行実装の整理）。循環 import を避けるため
``shiftai.solver`` 側の関数を遅延 re-export する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shiftai.domain import SolveResult


def repair_shift_absence(*args: object, **kwargs: object) -> "SolveResult":
    """:func:`shiftai.solver.repair_shift_absence` への後方互換エイリアス。"""
    from shiftai.solver import repair_shift_absence as _repair

    return _repair(*args, **kwargs)  # type: ignore[arg-type]


__all__ = ["repair_shift_absence"]
