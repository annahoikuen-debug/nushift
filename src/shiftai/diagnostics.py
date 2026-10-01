"""Infeasible（解なし）時の原因診断（Slack / IIS）と緩和ラダー。

**このモジュールが解決する問題**

配置基準を満たせないとき、素朴な実装は「解なし → 貪欲法に落ちる」だけで終わる。
現場から見ると「動かない／使えない」だけで、**なぜ最適化できなかったのか**が分からない。

そこで 2 つの視点を用意する。

1. **Slack 相当の構造診断**（CBC を起動しない / 即座に計算できる）
   :func:`shortfall_rows` は「どの日のどの時間帯で、人員が何名不足したか」を返す。
   さらに :func:`diagnose` はそれを

   * 人員不足（人数そのものが足りない）
   * 保育士不足（人数は足りるが保育士が足りない）
   * 総人時不足（1 週間全体で足りない）
   * 契約・休園日の衝突（その時間帯に誰も出られない）

   へ分類し、「何を変えれば足りるか」まで日本語で示す。

2. **IIS（Irreducible Infeasible Subsystem）相当の絞り込み**
   制約グループを 1 つずつ外して解き直し、
   「外したら解ける」＝そのグループが矛盾の当事者であることを示す。
   CBC を 1 回ずつ起動するため重く、UI では明示的なボタン操作でのみ呼ぶ。

**緩和ラダー**

:func:`relaxation_ladder` は :mod:`shiftai.relaxation` の段階を 0 から順に試し、
「どの段階で初めて解けたか」を返す。これで「何を変えれば解けるのか」が
数値で示せるため、現場の意思決定（増員／保育時間の見直し／希望休の調整）に使える。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from time import perf_counter

from shiftai.domain import (
    FacilitySettings,
    RequirementTable,
    SolveResult,
    SolveStatus,
    StaffMember,
    StaffPreferences,
)
from shiftai.relaxation import RELAX_LEVELS, RelaxLevel, normalize_level
from shiftai.solver import SupplyGap, shortfall_rows, solve_shift, supply_hours

__all__ = [
    "CONSTRAINT_GROUPS",
    "ConstraintGroup",
    "DiagnosisReason",
    "InfeasibilityReport",
    "LadderOutcome",
    "RelaxationLadder",
    "conflict_core_report",
    "diagnose",
    "find_conflict_core",
    "relaxation_ladder",
    "shortfall_dataframe",
    "solver_feasibility_probe",
]


# ---------------------------------------------------------------------------
# 制約グループ（IIS 絞り込みの候補）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConstraintGroup:
    """矛盾の当事者になりうる制約グループ 1 つ。"""

    key: str
    label: str
    detail: str


CONSTRAINT_GROUPS: tuple[ConstraintGroup, ...] = (
    ConstraintGroup(
        "contract",
        "契約時間帯（最早始業〜最遅終業）",
        "開所時刻と重なる職員を増やす。パートの始業を早める。",
    ),
    ConstraintGroup(
        "unavailable",
        "希望休・不在時間帯",
        "希望休を別の日へ移す。休みたい日を確認して見直す。",
    ),
    ConstraintGroup(
        "closed",
        "休園日・休日の出勤不可",
        "休園日の設定と「休日勤務可」の指定を見直す。",
    ),
    ConstraintGroup(
        "daily_cap",
        "1日の勤務上限時間",
        "法定 10 時間は超えられない。劳动合同時間を引き上げるには契約変更が要る。",
    ),
    ConstraintGroup(
        "weekly",
        "週所定出勤日数・最大連続勤務日数",
        "最低出勤日数を下げる。シフト目標を見直す。",
    ),
    ConstraintGroup(
        "rest",
        "勤務間の休息時間",
        "勤務ブロックの間隔を見直す。前後の日との組み合わせを組み替える。",
    ),
    ConstraintGroup(
        "break",
        "休憩ブロックの連続性",
        "休憩の分割ペナルティを外す。休憩の取り方を現場に合わせる。",
    ),
    ConstraintGroup(
        "fixed",
        "手動で確定したセル",
        "手動確定を解除すると解ける可能性がある。",
    ),
)
"""削除フィルタで調べる制約グループ。

**配置基準（``coverage``）はここに含めない。** 配置基準を外すと「全員オフ」が
最適解になり、どの入力でも必ず実行可能になるため、無条件に「配置基準が原因」と
出てしまう。配置基準を満たせるかどうかは Slack 相当の構造診断
（:func:`diagnose` / :func:`~shiftai.solver.shortfall_rows`）で判定する。
"""


# ---------------------------------------------------------------------------
# 構造診断
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiagnosisReason:
    """原因 1 種。"""

    code: str
    label: str
    message: str
    advice: str

    def to_dict(self) -> dict[str, str]:
        return {"区分": self.label, "状況": self.message, "対応": self.advice}


@dataclass(frozen=True)
class InfeasibilityReport:
    """構造診断の結果。"""

    rows: tuple[SupplyGap, ...] = ()
    reasons: tuple[DiagnosisReason, ...] = ()
    need_hours: float = 0.0
    supply_hours: float = 0.0
    structural_conflicts: tuple[str, ...] = ()
    eligible_staff: int = 0
    note: str = ""

    @property
    def has_gap(self) -> bool:
        return bool(self.rows) or bool(self.reasons)

    @property
    def total_gap(self) -> int:
        """不足の合計（配置基準を罰変数に落としたときの不足量と同じ単位）。"""
        return sum(g.gap for g in self.rows)

    @property
    def worst(self) -> SupplyGap | None:
        return self.rows[0] if self.rows else None

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(sorted({g.day for g in self.rows}))

    @property
    def ratio(self) -> float | None:
        """必要人時 / 配置可能人時。供給が 0 のときは ``None``（0 除算を避ける）。"""
        if self.supply_hours <= 0:
            return None
        return self.need_hours / self.supply_hours

    def headline(self) -> str:
        """結果を 1 文で表す。"""
        if self.structural_conflicts:
            return self.structural_conflicts[0]
        if not self.rows:
            return self.note or "構造的には不足は検出されませんでした。"
        worst = self.rows[0]
        ratio = self.ratio
        tail = ""
        if ratio is not None and ratio > 1.0:
            tail = f"（必要人時は配置可能人時の {ratio:.2f} 倍）"
        return (
            f"需要 > 供給 の時間帯が {len(self.rows)} 件あります。"
            f"最大は {worst.day.isoformat()} {worst.slot.label} の不足 {worst.gap} 名{tail}"
        )

    def messages(self, top: int = 5) -> list[str]:
        """UI / CLI に出す日本語メッセージの列。"""
        out = [self.headline()]
        for row in self.rows[:top]:
            out.append(f"  ・{row.describe()}")
        if len(self.rows) > top:
            out.append(f"  ・ほか {len(self.rows) - top} 時間帯")
        for reason in self.reasons:
            out.append(f"  ・{reason.label}: {reason.message} / 対応: {reason.advice}")
        for conflict in self.structural_conflicts:
            out.append(f"  ・{conflict}")
        return out


def shortfall_dataframe(report: InfeasibilityReport) -> object:
    """診断結果を DataFrame にして返す（UI の表描画用）。

    ``pandas`` を実際に import しなくてもよいよう、遅延 import する。
    """
    import pandas as pd

    records = [
        {
            "日付": g.day.isoformat(),
            "曜日": g.weekday,
            "時間帯": g.slot.label,
            "必要人員": g.need_staff,
            "必要保育士": g.need_qualified,
            "供給人員": g.supply_staff,
            "供給保育士": g.supply_qualified,
            "不足人員": g.gap_staff,
            "不足保育士": g.gap_qualified,
            "補足": f"支援員のみ {g.supply_support} 名",
        }
        for g in report.rows
    ]
    columns = [
        "日付",
        "曜日",
        "時間帯",
        "必要人員",
        "必要保育士",
        "供給人員",
        "供給保育士",
        "不足人員",
        "不足保育士",
        "補足",
    ]
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame.from_records(records, columns=columns)


def diagnose(
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
    *,
    solver_conflicts: Sequence[str] = (),
) -> InfeasibilityReport:
    """「なぜ配置基準を満たせないのか」を構造的に診断する（CBC は起動しない）。

    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param staff: 職員一覧
    :param preferences: 職員IDごとの個人希望
    :param settings: 園設定
    :param solver_conflicts: モデル構築時に検出した構造的矛盾（``solver`` が持つ文字列列）
    :returns: :class:`InfeasibilityReport`
    """
    fac = settings or FacilitySettings()
    rows = shortfall_rows(staff, requirements, preferences, fac)
    need_h = requirements.total_needed_hours()
    supply_h = supply_hours(staff, requirements, preferences, fac)
    eligible = sum(1 for m in staff if m.is_placeable)
    reasons: list[DiagnosisReason] = []

    staff_gaps = [g for g in rows if g.gap_staff > 0]
    qualified_gaps = [g for g in rows if g.gap_qualified > 0 and g.gap_staff == 0]
    if staff_gaps:
        worst = staff_gaps[0]
        reasons.append(
            DiagnosisReason(
                "STAFF_SHORT",
                "人員不足",
                f"{len(staff_gaps)} 時間帯で人数そのものが不足しています"
                f"（最大 {worst.day.isoformat()} {worst.slot.label} で {worst.gap_staff} 名）。",
                "職員の増員、登降園時間の調整、延長保育の縮小をご検討ください。",
            )
        )
    if qualified_gaps:
        worst = qualified_gaps[0]
        reasons.append(
            DiagnosisReason(
                "QUALIFIED_SHORT",
                "保育士不足",
                f"{len(qualified_gaps)} 時間帯で人数は足りるが保育士が足りません"
                f"（最大 {worst.day.isoformat()} {worst.slot.label} で {worst.gap_qualified} 名）。",
                "保育士の増員、または自治体の基準が認める代替措置（延長保育の緩和措置等）の"
                "利用をご検討ください。",
            )
        )
    if supply_h <= 0:
        reasons.append(
            DiagnosisReason(
                "NO_SUPPLY",
                "配置可能人時 0",
                "契約時間帯・希望休のどちらにも該当しない職員しかおらず、配置可能人時が 0 です。",
                "職員の契約時間帯（最早始業・最遅終業）と園の開所・閉所時間の重なりを確認してください。",
            )
        )
    elif need_h > supply_h:
        ratio = need_h / supply_h
        reasons.append(
            DiagnosisReason(
                "HOURS_SHORT",
                "総人時不足",
                f"必要人員合計 {need_h:.1f} 人時に対し、配置可能人時は {supply_h:.1f} 人時"
                f"（{ratio:.2f} 倍）です。",
                "1 週間全体で人員が足りません。職員数の増員か、保育時間の見直しが必要です。",
            )
        )
    if not rows and not solver_conflicts and not reasons:
        note = (
            "時間帯ごとの供給は足っています。休園日・休日の勤務不可・週所定出勤日数・"
            "最低休息時間などの制約が決め手になっている可能性があります。"
            "制約グループ別の絞り込み（IIS）で行いを確認してください。"
        )
        return InfeasibilityReport(
            rows=(),
            reasons=(),
            need_hours=need_h,
            supply_hours=supply_h,
            structural_conflicts=tuple(solver_conflicts),
            eligible_staff=eligible,
            note=note,
        )
    return InfeasibilityReport(
        rows=rows,
        reasons=tuple(reasons),
        need_hours=need_h,
        supply_hours=supply_h,
        structural_conflicts=tuple(solver_conflicts),
        eligible_staff=eligible,
    )


# ---------------------------------------------------------------------------
# IIS 相当の絞り込み
# ---------------------------------------------------------------------------


def find_conflict_core(
    feasible_without: Callable[[str], bool],
    groups: Sequence[ConstraintGroup] = CONSTRAINT_GROUPS,
) -> tuple[ConstraintGroup, ...]:
    """削除フィルタで「矛盾の当事者」を絞り込む（IIS 相当）。

    制約グループを順に 1 つだけ外して実行可能性を確認し、
    **外しても解けないもの**（＝外してもなお矛盾している＝何も変わっていない）を捨て、
    残ったグループだけ戻す。残ったものは「最終的に外されたことで初めて解ける」つまり
    矛盾の当事者である。

    **最後の 1 つは外せない**（外すと検証するものがなくなる）。
    したがって「外しても解けない」入力でも結果は空ではなく最後の 1 件が残る。
    それは「少なくともこれが矛盾に関与している」ことを意味する。

    :param feasible_without: 制約グループキーを 1 つ受け取り、その制約を外したときが実行可能なら ``True`` を返す関数。CBC を起動する実装を渡す想定。
    :param groups: 調べる制約グループ
    :returns: 矛盾の当事者となったグループ（元の順序）
    """
    kept: list[ConstraintGroup] = list(groups)
    for group in groups:
        remaining = [g for g in kept if g.key != group.key]
        if not remaining:
            continue
        if not feasible_without(group.key):
            kept = remaining
    return tuple(kept)


def solver_feasibility_probe(
    children: Sequence[object],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    *,
    settings: FacilitySettings | None = None,
    standard: object | None = None,
    time_limit_sec: int = 10,
) -> Callable[[str], bool]:
    """:func:`find_conflict_core` に渡す、ソルバ実行の判定関数を作る。

    **副作用として、呼ばれるたびに CBC を 1 回起動する。** したがって UI では
    利用者が明示的にボタンを押したときだけ生成すること。

    :returns: 制約グループキーを受け取り、その制約を外した时被が
        実行可能なら ``True`` を返す関数
    """
    cache: dict[str, bool] = {}

    def feasible_without(key: str) -> bool:
        if key not in cache:
            probe = solve_shift(
                children,
                staff,
                requirements,
                preferences,
                None,
                settings=settings,
                time_limit_sec=time_limit_sec,
                standard=standard,  # type: ignore[arg-type]
                drop_groups=frozenset({key}),
            )
            cache[key] = probe.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        return cache[key]

    return feasible_without


def conflict_core_report(
    children: Sequence[object],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    *,
    settings: FacilitySettings | None = None,
    standard: object | None = None,
    time_limit_sec: int = 10,
) -> tuple[ConstraintGroup, ...]:
    """実際のソルバを使って矛盾の当事者を絞り込む（IIS）。

    制約グループ数と同じ回数だけ CBC を起動するため数分かかる可能性がある。
    """
    probe = solver_feasibility_probe(
        children,
        staff,
        requirements,
        preferences,
        settings=settings,
        standard=standard,
        time_limit_sec=time_limit_sec,
    )
    return find_conflict_core(probe)


# ---------------------------------------------------------------------------
# 緩和ラダー
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderOutcome:
    """緩和ラダーの 1 段の結果。"""

    level: int
    label: str
    status: SolveStatus
    elapsed_sec: float
    gap_slots: int
    solved: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "段階": f"L{self.level} {self.label}",
            "結果": self.status.value,
            "不足時間帯": self.gap_slots,
            "所要秒": round(self.elapsed_sec, 2),
        }


@dataclass
class RelaxationLadder:
    """段階を順に試した結果。"""

    outcomes: tuple[LadderOutcome, ...] = field(default_factory=tuple)

    @property
    def first_solved(self) -> LadderOutcome | None:
        for outcome in self.outcomes:
            if outcome.solved:
                return outcome
        return None

    @property
    def best(self) -> LadderOutcome | None:
        return self.outcomes[-1] if self.outcomes else None

    def messages(self) -> list[str]:
        """UI / CLI に出す日本語メッセージの列。"""
        if not self.outcomes:
            return ["緩和ラダーを実行しませんでした。"]
        out = ["【緩和モードの検証結果（緩めるほど強い順に試行）】"]
        for o in self.outcomes:
            mark = " 将来" if o.solved else "     "
            out.append(
                f"{mark} L{o.level} {o.label}: {o.status.value}"
                f"／不足時間帯 {o.gap_slots} 件／{o.elapsed_sec:.1f} 秒"
            )
        first = self.first_solved
        if first is None:
            out.append(
                "どの段階でもハード制約のままは解けませんでした。"
                "職員数・保育時間そのものを見直す必要があります。"
            )
        else:
            out.append(
                f"L{first.level}（{first.label}）で初めて解けました。"
                "それより弱い緩和で足りた場合は、その一段だけで十分です。"
            )
        return out


def relaxation_ladder(
    children: Sequence[object],
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    *,
    settings: FacilitySettings | None = None,
    standard: object | None = None,
    max_level: int | None = None,
    time_limit_sec: int = 15,
) -> RelaxationLadder:
    """「どこまで緩めれば解けるか」を段階ごとに実測する。

    **副作用として CBC を ``max_level + 1`` 回起動する。** したがって UI では
    利用者が明示的にボタンを押したときだけ呼ぶこと（自動呼び出しはしない）。

    :param children: 園児の登降園予定
    :param staff: 職員一覧
    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param preferences: 職員IDごとの個人希望
    :param settings: 園設定
    :param standard: 早朝・延長の判定に必要な基準
    :param max_level: 最大試行する段階（既定は定義済みの最大）
    :param time_limit_sec: 各段階でソルバに与える時間上限（秒）
    :returns: :class:`RelaxationLadder`
    """
    top = normalize_level(max_level)
    budget = max(2, int(time_limit_sec))
    outcomes: list[LadderOutcome] = []
    for level in range(top + 1):
        spec = RELAX_LEVELS[level]
        started = perf_counter()
        result: SolveResult = solve_shift(
            children,
            staff,
            requirements,
            preferences,
            None,
            settings=settings,
            time_limit_sec=budget,
            standard=standard,  # type: ignore[arg-type]
            relaxation=level,
        )
        gaps = len(
            [
                g
                for g in shortfall_rows(staff, requirements, preferences, settings)
            ]
        )
        solved = result.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
        outcomes.append(
            LadderOutcome(
                level=level,
                label=spec.label,
                status=result.status,
                elapsed_sec=perf_counter() - started,
                gap_slots=gaps,
                solved=solved,
            )
        )
        if solved and level >= int(RelaxLevel.SOFT_COVERAGE):
            break
    return RelaxationLadder(tuple(outcomes))
