"""MILP formulation assembly and high-level shift solver entrypoints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from time import perf_counter

import pulp

from shiftai import config
from shiftai.domain import (
    CellState,
    ChildPlan,
    FacilitySettings,
    ObjectiveWeights,
    PairingRule,
    RequirementTable,
    SlotKind,
    SolveResult,
    SolveStatus,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
)
from shiftai.greedy import (
    solve_shift_greedy,
)
from shiftai.relaxation import (
    RELAX_LEVELS,
    RelaxLevel,
    describe_relaxations,
    normalize_level,
    relaxed_weight_names,
)
from shiftai.shift_patterns import ShiftPattern, normalize_patterns
from shiftai.solver_core.constraints.breaks import (
    _add_breaks,
    _add_consecutive,
    _add_rest,
)
from shiftai.solver_core.constraints.coverage import (
    _add_coverage,
    _add_workload,
    _build_cells,
)
from shiftai.solver_core.constraints.fairness import (
    _add_fairness,
)
from shiftai.solver_core.constraints.pairing import (
    _add_pairing_rules,
)
from shiftai.solver_core.constraints.pattern_align import (
    _add_pattern_alignment,
)
from shiftai.solver_core.constraints.preferences import (
    _add_hours_objective,
    _add_preferences,
)
from shiftai.solver_core.context import (
    _DEFAULT_BREAK_MINUTES,
    _INFEASIBLE_FALLBACK_MESSAGE,
    _SOFT_COVERAGE_MESSAGE,
    _constraint_specs,
    _ModelCtx,
    _var_value,
)
from shiftai.solver_core.decoder import (
    decode_solution,
)
from shiftai.solver_core.diagnosis import (
    _attach_violations,
    _coverage_diagnosis,
)
from shiftai.solver_core.engine import (
    _get_solver,
    apply_warm_start,
)


def _build_problem(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    prefs: Mapping[str, StaffPreferences],
    fixed: Mapping[tuple[str, date, str], CellState],
    settings: FacilitySettings,
    weights: ObjectiveWeights,
    standard: StaffingStandard | None,
    soft_coverage: bool = False,
    patterns: Sequence[ShiftPattern] | None = None,
    relaxation: int = int(RelaxLevel.STRICT),
    drop_groups: frozenset[str] = frozenset(),
    pattern_mode: bool = False,
    pairing_rules: Sequence[PairingRule] | None = None,
) -> _ModelCtx:
    """MILP を構築する。

    ``soft_coverage=True`` のときは配置基準を「不足人数 × shortfall_penalty」の
    罰変数にし、他のハード制約はそのまま保つ（ベストエフォート用）。

    :param patterns: 勤務パターンを指定すると整列ペナルティを加える（``None`` で無効）
    :param relaxation: :class:`~shiftai.relaxation.RelaxLevel` の段階。
        2 以上で出勤日数・休憩・休息時間のペナルティを、
        4 で希望休・休園日・休日勤務不可を無視する。
    :param drop_groups: IIS 診断のため無効化する制約グループ
        （``coverage`` / ``unavailable`` / ``closed`` / ``contract`` /
        ``weekly`` / ``rest`` / ``break`` / ``daily_cap`` / ``fixed`` / ``fairness``）。
        通常運用では空のまま。診断専用なので、戻り値は使わない前提。
    :param pattern_mode: ``True`` のとき現場パターン枠への厳密一致をハード制約とする
    """
    ctx = _ModelCtx(prob=pulp.LpProblem("shift_scheduling", pulp.LpMinimize))
    # 園長・主任（配置対象外）は保育基準の充填に使わない。模型的入口で除外する
    # ことで、配置・休憩・時間拘束・週上限のすべての制約から外れる。
    staff = [member for member in staff if member.is_placeable]
    slots = tuple(requirements.slots)
    if standard is not None:
        kinds = [standard.slot_kind(slot) for slot in slots]
        break_minutes = int(standard.break_minutes)
    else:
        kinds = [SlotKind.NORMAL] * len(slots)
        break_minutes = _DEFAULT_BREAK_MINUTES
    level = normalize_level(relaxation)
    weights = _relaxed_weights(weights, level)
    ignore_unavailable = level >= int(RelaxLevel.IGNORE_UNAVAILABLE)
    if ignore_unavailable:
        prefs = {}
        settings = replace(settings, closed_days=frozenset(), holiday_dates=frozenset())
    obj: list[object] = []
    _build_cells(
        ctx,
        staff,
        requirements,
        prefs,
        fixed,
        settings,
        kinds,
        obj,
        weights,
        ignore_unavailable=ignore_unavailable,
        drop_groups=drop_groups,
        allow_split=level >= int(RelaxLevel.RELAX_HOURS),
    )
    if "coverage" not in drop_groups:
        if soft_coverage or level >= int(RelaxLevel.SOFT_COVERAGE):
            _add_coverage(ctx, staff, requirements, obj, weights, standard)
        else:
            _add_coverage(ctx, staff, requirements, standard=standard)
    _add_workload(ctx, staff, requirements, obj, weights)
    if level < int(RelaxLevel.RELAX_HOURS) and "break" not in drop_groups:
        _add_breaks(ctx, staff, requirements, obj, weights, break_minutes)
    if level < int(RelaxLevel.RELAX_HOURS) and "rest" not in drop_groups:
        _add_rest(ctx, staff, requirements, obj, weights)
    if "weekly" not in drop_groups:
        _add_consecutive(ctx, staff, requirements, obj, weights)
    _add_preferences(ctx, staff, requirements, prefs, kinds, obj, weights)
    _add_hours_objective(ctx, staff, requirements, obj, weights, prefs)
    _add_pattern_alignment(ctx, staff, requirements, obj, weights, patterns or (), pattern_mode=pattern_mode)
    if "fairness" not in drop_groups:
        _add_fairness(ctx, staff, requirements, obj, weights, standard, patterns or ())
    if pairing_rules:
        _add_pairing_rules(ctx, staff, requirements, pairing_rules, obj)
    if obj:
        ctx.prob += pulp.lpSum(obj)
    else:
        ctx.prob += 0
    ctx.capture_specs()
    return ctx


def _relaxed_weights(weights: ObjectiveWeights, level: int) -> ObjectiveWeights:
    """緩和段階に応じて、対象にしたペナルティの重みを 0 にした重みを返す。

    重みを 0 にするとそのソフト制約は「守らないが罰もしない」＝事実上無効になる。
    ハード制約（希望休・契約時間帯・1日の上限時間）は対象外。
    """
    names = relaxed_weight_names(level)
    if not names:
        return weights
    overrides = {name: 0.0 for name in names}
    return replace(weights, **overrides)


@dataclass
class _SolveInput:
    """最適化1回分の入力（2パス目でも同じものを再利用する）。"""

    children: Sequence[ChildPlan]
    staff: list[StaffMember]
    requirements: RequirementTable
    prefs: dict[str, StaffPreferences]
    fixed: dict[tuple[str, date, str], CellState]
    settings: FacilitySettings
    weights: ObjectiveWeights
    standard: StaffingStandard | None
    patterns: tuple[ShiftPattern, ...] = ()
    relaxation: int = int(RelaxLevel.STRICT)
    solver_name: str = config.DEFAULT_SOLVER
    warm_start: bool = True
    warm_start_assignments: Mapping[tuple[str, date, str], CellState] | None = None
    pattern_mode: bool = False
    pairing_rules: tuple[PairingRule, ...] = ()


def solve_shift(
    children: Sequence[ChildPlan],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    weights: ObjectiveWeights | None = None,
    *,
    fixed_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    settings: FacilitySettings | None = None,
    time_limit_sec: int = config.DEFAULT_TIME_LIMIT_SEC,
    msg: bool = False,
    standard: StaffingStandard | None = None,
    patterns: Sequence[ShiftPattern] | None = None,
    relaxation: int = int(RelaxLevel.STRICT),
    drop_groups: frozenset[str] = frozenset(),
    solver_name: str = config.DEFAULT_SOLVER,
    warm_start: bool = True,
    warm_start_assignments: Mapping[tuple[str, date, str], CellState] | None = None,
    pattern_mode: bool = True,
    pairing_rules: Sequence[PairingRule] | None = None,
) -> SolveResult:
    """MILP でシフトを最適化する。配置基準を満たせない場合は自動で2パス目に退避する。

    1パス目は配置基準をハード制約として解く。1パス目が「解なし」だった場合のみ、
    配置基準を「不足人数 × shortfall_penalty」の罰変数に置き換えた2パス目で
    再最適化する。ハード制約（希望休・契約時間帯・1日上限・在勤ブロック・休息時間）は
    そのまま保つので、法令・契約に反するシフトは生成されない。貪欲法は2パス目にも
    解がない場合の最終保険。

    :param children: 園児の登降園予定（統計目的にのみ使用する）
    :param staff: 職員一覧
    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param preferences: 職員IDごとの個人希望
    :param weights: 目的関数の重み
    :param fixed_assignments: (職員ID, 日付, "HH:MM-HH:MM") -> CellState の手動確定セル
    :param settings: 園設定（休園日など）
    :param time_limit_sec: ソルバの実行時間上限（秒）
    :param msg: ソルバログを表示するか
    :param standard: 早朝・延長の時間帯区分を判定するための基準（省略時は全て NORMAL）
    :param patterns: 早番・日勤・遅番などの勤務パターン。
        指定すると勤務ブロックの境界をパターンへ引き寄せる（ソフトペナルティ）。
    :param relaxation: :class:`~shiftai.relaxation.RelaxLevel` の段階。
        ``1`` 以上なら 1 パス目から配置基準を罰変数化するため、
        「ハード制約では解なし」と証明される時間を短縮できる。
    :param drop_groups: IIS 診断用に無効化する制約グループ（通常は空のまま）
    :returns: 最適/実行可能/部分的なシフト、違反情報、所要時間などの統計
    """
    started = perf_counter()
    level = normalize_level(relaxation)
    payload = _SolveInput(
        children=list(children),
        staff=list(staff),
        requirements=requirements,
        prefs=dict(preferences or {}),
        fixed=dict(fixed_assignments or {}),
        settings=settings or FacilitySettings(),
        weights=weights or ObjectiveWeights(),
        standard=standard,
        patterns=normalize_patterns(patterns),
        relaxation=level,
        solver_name=solver_name,
        warm_start=warm_start,
        warm_start_assignments=warm_start_assignments,
        pattern_mode=pattern_mode,
        pairing_rules=tuple(pairing_rules or ()),
    )
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    budget = max(2, int(time_limit_sec))
    stats: dict = {
        "solver": payload.solver_name,
        "solver_status": "",
        "solver_status_pass1": "",
        "pass": 1,
        "relaxed": False,
        "elapsed_sec": 0.0,
        "num_variables": 0,
        "num_constraints": 0,
        "num_staff": sum(1 for s in payload.staff if s.is_placeable),
        "num_children": len(children),
        "num_days": len(days),
        "num_slots": len(slots),
        "relaxation": level,
        "relaxation_label": RELAX_LEVELS[level].label,
        "relaxed_constraints": list(describe_relaxations(level)),
        "patterns": [p.label for p in payload.patterns],
        "pattern_mode": payload.pattern_mode,
        "warm_start": payload.warm_start,
    }

    if not payload.staff or not days or not slots:
        return SolveResult(
            status=SolveStatus.ERROR,
            messages=["職員・対象日・時間帯のいずれかが空です。入力条件を確認してください。"],
            stats=stats,
        )

    try:
        ctx = _build_problem(
            payload.staff,
            requirements,
            payload.prefs,
            payload.fixed,
            payload.settings,
            payload.weights,
            payload.standard,
            patterns=payload.patterns,
            relaxation=payload.relaxation,
            drop_groups=drop_groups,
            pattern_mode=payload.pattern_mode,
            pairing_rules=payload.pairing_rules,
        )
    except Exception as exc:
        stats["elapsed_sec"] = round(perf_counter() - started, 3)
        stats["solver_status"] = "ModelError"
        return _greedy(payload, stats, started, f"モデル構築に失敗しました: {exc}")

    stats["num_variables"], stats["num_constraints"] = ctx.counts()
    if drop_groups:
        # IIS 診断用の実行なので、貪欲法へ落ちたら「外しても解なし」と解釈する。
        if ctx.conflicts:
            stats["solver_status"] = "PrecheckInfeasible"
            return SolveResult(
                status=SolveStatus.INFEASIBLE,
                messages=list(ctx.conflicts),
                stats=stats,
            )
        status, raw = _feasibility_status(ctx, max(1, budget), msg, solver_name=payload.solver_name)
        stats["solver_status"] = raw
        return SolveResult(
            status=status,
            messages=[raw],
            stats=stats,
        )

    stats["num_variables"], stats["num_constraints"] = ctx.counts()
    if ctx.conflicts:
        stats["solver_status_pass1"] = "PrecheckInfeasible"
        return _best_effort(payload, stats, started, budget, "PrecheckInfeasible")

    if payload.relaxation >= int(RelaxLevel.SOFT_COVERAGE):
        # 配置基準が既に罰変数化されている段階では 1 パス目を飛ばす
        # （解なしを証明する時間を丸ごと節約できる）。
        stats["solver_status_pass1"] = f"Skipped（緩和 L{payload.relaxation}）"
        return _best_effort(payload, stats, started, budget, "Skipped")

    # ウォームスタート（初期解投入）
    if payload.warm_start:
        warm_assignments = payload.warm_start_assignments
        if warm_assignments is None:
            try:
                g_res = solve_shift_greedy(
                    children=payload.children,
                    staff=payload.staff,
                    requirements=requirements,
                    preferences=payload.prefs,
                    settings=payload.settings,
                    standard=payload.standard,
                )
                warm_assignments = g_res.assignments
            except Exception:
                warm_assignments = None
        if warm_assignments:
            applied_count = apply_warm_start(ctx, slots, warm_assignments)
            stats["warm_start_applied"] = applied_count > 0
            stats["warm_start_vars"] = applied_count

    status, raw, actual_solver = _run_solver(
        ctx,
        max(1, int(budget * 0.5)),
        msg,
        solver_name=payload.solver_name,
        warm_start=payload.warm_start and bool(stats.get("warm_start_applied", False)),
    )
    stats["solver"] = actual_solver
    stats["solver_status_pass1"] = raw
    if status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
        return _finish(ctx, payload, stats, started, status, pass_no=1, relaxed=False)
    stats["solver_status"] = raw
    return _best_effort(payload, stats, started, budget, raw)


def _run_solver(
    ctx: _ModelCtx,
    limit_sec: int,
    msg: bool,
    solver_name: str = "auto",
    warm_start: bool = False,
) -> tuple[SolveStatus, str, str]:
    """MILP ソルバー（HiGHS または CBC）を実行して SolveStatus とステータス文字列、ソルバー名を返す。"""
    solver_instance, actual_solver = _get_solver(
        limit_sec=limit_sec,
        msg=msg,
        solver_name=solver_name,
        warm_start=warm_start,
    )
    try:
        ctx.prob.solve(solver_instance)
    except Exception as exc:
        return SolveStatus.ERROR, f"SolveError / {exc}", actual_solver
    status_raw = pulp.LpStatus.get(ctx.prob.status, "Undefined")
    sol_raw = pulp.LpSolution.get(ctx.prob.sol_status, "No Solution Found")
    raw = f"{status_raw} / {sol_raw}"
    if status_raw == "Infeasible":
        return SolveStatus.INFEASIBLE, raw, actual_solver
    if status_raw == "Unbounded":
        return SolveStatus.ERROR, raw, actual_solver
    if not _has_solution(ctx):
        return SolveStatus.INFEASIBLE, raw, actual_solver
    unassigned = _unassigned_variables(ctx)
    if unassigned:
        raw = f"{raw}（{unassigned} 変数の値なし＝解未読込）"
        return SolveStatus.INFEASIBLE, raw, actual_solver
    if _non_integral_variables(ctx):
        # 時間切れでソルバーが LP 緩和値（0.19 や 0.83 のような途中値）を書き戻すことがある。
        # それを丸めてハード制約を満たすなら正しい MILP 解なので採用し、
        # 満たさないなら「解なし」として 2 パス目（罰変数化）に退避する。
        _round_to_integral(ctx)
        if _non_integral_variables(ctx):
            return SolveStatus.INFEASIBLE, raw, actual_solver
        raw = f"{raw}（途中値を0/1に丸めて採用）"
    violated = verify_solution(ctx)
    if violated:
        raw = f"{raw}（制約違反 {len(violated)} 本: {violated[0]}）"
        return SolveStatus.INFEASIBLE, raw, actual_solver
    if status_raw == "Optimal" and sol_raw == "Optimal Solution Found":
        return SolveStatus.OPTIMAL, raw, actual_solver
    if status_raw == "Optimal":
        return SolveStatus.FEASIBLE, raw, actual_solver
    return SolveStatus.FEASIBLE, raw, actual_solver


def _run_cbc(ctx: _ModelCtx, limit_sec: int, msg: bool) -> tuple[SolveStatus, str]:
    """後方互換用: CBC を実行して SolveStatus と生のステータス文字列を返す。"""
    status, raw, _ = _run_solver(ctx, limit_sec, msg, solver_name="PULP_CBC_CMD", warm_start=False)
    return status, raw


def _has_solution(ctx: _ModelCtx) -> bool:
    """求まった解に勤務セルが1つでも含まれるか。"""
    return any(_var_value(v) > 0.5 for v in ctx.work.values() if not isinstance(v, int | float))


def _feasibility_status(
    ctx: _ModelCtx,
    limit_sec: int,
    msg: bool,
    solver_name: str = "auto",
) -> tuple[SolveStatus, str]:
    """IIS 診断用: 「解が求まるか」だけを見る。

    :func:`_run_solver` は勤務セルが 1 つでも入っていないと「解なし」と見なす。
    制約グループを外したモデルでは「全員オフ」が最適解になりうるので、
    診断の判定にはソルバーの報告だけを参考にする。 Assignments の有無は問わない。
    """
    solver_instance, _ = _get_solver(limit_sec, msg, solver_name=solver_name)
    try:
        ctx.prob.solve(solver_instance)
    except Exception as exc:
        return SolveStatus.ERROR, f"SolveError / {exc}"
    status_raw = pulp.LpStatus.get(ctx.prob.status, "Undefined")
    sol_raw = pulp.LpSolution.get(ctx.prob.sol_status, "No Solution Found")
    raw = f"{status_raw} / {sol_raw}"
    if status_raw in ("Optimal", "Not Solved") and sol_raw in (
        "Optimal Solution Found",
        "Solution Found",
    ):
        return SolveStatus.OPTIMAL, raw
    if status_raw == "Infeasible" or sol_raw == "No Solution Found":
        return SolveStatus.INFEASIBLE, raw
    return SolveStatus.FEASIBLE, raw


def verify_solution(ctx: _ModelCtx, tol: float = 1e-4) -> list[str]:
    """返ってきた解がすべてのハード制約を満たしているかを確認する。

    CBC は「解なし」と報告した場合にも .solu ファイルへ途中の値を書き残すことがある
    ため、PuLP が Optimal/Feasible と報告しても制約違反した解が読み込まれることがある。
    UI には法令違反を含んだ解を出さないため、実際に値を評価して検証する。

    検査は ``_constraint_specs`` が保持する数値スナップショットで行う。
    ``LpConstraint.value()`` に依存しないのは、同メソッドが変数値未決時に
    ``None``（判定不能）を返し、例外を投げる版も存在するためである。

    **この関数の責務は「数値的に確定したハード制約違反」の列挙だけ**である。
    変数値が ``None``（``.solu`` が読めていない / 途中解）の制約は
    **違反として数えない**。``None`` を 0 とみなすと配置基準の定数項
    （``expr >= need`` → ``-need``）が未達と判定され全制約が偽陽性になり、
    違反とみなすと判定不能な状態から 300 件超の偽の違反が出る。どちらも採らない。

    「解がそもそも読み込まれたか」は**別の関数**が判定する:
    ``_unassigned_variables`` が 1 個でも ``None`` を数え、``_run_cbc`` が
    ``verify_solution`` を呼ぶ**前に**確認して ``INFEASIBLE`` を返す。
    この 2 段構えが「CBC の途中解が『制約を満たす解』として採用される」ことを防ぐ。
    ``verify_solution`` 単体を呼ぶときはこの前提に注意すること。

    :returns: 違反した制約名のリスト（空なら「違反は見つからなかった」）。
              判定不能な制約は含まれない
    """
    return [spec.name for spec in _constraint_specs(ctx) if spec.is_violated(tol)]


def _unassigned_variables(ctx: _ModelCtx) -> int:
    """値が入っていない変数の個数（解が読み込まれていない判定用）。"""
    return sum(1 for v in ctx.prob.variables() if v.varValue is None)


def _non_integral_variables(ctx: _ModelCtx, tol: float = 1e-4) -> int:
    """0/1 になっていない整数変数の個数（CBC の途中値を検出するため）。"""
    count = 0
    for var in ctx.prob.variables():
        if var.cat != pulp.LpInteger:
            continue
        raw = var.varValue
        if raw is None or abs(float(raw) - round(float(raw))) > tol:
            count += 1
    return count


def _round_to_integral(ctx: _ModelCtx, tol: float = 1e-4) -> None:
    """整数変数を 0/1 に丸める（丸めた後にハード制約を検証し直す）。"""
    for var in ctx.prob.variables():
        if var.cat != pulp.LpInteger or var.varValue is None:
            continue
        rounded = float(round(float(var.varValue)))
        if abs(float(var.varValue) - rounded) > tol:
            var.varValue = rounded


def _best_effort(
    payload: _SolveInput,
    stats: dict,
    started: float,
    budget: int,
    pass1_status: str,
) -> SolveResult:
    """配置基準を罰変数に置き換えた2パス目（ベストエフォート）を実行する。"""
    diagnosis = _coverage_diagnosis(payload)
    try:
        soft = _build_problem(
            payload.staff,
            payload.requirements,
            payload.prefs,
            payload.fixed,
            payload.settings,
            payload.weights,
            payload.standard,
            soft_coverage=True,
            patterns=payload.patterns,
            relaxation=payload.relaxation,
            pattern_mode=payload.pattern_mode,
            pairing_rules=payload.pairing_rules,
        )
    except Exception:
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)

    if payload.warm_start and payload.warm_start_assignments:
        apply_warm_start(soft, tuple(payload.requirements.slots), payload.warm_start_assignments)

    left = budget - (perf_counter() - started)
    status, raw, actual_solver = _run_solver(
        soft,
        max(1, int(left)),
        False,
        solver_name=payload.solver_name,
        warm_start=payload.warm_start and bool(payload.warm_start_assignments),
    )
    stats["solver"] = actual_solver
    stats["solver_status"] = f"{raw}（配置基準を罰変数化）"
    if status in (SolveStatus.INFEASIBLE, SolveStatus.ERROR):
        return _greedy(payload, stats, started, _INFEASIBLE_FALLBACK_MESSAGE, diagnosis)
    stats["num_variables"], stats["num_constraints"] = soft.counts()
    result = _finish(soft, payload, stats, started, status, pass_no=2, relaxed=True)
    # この分岐に来るのは **貪欲法の結果ではない**。
    # ``status`` が INFEASIBLE / ERROR なら上の行で既に ``_greedy`` に
    # 分岐しているため、ここに到達した結果は必ず MILP 2 パス目の解である。
    # 修正前: 貪欲法のメッセージを無条件に先頭に置いていたため、
    # 「貪欲法による暫定シフトを生成しました」と
    # 「最適解を求めました」が同じ結果に対して同時に表示され、
    # 利用者が結果全体を信用できなくなる状態になっていた。
    result.messages = [_SOFT_COVERAGE_MESSAGE, *diagnosis, *result.messages]
    return result


def _greedy(
    payload: _SolveInput,
    stats: dict,
    started: float,
    message: str,
    diagnosis: list[str] | None = None,
) -> SolveResult:
    """貪欲法による最終保険。"""
    result = solve_shift_greedy(
        payload.children,
        payload.staff,
        payload.requirements,
        payload.prefs,
        fixed_assignments=payload.fixed,
        standard=payload.standard,
        settings=payload.settings,
    )
    result.messages = [message, *(diagnosis or []), *result.messages]
    result.stats.update({k: v for k, v in stats.items() if k != "solver"})
    result.stats["solver"] = "PULP_CBC_CMD"
    result.stats["fallback"] = "greedy"
    return result


def _finish(
    ctx: _ModelCtx,
    payload: _SolveInput,
    stats: dict,
    started: float,
    status: SolveStatus,
    *,
    pass_no: int,
    relaxed: bool,
) -> SolveResult:
    """解をデコードして SolveResult にまとめる。"""
    elapsed = perf_counter() - started
    if not stats.get("solver_status"):
        stats["solver_status"] = stats.get("solver_status_pass1", "")
    stats["elapsed_sec"] = round(elapsed, 3)
    stats["pass"] = pass_no
    stats["relaxed"] = relaxed
    requirements = payload.requirements
    days = requirements.all_days()
    slots = tuple(requirements.slots)
    shift_days, assignments = decode_solution(ctx, payload.staff, days, slots)
    objective = pulp.value(ctx.prob.objective)
    bad = sorted(
        (
            (_var_value(gap), need, day, slot.label, label)
            for gap, need, day, slot, label in ctx.shortfall.values()
        ),
        reverse=True,
    )
    bad = [row for row in bad if row[0] > 0.5]
    if bad:
        status = SolveStatus.PARTIAL
    result = SolveResult(
        status=status,
        shift_days=shift_days,
        assignments=assignments,
        objective_value=float(objective) if objective is not None else None,
        messages=_solution_messages(status, objective, elapsed, relaxed, bad, stats),
        stats=stats,
    )
    _attach_violations(
        result, requirements, payload.staff, payload.prefs, payload.standard, payload.settings
    )
    return result


def _solution_messages(
    status: SolveStatus,
    objective: float | None,
    elapsed: float,
    relaxed: bool,
    bad: list[tuple],
    stats: dict,
) -> list[str]:
    """日本語のサマリーメッセージを組み立てる。"""
    obj = f"{objective:.2f}" if objective is not None else "算出不可"
    if status is SolveStatus.OPTIMAL:
        head = f"最適解を求めました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    elif status is SolveStatus.FEASIBLE:
        head = f"時間制限内で実行可能解を得ました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    elif status is SolveStatus.PARTIAL:
        head = (
            f"配置基準を満たしきれませんでした。可能な限り充填した暫定シフトです"
            f"（目的関数={obj}、所要{elapsed:.2f}秒）。"
        )
    else:
        head = f"シフトを生成しました（目的関数={obj}、所要{elapsed:.2f}秒）。"
    out = [head]
    if relaxed:
        out.append(
            "配置基準を罰変数に置き換えた2パス目（ベストエフォート）で最適化しました。"
            f"1パス目の結果: {stats.get('solver_status_pass1', '-')}"
        )
    out.extend(str(m) for m in stats.get("relaxed_constraints", ()))
    if bad:
        total = sum(row[0] for row in bad)
        worst = bad[0]
        out.append(
            f"配置不足は {len(bad)} 時間帯・合計{total:.0f} 名分です。"
            f"最大は {worst[2].isoformat()} {worst[3]} の{worst[4]}が {worst[0]:.0f} 名不足"
            f"（必要 {worst[1]:.0f} 名）です。"
        )
    return out


