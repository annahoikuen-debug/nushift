"""月次シフト2段階階層的求解（Hierarchical MILP）エンジン。

Stage 1: 月間日次パターン割り当て（職員×日付×勤務パターン）
  - 連続勤務上限（前月末境界バッファ考慮）
  - 月間所定公休目標の厳密遵守
  - パート職員の契約労働時間・所定労働日数遵守
  - 日次・時間帯ごとの概略配置需要充足

Stage 2: 週スライス単位の 30分スロット詳細配置 & 休憩スタッガー最適化
  - Stage 1 の公休および勤務パターンの時間外スロットを OFF 固定
  - パターン時間内の 30分スロットのみで配置基準充足・休憩スタッガーを最適化
  - 変数数を激減させ、高速かつ確実に実行可能解（FEASIBLE / OPTIMAL）に到達
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace

import pulp

from shiftai.domain import (
    CellState,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    ObjectiveWeights,
    RequirementTable,
    Role,
    ShiftDay,
    SlotKind,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    to_minutes,
)
from shiftai.shift_patterns import ShiftPattern
from shiftai.solver import is_highs_available, snap_to_patterns, solve_shift


@dataclass(frozen=True)
class MonthlySolveContext:
    target_month: str
    all_days: list[dt.date]
    staff: list[StaffMember]
    contracts: dict[str, Contract]
    preferences: dict[str, StaffPreferences]
    patterns: list[ShiftPattern]
    requirements: RequirementTable
    facility_settings: FacilitySettings
    fixed_assignments: dict[tuple[str, dt.date, str], CellState]
    prior_duty_history: dict[str, int]
    monthly_holiday_target: int
    children: Sequence[ChildPlan] = field(default_factory=tuple)
    standard: StaffingStandard | None = None
    weights: ObjectiveWeights | None = None


def _ensure_effective_patterns(context: MonthlySolveContext) -> list[ShiftPattern]:
    """園の開所・閉所スロットおよび職員構成（パート・短時間）に即した実務パターン群を整備・拡充する。"""
    existing_patterns = list(context.patterns or ())
    slots = tuple(context.requirements.slots)
    if not slots:
        return existing_patterns

    open_min = slots[0].start_minutes
    close_min = slots[-1].end_minutes

    def _clock_m(m: int) -> dt.time:
        m = max(0, min(1440, m))
        return dt.time((m // 60) % 24, m % 60)

    # 開所〜閉所の実務標準枠（フルタイム 9h拘束、パート 5h拘束）
    full_span = min(9 * 60, close_min - open_min)
    part_span = min(5 * 60, close_min - open_min)

    cand_patterns: list[ShiftPattern] = []

    # 1. フルタイム勤務（9時間拘束 = 8時間実働 + 1時間休憩）
    for start_m, key, label, sym in [
        (open_min, "early1", "早番1", "早1"),
        (open_min + 30, "early2", "早番2", "早2"),
        (open_min + 60, "early3", "早番3", "早3"),
        (8 * 60 + 30, "day1", "日勤1", "日1"),
        (9 * 60, "day2", "日勤2", "日2"),
        (close_min - full_span - 30, "late1", "遅番1", "遅1"),
        (close_min - full_span, "late2", "遅番2", "遅2"),
    ]:
        if start_m >= open_min and start_m + full_span <= close_min:
            cand_patterns.append(
                ShiftPattern(key, label, _clock_m(start_m), _clock_m(start_m + full_span), symbol=sym)
            )

    # 2. 短時間パート勤務（5時間拘束 = 5時間実働、休憩0分）
    for start_m, key, label, sym in [
        (open_min, "part_early1", "早番パート1", "前1"),
        (open_min + 30, "part_early2", "早番パート2", "前2"),
        (open_min + 60, "part_early3", "早番パート3", "前3"),
        (8 * 60 + 30, "part_mid1", "中勤パート1", "中1"),
        (9 * 60, "part_mid2", "中勤パート2", "中2"),
        (10 * 60, "part_mid3", "中勤パート3", "中3"),
        (13 * 60, "part_pm", "午後パート", "後1"),
        (close_min - part_span, "part_late", "遅番パート", "後2"),
    ]:
        if start_m >= open_min and start_m + part_span <= close_min:
            cand_patterns.append(
                ShiftPattern(key, label, _clock_m(start_m), _clock_m(start_m + part_span), symbol=sym)
            )

    # 既存パターンで上書き・優先マージ（重複する開始・終了時刻は既存を尊重）
    result: list[ShiftPattern] = []
    seen: set[tuple[int, int]] = set()
    for p in existing_patterns:
        key = (p.start_minutes, p.end_minutes)
        seen.add(key)
        result.append(p)

    for p in cand_patterns:
        key = (p.start_minutes, p.end_minutes)
        if key not in seen:
            seen.add(key)
            result.append(p)

    return sorted(result, key=lambda p: (p.start_minutes, p.end_minutes))


def solve_monthly_hierarchical(
    context: MonthlySolveContext,
    time_limit_sec: int = 120,
    solver_name: str | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    cancellation_token: object | None = None,
    parallel_workers: int = 1,
    weights: ObjectiveWeights | None = None,
) -> SolveResult:
    """2段階階層的アプローチにより月次一括シフトを求解する。

    Timefold 思想に基づく拡張:
      - progress_callback: 求解フェーズ・進捗率（0-100%）・暫定スコアのリアルタイム通知
      - cancellation_token: 協調的キャンセル機構（中断時も直前までの暫定最良解を保持）
      - parallel_workers: Stage 2 日スライスのマルチコア並列探索 (Partitioned Search)
      - weights: 園別・実行時制約重みの注入（未指定時は context.weights または規定値）
      - ruin_and_recreate: Stage 2 で解なし・タイムアウト時の局所探索自動フォールバック
    """
    def _is_cancelled() -> bool:
        if cancellation_token is None:
            return False
        if hasattr(cancellation_token, "is_cancelled"):
            return cancellation_token.is_cancelled()
        if hasattr(cancellation_token, "is_set"):
            return cancellation_token.is_set()
        if callable(cancellation_token):
            return bool(cancellation_token())
        return False

    def _notify(stage: str, progress: int, day: str | None = None, extra: dict | None = None) -> None:
        if progress_callback is None:
            return
        payload = {
            "stage": stage,
            "progress_percent": progress,
            "current_day": day,
            "total_days": len(context.all_days),
        }
        if extra:
            payload.update(extra)
        try:
            progress_callback(payload)
        except Exception:
            pass

    _notify("STAGE_1_PATTERNS", 5)

    if _is_cancelled():
        return SolveResult(
            status=SolveStatus.PARTIAL,
            shift_days=[],
            violations=[],
            messages=["Cancelled before Stage 1"],
            stats={"cancelled": True},
        )

    # 開所・閉所スロットおよびパート枠に即した実務パターン群を自動拡充
    effective_patterns = _ensure_effective_patterns(context)
    if len(effective_patterns) != len(context.patterns):
        context = replace(context, patterns=effective_patterns)

    # ソルバーの自動選択（HiGHS 優先、なければ CBC）
    effective_solver = solver_name
    if not effective_solver:
        effective_solver = "HiGHS" if is_highs_available() else "PULP_CBC_CMD"

    # 有効な制約重み（DB または呼び出し元から注入）
    effective_weights = (
        weights
        or getattr(context, "weights", None)
        or ObjectiveWeights(split_duty_penalty=5000.0)
    )

    # ---------------------------------------------------------
    # Stage 1: 月間日次パターンの割り当て (MILP)
    # ---------------------------------------------------------
    stage1_time = min(max(time_limit_sec // 4, 15), 30)
    stage1_res = _solve_stage1_daily_patterns(
        context=context,
        time_limit_sec=stage1_time,
        solver_name=effective_solver,
        weights=effective_weights,
    )

    _notify("STAGE_1_DONE", 20, extra={"stage1_patterns": len(stage1_res)})

    if _is_cancelled():
        return SolveResult(
            status=SolveStatus.PARTIAL,
            shift_days=[],
            violations=[],
            messages=["Cancelled after Stage 1"],
            stats={"cancelled": True, "stage1_patterns": len(stage1_res)},
        )

    combined_fixed = dict(context.fixed_assignments)
    warm_starts: dict[tuple[str, dt.date, str], CellState] = {}

    slots = tuple(context.requirements.slots)
    pattern_by_key = {p.key: p for p in context.patterns}

    for (staff_id, day), pat_key in stage1_res.items():
        if pat_key == "OFF":
            for s in slots:
                key = (staff_id, day, s.label)
                if key not in combined_fixed:
                    # Stage 1 で OFF と判定された日は Stage 2 でも全スロット OFF 固定
                    combined_fixed[key] = CellState.OFF
                    warm_starts[key] = CellState.OFF
            continue

        pat = pattern_by_key.get(pat_key)
        if not pat:
            continue

        pat_start_min = to_minutes(pat.start)
        pat_end_min = to_minutes(pat.end)

        for s in slots:
            key = (staff_id, day, s.label)
            if key in combined_fixed:
                continue
            if s.start_minutes >= pat_start_min and s.end_minutes <= pat_end_min:
                warm_starts[key] = CellState.WORK
            else:
                # パターン時間外のスロットは OFF に固定して変数を大幅削減
                combined_fixed[key] = CellState.OFF
                warm_starts[key] = CellState.OFF

    # ---------------------------------------------------------
    # Stage 2: 日スライス単位の 30分スロット詳細配置 & 休憩スタッガー最適化
    # ---------------------------------------------------------
    chunk_size = 1
    day_chunks = [
        context.all_days[i : i + chunk_size]
        for i in range(0, len(context.all_days), chunk_size)
    ]

    all_shift_days: list[ShiftDay] = []
    all_violations: list[Violation] = []
    all_messages: list[str] = []
    is_all_optimal = True
    is_any_feasible = False

    weights = effective_weights
    remaining_budget = max(time_limit_sec - stage1_time, 30)
    per_chunk_time = max(remaining_budget // max(len(day_chunks), 1), 10)

    total_chunks = len(day_chunks)
    completed_chunks = 0
    progress_lock = threading.Lock()

    def _solve_chunk_task(week_days: list[dt.date]) -> tuple[list[ShiftDay], list[Violation], list[str], SolveStatus] | None:
        if _is_cancelled():
            return None

        week_days_set = set(week_days)
        closed_days_set = set(getattr(context.facility_settings, "closed_days", None) or [])
        if all(
            d in closed_days_set or (hasattr(context.requirements, "is_closed") and context.requirements.is_closed(d))
            for d in week_days
        ):
            # 休園日は全職員 OFF の ShiftDay を直接生成して即時返却
            closed_sds = [
                ShiftDay(day=d, assignments={s.staff_id: {slot.label: CellState.OFF for slot in slots} for s in context.staff})
                for d in week_days
            ]
            return (closed_sds, [], [f"{d.isoformat()} は休園日のため全員 OFF です" for d in week_days], SolveStatus.OPTIMAL)

        week_children = [c for c in context.children if c.day in week_days_set]

        week_requirements = RequirementTable(
            day_open=context.requirements.day_open,
            day_close=context.requirements.day_close,
            granularity_min=context.requirements.granularity_min,
            slots=context.requirements.slots,
            rows={d: context.requirements.for_day(d) for d in week_days},
            notes=list(context.requirements.notes),
        )

        week_fixed = {k: v for k, v in combined_fixed.items() if k[1] in week_days_set}
        week_warm = {k: v for k, v in warm_starts.items() if k[1] in week_days_set}

        # 早朝・夕方延長保育時間帯での休憩取得を禁止（勤務パターン内の時間帯は WORK に確定）
        if context.standard:
            for s in slots:
                kind = context.standard.slot_kind(s)
                if kind in (SlotKind.EARLY, SlotKind.LATE, SlotKind.LATE_STRICT):
                    for s_mem in context.staff:
                        for d in week_days:
                            k = (s_mem.staff_id, d, s.label)
                            if week_warm.get(k) == CellState.WORK:
                                week_fixed[k] = CellState.WORK

        sub_res = solve_shift(
            week_children,
            staff=context.staff,
            requirements=week_requirements,
            preferences=context.preferences,
            weights=weights,
            settings=context.facility_settings,
            fixed_assignments=week_fixed,
            time_limit_sec=per_chunk_time,
            solver_name=effective_solver,
            warm_start=True,
            warm_start_assignments=week_warm,
            patterns=context.patterns,
            pattern_mode=True,
            standard=context.standard,
        )

        if context.patterns and sub_res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
            sub_res, _ = snap_to_patterns(
                sub_res,
                context.patterns,
                requirements=week_requirements,
                preferences=context.preferences,
                staff=context.staff,
                settings=context.facility_settings,
            )

        # Timefold 思想: MILP が解なしまたはタイムアウトで実行不可能解となった場合、
        # 自動的に Ruin & Recreate 局所探索フォールバックを起動して修復する
        if sub_res.status not in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE) or not sub_res.shift_days:
            try:
                from shiftai.local_search import ruin_and_recreate
                ls_res = ruin_and_recreate(
                    initial_result=sub_res,
                    requirements=week_requirements,
                    staff=context.staff,
                    preferences=context.preferences,
                    standard=context.standard,
                    settings=context.facility_settings,
                    fixed_assignments=week_fixed,
                    patterns=context.patterns,
                    weights=weights,
                )
                if ls_res.shift_days:
                    sub_res = ls_res
            except Exception as e:
                sub_res.messages.append(f"Local search fallback error: {e}")

        return (sub_res.shift_days, sub_res.violations, sub_res.messages, sub_res.status)

    was_cancelled = False

    if parallel_workers > 1 and len(day_chunks) > 1:
        # Partitioned Search: 日スライスを並列求解
        chunk_results: dict[int, tuple[list[ShiftDay], list[Violation], list[str], SolveStatus]] = {}
        with ThreadPoolExecutor(max_workers=min(parallel_workers, len(day_chunks))) as executor:
            future_to_idx = {
                executor.submit(_solve_chunk_task, chunk): idx
                for idx, chunk in enumerate(day_chunks)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                if _is_cancelled():
                    was_cancelled = True
                    break
                try:
                    res_tuple = future.result()
                    if res_tuple is not None:
                        chunk_results[idx] = res_tuple
                except Exception as e:
                    all_messages.append(f"Chunk {idx} error: {e}")

                with progress_lock:
                    completed_chunks += 1
                    pct = 20 + int(75 * completed_chunks / total_chunks)
                    day_str = day_chunks[idx][0].isoformat() if day_chunks[idx] else None
                    _notify("STAGE_2_SLOTS", min(pct, 95), day=day_str, extra={"completed_days": completed_chunks})

        for idx in range(total_chunks):
            if idx in chunk_results:
                sd_list, v_list, m_list, status = chunk_results[idx]
                all_shift_days.extend(sd_list)
                all_violations.extend(v_list)
                all_messages.extend(m_list)
                if status == SolveStatus.OPTIMAL:
                    is_any_feasible = True
                elif status == SolveStatus.FEASIBLE:
                    is_all_optimal = False
                    is_any_feasible = True
                else:
                    is_all_optimal = False
    else:
        # 直列求解
        for idx, week_days in enumerate(day_chunks):
            if _is_cancelled():
                was_cancelled = True
                break
            res_tuple = _solve_chunk_task(week_days)
            if res_tuple is None:
                was_cancelled = True
                break

            sd_list, v_list, m_list, status = res_tuple
            all_shift_days.extend(sd_list)
            all_violations.extend(v_list)
            all_messages.extend(m_list)

            if status == SolveStatus.OPTIMAL:
                is_any_feasible = True
            elif status == SolveStatus.FEASIBLE:
                is_all_optimal = False
                is_any_feasible = True
            else:
                is_all_optimal = False

            completed_chunks += 1
            pct = 20 + int(75 * completed_chunks / total_chunks)
            day_str = week_days[0].isoformat() if week_days else None
            _notify("STAGE_2_SLOTS", min(pct, 95), day=day_str, extra={"completed_days": completed_chunks})

    # 解けた日をソート
    all_shift_days.sort(key=lambda x: x.day)

    if was_cancelled:
        overall_status = SolveStatus.PARTIAL
        all_messages.append("Solve cancelled by user request; partial checkpoint solution returned")
    elif is_all_optimal and len(all_shift_days) == len(context.all_days):
        overall_status = SolveStatus.OPTIMAL
    elif is_any_feasible and len(all_shift_days) == len(context.all_days):
        overall_status = SolveStatus.FEASIBLE
    else:
        overall_status = SolveStatus.PARTIAL

    res = SolveResult(
        status=overall_status,
        shift_days=all_shift_days,
        violations=all_violations,
        messages=list(dict.fromkeys(all_messages)),
        stats={
            "solver": "hierarchical_monthly",
            "backend_solver": effective_solver,
            "num_days": len(context.all_days),
            "num_chunks": len(day_chunks),
            "completed_chunks": completed_chunks,
            "stage1_patterns": len(stage1_res),
            "parallel_workers": parallel_workers,
            "cancelled": was_cancelled,
        },
    )
    try:
        from shiftai.score import compute_shift_score

        res.score = compute_shift_score(
            shift_days=res.shift_days,
            violations=res.violations,
            requirements=context.requirements,
            staff=context.staff,
            preferences=context.preferences,
            weights=weights,
        )
    except Exception:
        pass

    _notify("DONE", 100, extra={
        "status": res.status.value,
        "days": len(res.shift_days),
        "hard_score": res.score.hard_score if res.score else 0,
        "soft_score": res.score.soft_score if res.score else 0.0,
    })

    return res


def _solve_stage1_daily_patterns(
    context: MonthlySolveContext,
    time_limit_sec: int = 30,
    solver_name: str | None = None,
    weights: ObjectiveWeights | None = None,
) -> dict[tuple[str, dt.date], str]:
    """Stage 1: 日単位のパターン割り当てモデルを PuLP で定式化・求解する。"""
    prob = pulp.LpProblem("Monthly_Stage1_Pattern_Assignment", pulp.LpMinimize)

    # 保育枠に計上可能な職員（園長・調理員など配置対象外を除く）のみを対象とする
    placeable_staff = [s for s in context.staff if s.is_placeable]
    staff_ids = [s.staff_id for s in placeable_staff]
    days = context.all_days
    patterns = context.patterns
    pat_keys = [p.key for p in patterns]

    x_vars: dict[tuple[str, dt.date, str], pulp.LpVariable] = {}
    duty_vars: dict[tuple[str, dt.date], pulp.LpVariable] = {}

    for sid in staff_ids:
        for d in days:
            duty_vars[(sid, d)] = pulp.LpVariable(f"duty_{sid}_{d.isoformat()}", cat=pulp.LpBinary)
            for p in patterns:
                x_vars[(sid, d, p.key)] = pulp.LpVariable(
                    f"x_{sid}_{d.isoformat()}_{p.key}", cat=pulp.LpBinary
                )

    obj_terms: list[pulp.LpAffineExpression] = []

    # 制約1: 1日1パターン以下、勤務時は duty_vars とリンク
    for sid in staff_ids:
        for d in days:
            prob += pulp.lpSum([x_vars[(sid, d, pk)] for pk in pat_keys]) == duty_vars[(sid, d)]

    # 制約1-1: 休園日（closed_days）は全職員出勤禁止（全パターン 0 / duty 0）
    closed_days = set(getattr(context.facility_settings, "closed_days", None) or [])
    for d in days:
        is_closed = (d in closed_days) or (
            hasattr(context.requirements, "is_closed") and context.requirements.is_closed(d)
        )
        if is_closed:
            for sid in staff_ids:
                prob += duty_vars[(sid, d)] == 0
                for p in patterns:
                    prob += x_vars[(sid, d, p.key)] == 0

    # 制約1-2: 手動確定（fixed_assignments）の反映
    fixed_by_staff_day: dict[tuple[str, dt.date], set[CellState]] = {}
    for (fsid, fday, _), fstate in context.fixed_assignments.items():
        if (fsid, fday) in duty_vars:
            fixed_by_staff_day.setdefault((fsid, fday), set()).add(fstate)

    for (fsid, fday), fstates in fixed_by_staff_day.items():
        if CellState.WORK in fstates:
            prob += duty_vars[(fsid, fday)] == 1
        elif CellState.OFF in fstates or fstates == {CellState.OFF}:
            prob += duty_vars[(fsid, fday)] == 0

    # 制約1-3: 希望休（pref.unavailable）の反映
    for sid in staff_ids:
        pref = context.preferences.get(sid)
        if not pref or not pref.unavailable:
            continue
        for u in pref.unavailable:
            if u.day in days:
                d = u.day
                u_start = u.start.hour * 60 + u.start.minute
                u_end = u.end.hour * 60 + u.end.minute
                is_all_day = (u_start == 0 and u_end >= 23 * 60 + 50)
                for p in patterns:
                    if is_all_day:
                        prob += x_vars[(sid, d, p.key)] == 0
                    else:
                        if max(p.start_minutes, u_start) < min(p.end_minutes, u_end):
                            prob += x_vars[(sid, d, p.key)] == 0

    # 制約2: 契約所定労働時間（daily_hours）および最低勤務時間のパターン制限
    for s in placeable_staff:
        sid = s.staff_id
        contract = context.contracts.get(sid) or s.contract
        fac_sei_max = getattr(context.facility_settings, "sei_max_daily_hours", 8.0)
        if contract and contract.max_daily_hours is not None:
            max_daily_h = contract.max_daily_hours
        elif contract and getattr(contract, "daily_hours", None) is not None:
            max_daily_h = (
                min(contract.daily_hours, fac_sei_max)
                if contract.employment_type == EmploymentType.SEI
                else contract.daily_hours
            )
        else:
            max_daily_h = fac_sei_max
        min_daily_h = 0.0
        if contract and contract.employment_type == EmploymentType.SEI:
            min_daily_h = (
                contract.min_daily_hours
                if contract.min_daily_hours is not None
                else getattr(context.facility_settings, "sei_min_daily_hours", 4.0)
            )
        elif contract and contract.min_daily_hours is not None:
            min_daily_h = contract.min_daily_hours

        for p in patterns:
            pat_h = (to_minutes(p.end) - to_minutes(p.start)) / 60.0
            work_h = pat_h - 1.0 if pat_h > 6.0 else pat_h
            if work_h > max_daily_h + 0.1 or (min_daily_h > 0 and work_h < min_daily_h - 0.1):
                # この職員の 1日上限超過または最低勤務時間未満のパターンは禁止
                for d in days:
                    prob += x_vars[(sid, d, p.key)] == 0

    # 制約3: 月間公休目標
    for sid in staff_ids:
        contract = context.contracts.get(sid)
        target_holidays = context.monthly_holiday_target
        if contract and hasattr(contract, "monthly_holiday_target") and contract.monthly_holiday_target:
            target_holidays = contract.monthly_holiday_target
        elif contract and getattr(contract, "weekly_work_days", None):
            est_work_days = round(contract.weekly_work_days * len(days) / 7.0)
            target_holidays = max(0, len(days) - est_work_days)

        work_days_target = len(days) - target_holidays
        prob += pulp.lpSum([duty_vars[(sid, d)] for d in days]) <= work_days_target

    # 制約4: 連続勤務上限（前月末連勤履歴を仮想ウィンドウで正確に考慮）
    for sid in staff_ids:
        contract = context.contracts.get(sid)
        max_streak = 5
        if contract and hasattr(contract, "max_consecutive_days") and contract.max_consecutive_days:
            max_streak = contract.max_consecutive_days

        prior_streak = min(context.prior_duty_history.get(sid, 0), max_streak)
        # 前月末の連勤履歴（1）と月内の勤務変数を結合した拡張系列で sliding window を適用
        extended_vars: list[pulp.LpVariable | int] = [1] * prior_streak + [
            duty_vars[(sid, d)] for d in days
        ]
        window_size = max_streak + 1
        for i in range(len(extended_vars) - window_size + 1):
            window_slice = extended_vars[i : i + window_size]
            prob += pulp.lpSum(window_slice) <= max_streak

    # 制約5: 日次最低必要人員（大まかな確保）および時間帯別カバー制約（朝・昼・夕の必要人員と有資格者）
    qualified_staff_ids = {
        s.staff_id for s in placeable_staff
        if getattr(s, "is_qualified", False) or s.has_role(Role.HOIKUSHI)
    }
    slots = tuple(context.requirements.slots)

    for d in days:
        req_list = context.requirements.for_day(d)
        max_req = max([r.needed_staff for r in req_list], default=0)
        if max_req > 0:
            prob += pulp.lpSum([duty_vars[(sid, d)] for sid in staff_ids]) >= min(max_req, len(staff_ids))

        # スロットごとの時間帯別カバー制約
        for idx, s in enumerate(slots):
            n_staff = context.requirements.needed_staff(d, s)
            n_qual = context.requirements.needed_qualified(d, s)
            if n_staff <= 0 and n_qual <= 0:
                continue

            mid_min = s.start_minutes + s.minutes // 2
            covering_pats = [p.key for p in patterns if p.start_minutes <= mid_min < p.end_minutes]
            if not covering_pats:
                continue

            # 昼休憩帯（11:30〜14:30）は交代休憩があるためバッファ +1
            is_lunch_peak = (11 * 60 + 30 <= mid_min < 14 * 60 + 30)
            target_staff = n_staff + (1 if is_lunch_peak else 0)
            target_qual = n_qual + (1 if is_lunch_peak else 0)

            if n_staff > 0:
                v_slack_s = pulp.LpVariable(f"slack_staff_{d.isoformat()}_{idx}", lowBound=0)
                prob += (
                    pulp.lpSum([x_vars[(sid, d, pk)] for sid in staff_ids for pk in covering_pats])
                    + v_slack_s
                    >= min(target_staff, len(staff_ids))
                )
                obj_terms.append(v_slack_s * 100000)

            if n_qual > 0 and qualified_staff_ids:
                v_slack_q = pulp.LpVariable(f"slack_qual_{d.isoformat()}_{idx}", lowBound=0)
                prob += (
                    pulp.lpSum([x_vars[(sid, d, pk)] for sid in qualified_staff_ids for pk in covering_pats])
                    + v_slack_q
                    >= min(target_qual, len(qualified_staff_ids))
                )
                obj_terms.append(v_slack_q * 120000)

    # 制約6: 職員の公平な勤務日数平準化（契約別の目標日数に基づく目的関数）
    for sid in staff_ids:
        contract = context.contracts.get(sid)
        target_holidays = context.monthly_holiday_target
        if contract and hasattr(contract, "monthly_holiday_target") and contract.monthly_holiday_target:
            target_holidays = contract.monthly_holiday_target
        elif contract and getattr(contract, "weekly_work_days", None):
            est_work_days = round(contract.weekly_work_days * len(days) / 7.0)
            target_holidays = max(0, len(days) - est_work_days)

        target_work_days = max(0, len(days) - target_holidays)
        work_diff = pulp.LpVariable(f"diff_{sid}", lowBound=0)
        prob += pulp.lpSum([duty_vars[(sid, d)] for d in days]) - target_work_days <= work_diff
        prob += target_work_days - pulp.lpSum([duty_vars[(sid, d)] for d in days]) <= work_diff
        obj_terms.append(work_diff * 10)

    prob += pulp.lpSum(obj_terms)

    chosen_solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_sec)
    if solver_name in ("HiGHS", "auto") and hasattr(pulp, "HiGHS"):
        try:
            num_threads = min(4, os.cpu_count() or 1)
            chosen_solver = pulp.HiGHS(
                msg=False,
                timeLimit=time_limit_sec,
                threads=num_threads,
                gapRel=0.03,
                mip_heuristic_effort=0.2,
            )
        except Exception:
            pass

    prob.solve(chosen_solver)

    assignments: dict[tuple[str, dt.date], str] = {
        (s.staff_id, d): "OFF" for s in context.staff for d in days
    }
    for sid in staff_ids:
        for d in days:
            assigned = "OFF"
            for p in patterns:
                var = x_vars.get((sid, d, p.key))
                if var and var.varValue and var.varValue > 0.5:
                    assigned = p.key
                    break
            assignments[(sid, d)] = assigned

    return assignments
