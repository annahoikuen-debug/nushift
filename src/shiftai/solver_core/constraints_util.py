"""``shiftai.solver`` 内部ヘルパーの再エクスポート。

``solver_core.constraints.classrooms`` が ``shiftai.solver`` の
プライベートヘルパー（``_ModelCtx`` / ``_hard_ge`` / ``_is_zero`` / ``_linear``）
を参照するための中継。solver.py を直接 import すると循環になるため、
``solver._build_problem`` 内の遅延 import 経由でのみ使われる。
"""

from shiftai.solver import (  # noqa: F401
    _hard_ge,
    _is_zero,
    _linear,
)
from shiftai.solver import _ModelCtx  # noqa: F401

__all__ = ["_ModelCtx", "_hard_ge", "_is_zero", "_linear"]
