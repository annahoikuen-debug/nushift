"""MILP ソルバーバックエンド検出・診断・インスタンス生成。"""

from __future__ import annotations

import os
from typing import Any

import pulp

from shiftai import config
from shiftai.highs_start import HiGHSWithStart


def is_highs_available() -> bool:
    """HiGHS ソルバーが利用可能か判定する。"""
    if not hasattr(pulp, "HiGHS"):
        return False
    try:
        return bool(pulp.HiGHS(msg=False).available())
    except Exception:
        return False


def is_cbc_available() -> bool:
    """CBC ソルバーが利用可能か判定する。"""
    try:
        return bool(pulp.PULP_CBC_CMD(msg=False).available())
    except Exception:
        return False


def available_solvers() -> list[str]:
    """PuLP 経由で利用可能な MILP ソルバーの一覧を返す。"""
    try:
        solvers = list(pulp.listSolvers(onlyAvailable=True))
        if is_highs_available() and "HiGHS" not in solvers:
            solvers.append("HiGHS")
        return solvers
    except Exception:
        return []


def select_optimal_solver(preference: str = "auto") -> tuple[str, bool]:
    """最適なソルバー名と利用可能性を判定して返す。

    Returns:
        tuple[str, bool]: (選択されたソルバー名, 利用可能フラグ)
    """
    pref = (preference or "auto").strip().lower()
    if pref in ("highs", "highspy"):
        if is_highs_available():
            return "HiGHS", True
        return "PULP_CBC_CMD", is_cbc_available()
    if pref in ("cbc", "pulp_cbc_cmd"):
        return "PULP_CBC_CMD", is_cbc_available()

    # auto: HiGHS 優先、なければ CBC
    if is_highs_available():
        return "HiGHS", True
    if is_cbc_available():
        return "PULP_CBC_CMD", True
    return "NONE", False


def solver_diagnostic_info() -> dict[str, Any]:
    """ソルバー診断情報を辞書形式で返す。UI やヘルスチェックで使用。"""
    highs_ok = is_highs_available()
    cbc_ok = is_cbc_available()
    optimal, any_ok = select_optimal_solver()
    return {
        "optimal_solver": optimal,
        "is_available": any_ok,
        "highs_available": highs_ok,
        "cbc_available": cbc_ok,
        "available_solvers": available_solvers(),
    }


def _get_solver(
    limit_sec: int,
    msg: bool,
    solver_name: str = "auto",
    warm_start: bool = False,
) -> tuple[pulp.LpSolver, str]:
    """指定または自動選択されたソルバーインスタンスとソルバー識別名を返す。"""
    limit = max(1, int(limit_sec))
    chosen = (solver_name or "auto").strip()
    if chosen == "auto":
        chosen, _ = select_optimal_solver("auto")

    if chosen.lower() in ("highs", "highspy") and is_highs_available():
        try:
            num_threads = min(4, os.cpu_count() or 1)
            return (
                HiGHSWithStart(
                    msg=msg,
                    timeLimit=limit,
                    threads=num_threads,
                    gapRel=config.HIGHS_GAP_REL,
                    mip_heuristic_effort=config.HIGHS_MIP_HEURISTIC_EFFORT,
                ),
                "HiGHS",
            )
        except Exception:
            pass

    return pulp.PULP_CBC_CMD(msg=msg, timeLimit=limit, warmStart=warm_start), "PULP_CBC_CMD"
