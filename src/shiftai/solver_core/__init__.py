"""MILP モデルの部品モジュール。

現在稼働している実装は :mod:`shiftai.solver`（モノリス）に統合されており、
このパッケージには実際に ``solver.py`` から参照されるモジュールだけが残っている:

* :mod:`shiftai.solver_core.constraints.classrooms` — クラス配置・開所/閉所スキル制約
* :mod:`shiftai.solver_core.repair` — 当日欠勤リペア（``shiftai.solver`` へのエイリアス）

かつてここにあった ``solve`` / ``engine`` / ``decoder`` / ``diagnosis`` /
``post_process`` / ``context`` と他の制約モジュールは、``solver.py`` と
静かに乖離した未参照の二重実装だったため削除した（単一の正を維持するため）。
"""

from __future__ import annotations

from shiftai.solver_core.repair import repair_shift_absence

__all__ = ["repair_shift_absence"]
