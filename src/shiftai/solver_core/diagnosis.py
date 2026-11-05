"""Supply gap analysis and staffing diagnostics for solver outputs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shiftai.solver import _SolveInput
from dataclasses import dataclass
from datetime import date

from shiftai.domain import (
    FacilitySettings,
    RequirementTable,
    Role,
    Slot,
    SolveResult,
    StaffingStandard,
    StaffMember,
    StaffPreferences,
    Violation,
    ViolationSeverity,
    japanese_weekday,
    qualified_count,
    weekly_periods,
)
from shiftai.solver_core.context import (
    _daily_cap_minutes,
    _day_is_workable,
    _is_sei,
    _is_unavailable,
    _slot_is_contractible,
)


def _coverage_diagnosis(payload: _SolveInput, top: int = 5) -> list[str]:
    """配置基準を満たせない原因を日本語で説明する（需要と供給の不足を提示する）。"""
    rows = _shortfall_supply(payload)
    out: list[str] = []
    if rows:
        out.append(
            f"配置基準を満たせませんでした。需要 > 供給 となる時間帯は {len(rows)} 件です。"
            f"不足の大きい順に上位{top}件を示します"
            "（供給＝契約時間帯内で希望休でもない職員数）:"
        )
        for day, label, need_all, need_q, sup_all, sup_q, _support in rows[:top]:
            detail = []
            if sup_q < need_q:
                detail.append(f"保育士 不足{need_q - sup_q}名（必要{need_q}名/供給{sup_q}名）")
            if sup_all < need_all:
                detail.append(
                    f"人員 不足{need_all - sup_all}名（必要{need_all}名/供給{sup_all}名）"
                )
            out.append(f"  ・{day.isoformat()} {label}: " + "、".join(detail))
    out.extend(_staffing_advice(payload, rows))
    return out


def _staffing_advice(payload: _SolveInput, rows: Sequence[tuple]) -> list[str]:
    """不足の構造から「何を変えれば足りるか」を日本語で提案する。

    ``need_h`` / ``supply_h`` のどちらかが 0 になりうるため、充足率は必ず
    0 除算を避けて算出する。0 のときは充足率を出さず、原因と対処を
    日本語で返す（``solve_shift`` 全体が例外で落ちないことを優先する）。
    """
    need_h = payload.requirements.total_needed_hours()
    supply_h = _supply_hours(payload)
    if not rows:
        if need_h <= 0:
            return [
                "需要人時が 0 時間です。園児の登降園予定が登録されていないか、"
                "園の開所時間に合っているかを確認してください。"
                "在園予定を登録してから再実行してください。",
            ]
        return []
    staff = payload.staff
    placeable = [s for s in staff if s.is_placeable]
    q_total = sum(1 for s in placeable if s.is_qualified)
    se = sum(
        1
        for s in placeable
        if s.primary_role is Role.HOIKUSHI and s.contract.employment_type is not None and _is_sei(s)
    )
    part_q = q_total - se
    support = sum(1 for s in placeable if s.has_role(Role.SHIENSHIIN))
    if supply_h <= 0:
        # 契約時間帯が園の開所時間と 1 分も重なっていない入力では除算が 0 割になる
        ratio_line = (
            f"  必要人員合計 {need_h:.1f} 人時／計画期間内の契約上限合計 0.0 人時"
            "（充足率は算定不可）"
        )
    else:
        ratio_line = (
            f"  必要人員合計 {need_h:.1f} 人時／計画期間内の契約上限合計 {supply_h:.1f} 人時"
            f"（充足率 {need_h / supply_h * 100:.0f}% 相当）"
        )
    advice = [
        "",
        "【人員の見直し】",
        ratio_line,
        f"  保有: 保育士 {q_total} 名（正職員 {se} 名・パート {part_q} 名）"
        f"／子育て支援員 {support} 名／その他 {len(staff) - q_total - support} 名",
    ]
    if payload.standard is not None and payload.standard.nurse_as_qualified_cap > 0:
        nurses = sum(1 for s in placeable if s.is_nurse)
        advice.append(
            f"  看護師・准看護師 {nurses} 名は、1 時間帯につき最大 "
            f"{payload.standard.nurse_as_qualified_cap} 名を保育士1名分として数えられます"
            "（みなし保育士）。"
        )
    if supply_h <= 0:
        advice.append(
            "  ・配置できる人時が 0 です。職員の契約時間帯（earliest_start / latest_end）が"
            "園の開所時間・必要人員の時間帯と 1 分も重なっていません。"
            "契約の始業・終業時刻、園の開所時刻、延長保育の有無を調整してから"
            "再実行してください。"
        )
    elif supply_h < need_h:
        # 0 < supply_h < need_h: 供給はあるが明らかに不足。
        # 表現に注意: この分岐は supply_h < need_h なので need_h/supply_h > 1 であり、
        # 「必要量は供給の何倍か」と読む表現にしなければならない
        # （「供給は必需の何倍」と書くと供給が多いという逆の誤解を招く）。
        advice.append(
            f"  ・必要人時は配置可能人時の {need_h / supply_h:.1f} 倍です"
            f"（必要 {need_h:.1f} 人時に対し契約上限は {supply_h:.1f} 人時のみ）。"
            "計画期間全体で見ても人員が不足する計算です。職員数の増員、"
            "登降園予定の見直し、延長保育の縮小をご検討ください。"
        )
    for day, label, need_all, need_q, sup_all, sup_q, _support in rows[:3]:
        if sup_q < need_q and sup_all >= need_all:
            advice.append(
                f"  ・{day.isoformat()} {label} は「人数は足りるが保育士が足りない」状態です。"
                f"{label} の保育標準時間は保育士配置が必要なので、"
                "①保育士の増員、②他職種（支援員）を充てる条件の緩和、"
                "③保育標準時間帯の在園目標数の見直しをご検討ください。"
            )
        elif sup_all < need_all:
            advice.append(
                f"  ・{day.isoformat()} {label} は人員そのものが足りません（{need_all}名必要/"
                f"最大{sup_all}名配置可能）。職員数の増員、登降園時間の調整、"
                "延長保育の縮小をご検討ください。"
            )
        else:
            advice.append(
                f"  ・{day.isoformat()} {label} は供給自体は足りますが、"
                "1日の上限時間・連続勤務日数・最低休息時間により同時刻に揃えることが"
                "できない可能性があります。契約時間の変更（1日の上限・週所定日数）"
                "をご検討ください。"
            )
    advice.append(
        "  ・支援員は「必要保育士数」に数えられないため、保育標準時間には配置できません。"
        "保育基準で代替を認めている時間帯（延長保育の緩和措置など）を利用してください。"
    )
    return advice


def supply_hours(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
) -> float:
    """計画期間内に配置できる勤務分の上限合計（人時）を返す。

    UI の「必要／供給比」とソルバの診断で **同じ式**を使うための公開関数。
    以前 UI 側は「週契約時間 × 日数/5」を独自に計算しており、ソルバ側と
    約 1.2〜1.3 倍乖離していた。UI が「充足可能」と緑表示でもソルバが
    不足を返すような、表示と実態が食い違う不具合があった。

    算出根拠:
      * 必要人員の行がある日（＝休園日を除く）だけを数える
      * 契約時間帯・希望休のどちらにも該当しない時間帯しか無い職員は 0
      * ``max_weekly_days`` は「任意の連続した 1 週間あたりの出勤日数」の上限なので、
        :func:`~shiftai.domain.weekly_periods` の週ごとに在勤可能な日数を数え、
        契約上限と小さい方を合計する
      * ``max_weekly_days`` が 0 のときは「週あたりの上限なし」と解釈する
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    days = [d for d in requirements.all_days() if requirements.for_day(d)]
    total = 0.0
    for member in staff:
        if not member.is_placeable:
            continue
        workable = {
            d
            for d in days
            if _day_is_workable(member, d, fac)
            and any(
                _slot_is_contractible(member, s)
                and not _is_unavailable(prefs.get(member.staff_id), d, s)
                for s in requirements.slots
            )
        }
        if not workable:
            continue
        weekly_cap = member.contract.max_weekly_days
        cap_days = 0
        for window in weekly_periods(days):
            in_window = workable & set(window)
            cap_days += len(in_window) if weekly_cap <= 0 else min(len(in_window), weekly_cap)
        total += cap_days * _daily_cap_minutes(member.contract) / 60.0
    return total


def _supply_hours(payload: _SolveInput) -> float:
    """``supply_hours`` の ``_SolveInput`` 版（内部呼び出し用）。"""
    return supply_hours(payload.staff, payload.requirements, payload.prefs, payload.settings)


@dataclass(frozen=True)
class SupplyGap:
    """需要 > 供給となる 1 時間帯（Slack 相当の情報）。

    ``gap`` は「この時間帯のハード制約を満たすためにあと何人が要るか」を表す。
    配置基準を罰変数に落とした 2 パス目で実際に発生する不足量とも一致する。
    """

    day: date
    slot: Slot
    need_staff: int
    need_qualified: int
    supply_staff: int
    supply_qualified: int
    supply_support: int

    @property
    def gap_staff(self) -> int:
        return max(0, self.need_staff - self.supply_staff)

    @property
    def gap_qualified(self) -> int:
        return max(0, self.need_qualified - self.supply_qualified)

    @property
    def gap(self) -> int:
        return self.gap_staff + self.gap_qualified

    @property
    def weekday(self) -> str:
        return japanese_weekday(self.day)

    def describe(self) -> str:
        parts = []
        if self.gap_qualified:
            parts.append(
                f"保育士 不足{self.gap_qualified}名（必要{self.need_qualified}名/"
                f"供給{self.supply_qualified}名）"
            )
        if self.gap_staff:
            parts.append(
                f"人員 不足{self.gap_staff}名（必要{self.need_staff}名/供給{self.supply_staff}名）"
            )
        return f"{self.day.isoformat()} {self.slot.label}: " + "、".join(parts)


def shortfall_rows(
    staff: Sequence[StaffMember],
    requirements: RequirementTable,
    preferences: Mapping[str, StaffPreferences] | None = None,
    settings: FacilitySettings | None = None,
    standard: StaffingStandard | None = None,
) -> tuple[SupplyGap, ...]:
    """需要 > 供給となる (日, 時間帯) を不足量の大きい順に返す。

    「どの日のどの時間帯で人員が足りなかったか」を可視化するための公開関数。
    CBC を起動しないため Tab2 の「実現可能性チェック」と CLI から安全に呼べる。

    :param staff: 職員一覧（園長・主任など配置対象外の職員も渡してよい）
    :param requirements: 配置基準エンジンが必要人員を出した結果
    :param preferences: 職員IDごとの個人希望（不在時間帯の判定に使う）
    :param settings: 園設定（休園日など）
    :param standard: 看護師のみなし保育士の上限など、資格要件を判定する基準
    :returns: 不足が 0 でない時間帯だけの :class:`SupplyGap`（降順）
    """
    prefs = dict(preferences or {})
    fac = settings or FacilitySettings()
    slots = tuple(requirements.slots)
    index_of = {slot: i for i, slot in enumerate(slots)}
    need: dict[tuple[date, int], list[int]] = {}
    for day in requirements.all_days():
        for row in requirements.for_day(day):
            if not row.is_binding:
                continue
            idx = index_of.get(row.slot)
            if idx is None:
                continue
            acc = need.setdefault((day, idx), [0, 0])
            acc[0] += row.needed_staff
            acc[1] += row.needed_qualified
    placeable = [m for m in staff if m.is_placeable]
    out: list[SupplyGap] = []
    for (day, idx), (need_all, need_q) in need.items():
        if need_all <= 0 and need_q <= 0:
            continue
        sup_all = sup_q = sup_support = 0
        available: list[StaffMember] = []
        for member in placeable:
            if not _day_is_workable(member, day, fac):
                continue
            if not _slot_is_contractible(member, slots[idx]):
                continue
            if _is_unavailable(prefs.get(member.staff_id), day, slots[idx]):
                continue
            available.append(member)
        sup_all = len(available)
        sup_q = qualified_count(available, standard)
        sup_support = sum(1 for m in available if m.has_role(Role.SHIENSHIIN))
        gap = max(0, need_all - sup_all) + max(0, need_q - sup_q)
        if gap > 0:
            out.append(
                SupplyGap(
                    day=day,
                    slot=slots[idx],
                    need_staff=need_all,
                    need_qualified=need_q,
                    supply_staff=sup_all,
                    supply_qualified=sup_q,
                    supply_support=sup_support,
                )
            )
    out.sort(key=lambda g: (-g.gap, g.day, g.slot.label))
    return tuple(out)


def _shortfall_supply(payload: _SolveInput) -> list[tuple]:
    """需要 > 供給となる (日, 時間帯) を不足量の大きい順に返す（内部用の簡略版）。"""
    return [
        (
            g.day,
            g.slot.label,
            g.need_staff,
            g.need_qualified,
            g.supply_staff,
            g.supply_qualified,
            g.supply_support,
        )
        for g in shortfall_rows(
            payload.staff,
            payload.requirements,
            payload.prefs,
            payload.settings,
            payload.standard,
        )
    ]




def _attach_violations(
    result: SolveResult,
    requirements: RequirementTable,
    staff: Sequence[StaffMember],
    preferences: dict[str, StaffPreferences],
    standard: StaffingStandard | None,
    settings: FacilitySettings,
) -> None:
    """解に対して法令・契約違反を検査しアタッチする。"""
    from shiftai.gap_analysis import check_violations

    try:
        result.violations = check_violations(
            requirements,
            result,
            staff,
            preferences,
            standard=standard,
            settings=settings,
        )
    except Exception as exc:
        result.violations = [
            Violation(
                severity=ViolationSeverity.WARNING,
                code="CHECKER_FAILED",
                message=f"違反検査中にエラーが発生しました: {exc}",
            )
        ]
