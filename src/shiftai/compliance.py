"""制度別の適合チェックリスト（    認可外保育施設の報告・巡回指導・助成申請の場面）。

**このモジュールが解決する問題**

配置基準エンジン（:mod:`shiftai.standards`）が扱うのは
「その時間帯に何人在園させればよいか」だけである。実際の届出・報告では
1 日の**常勤換算人数**で判定し、さらに施設の属性agroove依存の必置職員や
比率上限が加わる。シフト作成の機能では表せない次の要件がある。

* 月極めの基礎乳幼児数と、日極め等の平均加算（指導監督基準 第1(1)）
* 短時間勤務職員の**常勤換算**（有資格者・その他別に勤務延べ時間÷8時間）
* 保育従事者の**半分以上**（保育事業者型は4分の3以上）が保育士であること
* 保健師・看護師・准看護師の**みなし保育士は1人に限り**
* **嘱託医・調理員の必置**（調理業務の全部委託または食事の搬入で調理員を免除できる）
* 11時間を超える時間帯の**常時2人以上**
* **地域枠は総定員の50%以内**

これらを「人が表で確認する」運用では、項目が漏れると報告書に !
そのまま出す。ここでは制度を明示したうえで**機械的に判定**し、
「適合／不適合／該当なし／未確認」を判定根拠つきで返す。

**機械で判定できないものは「未確認」で出す**

嘱託医の有無、調理業務の委託形態、自治体の運用解釈は
**このツールの情報だけでは判定できない**。それらを勝手に適合にして
「適合」と言い切ると、巡回指導での信頼性を失わせる。
そのため該当項目は ``UNKNOWN`` を返し、``spec`` で利用者が明示する。
``UNKNOWN`` は不適合 (``NG``) として扱わないが、**報告に使える状態ではない**ことを
:attr:`ComplianceReport.is_filing_ready` が示す。

法令の主な根拠:

* 認可外保育施設指導監督基準（令和6年3月29日 こ成保第206号、
  令和6年4月10日 こ成保第230号）第1
* 「企業主導型保育事業費補助金実施要綱」第3の2(4)①（職員）・同(5)（設備）
* 家庭的保育事業等の設備及び運営に関する基準（平成26年厚生労働省令第61号）
* 児童福祉法第59条の2第1項・第6条の3第12項
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from enum import Enum

from shiftai.domain import (
    AgeClass,
    Role,
    StaffingStandard,
)

__all__ = [
    "OK",
    "NG",
    "NA",
    "UNKNOWN",
    "STATUS_LABELS",
    "ComplianceCheck",
    "ComplianceReport",
    "FacilitySpec",
    "Regulation",
    "StaffRecord",
    "audit_facility",
    "to_dataframe",
    "to_markdown",
]


class Regulation(str, Enum):
    """対象の制度。プリセット（:mod:`shiftai.local_rules`）と対��させる。"""

    LICENSED_HOIKUSHI = "認可保育所"
    UNLICENSED_HOIKUSHI = "認可外保育施設"
    CORPORATE_LED = "企業主導型保育事業"
    SMALL_SCALE = "小規模保育事業"
    WORKPLACE = "事業所内保育事業"

    @property
    def preset_key(self) -> str | None:
        """対応するプリセットのキー。

        ``LICENSED_HOIKUSHI`` と ``UNLICENSED_HOIKUSHI`` を除くと、
        「小規模保育事業」「事業所内保育事業」種別ごとに必要数・補助単価の枠組みが
        別 (``A``/``B``/``C`` 型、大規模型/小規模型) なので自動選択できない。
        そのため ``None`` を返し、利用者に ``FacilitySpec.standard`` の明示を求める。
        """
        if self is Regulation.LICENSED_HOIKUSHI:
            return "全国基準（厚労省）"
        if self is Regulation.UNLICENSED_HOIKUSHI:
            return "認可外保育施設（指導監督基準）"
        if self is Regulation.CORPORATE_LED:
            return "企業主導型保育事業（単独枠）"
        return None

    @property
    def is_unlicensed(self) -> bool:
        """認可外保育施設（企業主導型保育事業・小規模保育事業・事業所内保育事業）か。"""
        return self in _UNLICENSED


_UNLICENSED = frozenset(
    {
        Regulation.UNLICENSED_HOIKUSHI,
        Regulation.CORPORATE_LED,
        Regulation.SMALL_SCALE,
        Regulation.WORKPLACE,
    }
)

#: 判定結果。
OK = "ok"
NG = "ng"
NA = "na"
UNKNOWN = "unknown"

STATUS_LABELS: dict[str, str] = {
    OK: "適合",
    NG: "不適合",
    NA: "該当なし",
    UNKNOWN: "未確認",
}

#: 短時間勤務者を常勤換算するときの1人あたりの時間（時間）。
#: 認可外保育施設指導監督基準 第1(1) は有資格者・その他別に
#: 勤務延べ時間数の合計を 8 時間で除して常勤職員数とみなすとする。
FULL_TIME_HOURS = 8.0

#: 認可外保育施設の主たる開所時間（時間）。
MAIN_OPENING_HOURS = 11


# ---------------------------------------------------------------------------
# 入力
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaffRecord:
    """在籍職員 1 人分。シフトではなく**在籍と資格**だけを表現する。"""

    staff_id: str
    name: str = ""
    roles: tuple[Role, ...] = ()
    weekly_hours: float = FULL_TIME_HOURS * 5
    is_certified: bool | None = None
    """地域型子育て支援員研修／市町村研修の修了有無。``None`` は未確認。"""

    @property
    def is_hoikushi(self) -> bool:
        return Role.HOIKUSHI in self.roles

    @property
    def is_nurse(self) -> bool:
        return Role.KANGSHI in self.roles

    @property
    def is_chuubou(self) -> bool:
        return Role.CHUUBOU in self.roles

    @property
    def is_yojoi(self) -> bool:
        """嘱託医。職員表に資格を持たないため、外部入力でのみ立てる。"""
        return self.staff_id.strip() in {"嘱託医", "嘱託医師", "joi"}

    def full_time_equivalent(self) -> float:
        """常勤換算人数（有資格者・その他別はこのメソッドを分けて使う）。"""
        return max(0.0, self.weekly_hours) / FULL_TIME_HOURS


@dataclass(frozen=True)
class FacilitySpec:
    """1 園ぶんの申告情報（月次報告・巡回指導で Asking されるもの）。"""

    regulation: Regulation = Regulation.LICENSED_HOIKUSHI
    name: str = ""
    capacity: int = 0
    """利用定員。6人未満は届出対象外、20人以上で保育事業者型の基準になる。"""
    is_shared_operator: bool = False
    """保育事業者型事業（共同利用枠）を実施しているか。"""
    opening: time = time(8, 30)
    closing: time = time(19, 30)

    monthly_children: Mapping[AgeClass, int] = field(default_factory=dict)
    """月極めの基礎乳幼児数（年度初日の前日の満年齢ベース）。"""
    average_daily_children: Mapping[AgeClass, int] = field(default_factory=dict)
    """日極め等の**平均的な**加算人数。指導監督基準 第1(1) により基礎人数に加える。"""

    staff: tuple[StaffRecord, ...] = ()

    has_contract_doctor: bool | None = None
    """嘱託医を置くか。``None`` は未確認（機械では判定できない）。"""
    cooking_outsourced: bool | None = None
    """調理業務の全部委託。"""
    meals_imported: bool | None = None
    """他施設から食事の搬入を受ける。"""
    local_quota_children: int = 0
    """地域枠で受け入れる児童数（地域枠は総定員の50%以内）。"""

    standard: StaffingStandard | None = None
    """適用する配置基準。未指定なら制度から既定を選ぶ。"""

    def total_children(self) -> int:
        return sum(self.monthly_children.values()) + sum(self.average_daily_children.values())

    def effective_counts(self) -> dict[AgeClass, int]:
        """月極めの基礎人数に日極め等の平均加算を加えた人数。"""
        out = {age: max(0, n) for age, n in self.monthly_children.items()}
        for age, n in self.average_daily_children.items():
            out[age] = out.get(age, 0) + max(0, n)
        return {age: n for age, n in out.items() if n > 0}

    def standard_for(self) -> StaffingStandard:
        if self.standard is not None:
            return self.standard
        from shiftai import local_rules

        key = self.regulation.preset_key
        if key is None:
            raise ValueError(
                f"{self.regulation.value} に対応するプリセットがありません。"
                "FacilitySpec.standard に StaffingStandard を明示してください。"
            )
        return local_rules.get_standard(key)

    def qualified_ratio_required(self) -> float | None:
        """この施設で必要な保育士比率。判定できない制度では ``None``。"""
        if not self.regulation.is_unlicensed:
            return None
        # 保育事業者型事業は「利用定員20名以上」のときだけ 4分の3 以上。
        # 共同利用枠（is_shared_operator）でも定員20名未満なら単独認可外と同じ半数。
        if self.is_shared_operator and self.capacity >= 20:
            return 0.75
        return 0.5


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceCheck:
    """チェック 1 件。"""

    key: str
    title: str
    status: str
    actual: str
    required: str
    basis: str
    message: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status in (OK, NA)

    @property
    def is_violation(self) -> bool:
        return self.status == NG

    @property
    def is_unknown(self) -> bool:
        return self.status == UNKNOWN

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> dict[str, str]:
        return {
            "項目": self.title,
            "判定": self.status_label,
            "実測": self.actual,
            "基準": self.required,
            "根拠": self.basis,
            "内容": self.message,
        }


@dataclass(frozen=True)
class ComplianceReport:
    """チェック結果のまとめ。"""

    facility_name: str
    regulation: Regulation
    checks: tuple[ComplianceCheck, ...] = ()

    @property
    def violations(self) -> tuple[ComplianceCheck, ...]:
        return tuple(c for c in self.checks if c.is_violation)

    @property
    def unknowns(self) -> tuple[ComplianceCheck, ...]:
        return tuple(c for c in self.checks if c.is_unknown)

    @property
    def has_violation(self) -> bool:
        return bool(self.violations)

    @property
    def is_filing_ready(self) -> bool:
        """報告・申請に使える状態か。

        不適合が 0 かつ未確認が 0 のときだけ ``True``。
        機械で判定できない要件が残っている限り「適合」とは言わない。
        """
        return not self.violations and not self.unknowns

    def summary(self) -> str:
        if not self.checks:
            return "チェック項目がありません。"
        return (
            f"{self.regulation.value}／{self.facility_name or '(名称未設定)'}："
            f"不適合 {len(self.violations)} 件・未確認 {len(self.unknowns)} 件"
            f"／全 {len(self.checks)} 件"
            + ("（報告可）" if self.is_filing_ready else "（報告不可）")
        )

    def to_dataframe(self):  # pragma: no cover - pandas は lazy import
        from shiftai.compliance import to_dataframe as _to_dataframe

        return _to_dataframe(self)

    def to_markdown(self) -> str:
        from shiftai.compliance import to_markdown as _to_markdown

        return _to_markdown(self)


def _fmt_ratio(value: float) -> str:
    """0.5 → '2分の1'、0.75 → '4分の3' のように読みやすい形にする。"""
    for numerator, denominator in ((1, 2), (1, 3), (2, 3), (3, 4), (1, 4)):
        if abs(value - numerator / denominator) < 1e-9:
            return f"{denominator}分の{numerator}"
    return f"{value:.0%}"


def _duration_hours(opening: time, closing: time) -> float:
    """開所時間（時間単位）。翌日をまたぐ場合は 24 時間を足す。"""
    start = opening.hour * 60 + opening.minute
    end = closing.hour * 60 + closing.minute
    if end <= start:
        end += 24 * 60
    return (end - start) / 60.0


def _qualified_fte(records: Sequence[StaffRecord], standard: StaffingStandard) -> float:
    """「必要保育士数」を満たす人数を常勤換算で求める。

    :func:`shiftai.domain.qualified_count` と同じルール（看護師の扱い）を
    常勤換算に適用したもの。准看護師も対象だが、
    ``StaffRecord.roles`` には ``Role.KANGSHI`` しか無 nurse のため、
    看護師と准看護師の区別はこのデータモデルでは表現できない。
    """
    hoikushi = sum(r.full_time_equivalent() for r in records if r.is_hoikushi)
    nurses = sum(r.full_time_equivalent() for r in records if r.is_nurse)
    if standard.qualified_extra_roles & {Role.KANGSHI}:
        return hoikushi + nurses
    if standard.nurse_as_qualified_cap <= 0:
        return hoikushi
    return hoikushi + min(nurses, float(standard.nurse_as_qualified_cap))


def _nurse_note(standard: StaffingStandard) -> str:
    """看護師の扱いが制度で違うので、判定文に明示する。"""
    if standard.nurse_as_qualified_cap > 0:
        return f"（みなし保育士として看護師は最大 {standard.nurse_as_qualified_cap} 人を計上）"
    if standard.qualified_extra_roles & {Role.KANGSHI}:
        return "（看護師・准看護師は比率の分子にそのまま計上）"
    return "（看護師は保育士に数えない）"


def _staff_checks(spec: FacilitySpec, standard: StaffingStandard) -> list[ComplianceCheck]:
    """職員要件（人数・資格・必置職員）に関するチェック。"""
    out: list[ComplianceCheck] = []
    basis_facility = (
        "企業主導型保育事業費補助金実施要綱 第3の2(4)"
        if spec.regulation is Regulation.CORPORATE_LED
        else "認可外保育施設指導監督基準 第1"
    )
    basis_standard = "認可外保育施設指導監督基準 第1"
    # 嘱託医・調理員の必置は実施要綱の要件。認可外保育施設（指導監督基準）では
    # 家庭的保育事業等の設備及び運営に関する基準 第15条の食事提供に従う。
    basis_mandatory = (
        "企業主導型保育事業費補助金実施要綱 第3の2(4)①"
        if spec.regulation is Regulation.CORPORATE_LED
        else "家庭的保育事業等の設備及び運営に関する基準 第15条（食事提供）"
    )
    counts = spec.effective_counts()
    total_children = sum(counts.values())

    # --- 1. 必要保育従事者数（常勤換算） ---------------------------------
    needed = standard.headcount_for_slot(counts)
    needed = max(needed, standard.min_staff_per_room if total_children else 0)
    qualified_target = standard.slot_qualified_for(needed)
    hoikushi_fte = sum(s.full_time_equivalent() for s in spec.staff if s.is_hoikushi)
    other_fte = sum(
        s.full_time_equivalent() for s in spec.staff if not s.is_hoikushi and not s.is_chuubou
    )
    total_fte = hoikushi_fte + other_fte

    if total_children <= 0:
        out.append(
            ComplianceCheck(
                key="headcount",
                title="必要保育従事者数",
                status=NA,
                actual="在園児 0 名",
                required=f"{needed} 名",
                basis=basis_facility,
                message="在園児がいないため判定しません。",
            )
        )
    elif spec.regulation.is_unlicensed:
        detail = (
            f"月極めの基礎乳幼児数 + 日極めの平均加算 = {total_children} 名"
            f"／常勤換算 保育士 {hoikushi_fte:.2f} 人・その他 {other_fte:.2f} 人"
            f"（合計 {total_fte:.2f} 人）"
        )
        out.append(
            ComplianceCheck(
                key="headcount",
                title="必要保育従事者数（常勤換算）",
                status=OK if total_fte >= needed else NG,
                actual=detail,
                required=f"{needed} 名以上",
                basis=f"{basis_facility}／{basis_standard} 第1(1)",
                message=(
                    ""
                    if total_fte >= needed
                    else f"常勤換算で {needed - total_fte:.2f} 人不足しています。"
                    "短時間勤務者は週契約時間を 8 時間で割って常勤換算します。"
                ),
            )
        )
    else:
        out.append(
            ComplianceCheck(
                key="headcount",
                title="必要保育従事者数",
                status=NA,
                actual=f"在園児 {total_children} 名／保育従事者 {len(spec.staff)} 名",
                required=f"{needed} 名",
                basis="保育所の職員配置基準（昭和52年厚生省告示第49号）",
                message=(
                    "認可保育所の常勤換算は本モジュールの対象外です。"
                    "シフト作成時の必要人員は配置基準プリセットで確認してください。"
                ),
            )
        )

    # --- 2. 最低2名（そのうち1名以上保育士） -----------------------------
    if total_children <= 0:
        out.append(
            ComplianceCheck(
                key="min_two",
                title="最低配置人数",
                status=NA,
                actual="在園児 0 名",
                required=f"{standard.min_staff_per_room} 名（うち保育士 1 名以上）",
                basis=basis_facility,
                message="在園児がいないため判定しません。",
            )
        )
    else:
        present = [s for s in spec.staff if not s.is_chuubou]
        nurses_count = sum(1 for s in present if s.is_nurse)
        qualified = _qualified_fte(present, standard)
        min_two = standard.min_staff_per_room
        status = OK if (len(present) >= min_two and qualified >= 1) else NG
        message = ""
        if len(present) < min_two:
            message = f"保育従事者が {min_two - len(present)} 名不足しています。"
        elif qualified < 1:
            message = "保育士（またはみなし保育士として数えられる看護師）が 1 名もいません。"
        out.append(
            ComplianceCheck(
                key="min_two",
                title="最低配置人数",
                status=status,
                actual=(
                    f"保育従事者 {len(present)} 名"
                    f"（看護師 {nurses_count} 名、うち保育士換算 {qualified:.2f} 名）"
                ),
                required=f"{min_two} 名（うち保育士 1 名以上）",
                basis=f"{basis_facility}／月次報告の手引き",
                message=message,
            )
        )

    # 保育士比率は制度ごとに根拠が異なる
    if spec.regulation is Regulation.CORPORATE_LED:
        ratio_basis = "企業主導型保育事業費補助金実施要綱 第3の2(4)②"
    else:
        ratio_basis = "認可外保育施設指導監督基準 第1(2)"

    # --- 3. 保育士比率 ---------------------------------------------------
    ratio_required = spec.qualified_ratio_required()
    if ratio_required is None or total_children <= 0:
        out.append(
            ComplianceCheck(
                key="qualified_ratio",
                title="保育士比率",
                status=NA,
                actual=f"保育士 {hoikushi_fte:.2f} 人／保育従事者 {total_fte:.2f} 人",
                required="認可外保育施設の要件",
                basis=ratio_basis,
                message="この制度の保育士比率は認可外保育施設の要件で判定します。",
            )
        )
    else:
        # 比率の分子は制度で異なる。企業主導型保育事業は「保育士」だけ、
        # 認可外保育施設指導監督基準 第1(2) は「保育士・准看護師・看護師」。
        numerator_fte = _qualified_fte(spec.staff, standard)
        actual_ratio = (numerator_fte / total_fte) if total_fte > 0 else 0.0
        enough = total_fte > 0 and actual_ratio >= ratio_required - 1e-9
        out.append(
            ComplianceCheck(
                key="qualified_ratio",
                title="保育士比率",
                status=OK if enough else NG,
                actual=(
                    f"{actual_ratio:.1%}（{numerator_fte:.2f} 人／保育従事者 {total_fte:.2f} 人）"
                ),
                required=f"{_fmt_ratio(ratio_required)}以上",
                basis=ratio_basis,
                message=(
                    ""
                    if enough
                    else "保育従事者のうち"
                    f" {_fmt_ratio(ratio_required)}以上を保育士とする必要があります。"
                    + (
                        "利用定員20人以上の保育事業者型事業は4分の3以上です。"
                        if ratio_required >= 0.75
                        else "不足分は保育士として採用するか、"
                        "子育て支援員研修（地域型保育）の修了者の活用で補う。"
                    )
                ),
            )
        )
        if qualified_target > 0 and qualified < qualified_target:
            out.append(
                ComplianceCheck(
                    key="qualified_count",
                    title="必要保育士数（1 時間帯あたり）",
                    status=NG,
                    actual=f"常勤換算の保育士 {qualified:.2f} 人" + _nurse_note(standard),
                    required=f"{qualified_target} 名以上",
                    basis=ratio_basis,
                    message=(
                        f"必要保育士数 {qualified_target} 名に対し "
                        f"{qualified_target - qualified:.2f} 名不足しています。"
                        + (
                            "看護師は1人に限り保育士1名分として数えられます。"
                            if standard.nurse_as_qualified_cap > 0
                            else "看護師・准看護師は比率の分子にそのまま入ります。"
                        )
                    ),
                )
            )
        else:
            out.append(
                ComplianceCheck(
                    key="qualified_count",
                    title="必要保育士数（1 時間帯あたり）",
                    status=OK,
                    actual=f"常勤換算の保育士 {qualified:.2f} 人" + _nurse_note(standard),
                    required=f"{qualified_target} 名以上",
                    basis=ratio_basis,
                    message="",
                )
            )

    # --- 4. 保育従事者の資格 ---------------------------------------------
    # 保育士・看護師・准看護師は職種が資格そのものである。
    # is_certified は「それ以外の職員が研修を修了したか」だけを表す。
    others = [s for s in spec.staff if not (s.is_hoikushi or s.is_nurse or s.is_chuubou)]
    unqualified = [s for s in others if s.is_certified is not True]
    uncertified = [s for s in others if s.is_certified is None]
    if not spec.regulation.is_unlicensed:
        out.append(
            ComplianceCheck(
                key="staff_qualification",
                title="保育従事者の資格",
                status=NA,
                actual=f"職員 {len(spec.staff)} 名",
                required="認可保育所（保育所の職員配置基準）",
                basis="保育所の職員配置基準（昭和52年厚生省告示第49号）",
                message="資格要件は認可外保育施設の基準で判定します。",
            )
        )
    elif unqualified:
        out.append(
            ComplianceCheck(
                key="staff_qualification",
                title="保育従事者の資格",
                status=NG,
                actual=(
                    f"資格が確認できない保育従事者 {len(unqualified)} 名"
                    f"（{', '.join(s.name or s.staff_id for s in unqualified[:5])}）"
                ),
                required="保育士／子育て支援員研修（地域型）修了者／市町村研修修了者",
                basis=basis_facility,
                message=(
                    "上記以外の職員は保育従事者として数えられません。"
                    "研修了の記録、または当該年度に受講予定であるかを確認してください。"
                ),
            )
        )
    else:
        out.append(
            ComplianceCheck(
                key="staff_qualification",
                title="保育従事者の資格",
                status=OK,
                actual=(
                    f"資格を満たした保育従事者 "
                    f"{len(others) - len(uncertified)} 名"
                    f"／研修受講予定を確認していない {len(uncertified)} 名"
                    if uncertified
                    else f"資格を満たした保育従事者 {len(others)} 名"
                ),
                required="保育士／子育て支援員研修（地域型）修了者／市町村研修修了者",
                basis=basis_facility,
                message=("要綱は「当該年度中に受講予定者」を認めます。" if uncertified else ""),
            )
        )

    # --- 5. 嘱託医・調理員の必置 -------------------------------------------
    if spec.has_contract_doctor is None:
        out.append(
            ComplianceCheck(
                key="contract_doctor",
                title="嘱託医の配置",
                status=UNKNOWN,
                actual="未確認",
                required="嘱託医を置く",
                basis=basis_mandatory,
                message=(
                    "嘱託医の有無は職員表に資格を持たないため本ツールでは判定できません。"
                    " FacilitySpec.has_contract_doctor に True/False を入れてください。"
                ),
            )
        )
    else:
        out.append(
            ComplianceCheck(
                key="contract_doctor",
                title="嘱託医の配置",
                status=OK if spec.has_contract_doctor else NG,
                actual="あり" if spec.has_contract_doctor else "なし",
                required="嘱託医を置く",
                basis=basis_mandatory,
                message="" if spec.has_contract_doctor else "嘱託医が配置されていません。",
            )
        )

    cooking_outsourced = spec.cooking_outsourced
    meals_imported = spec.meals_imported
    if cooking_outsourced is None and meals_imported is None:
        out.append(
            ComplianceCheck(
                key="cook",
                title="調理員の配置",
                status=UNKNOWN,
                actual="未確認",
                required="調理員を置く（調理委託・食事搬入なら免除）",
                basis=basis_mandatory,
                message=(
                    "調理業務の委託形態は本ツールでは判定できません。"
                    " FacilitySpec.cooking_outsourced / meals_imported に"
                    " True/False を入れてください。"
                ),
            )
        )
    else:
        exempted = bool(cooking_outsourced) or bool(meals_imported)
        cooks = [s for s in spec.staff if s.is_chuubou]
        if exempted:
            out.append(
                ComplianceCheck(
                    key="cook",
                    title="調理員の配置",
                    status=NA,
                    actual=("調理業務の全部委託" if cooking_outsourced else "他施設から食事の搬入"),
                    required="調理員を置く（委託・搬入なら免除）",
                    basis=basis_mandatory,
                    message="委託または搬入のため調理員の配置は免除されます。",
                )
            )
        else:
            out.append(
                ComplianceCheck(
                    key="cook",
                    title="調理員の配置",
                    status=OK if cooks else NG,
                    actual=f"調理員 {len(cooks)} 名",
                    required="調理員 1 名以上",
                    basis=basis_mandatory,
                    message="" if cooks else "調理員が配置されていません。",
                )
            )

    # --- 6. 11時間を超える時間帯の常時2人以上 ----------------------------
    out.append(_main_hours_check(spec))

    # --- 7. 地域枠 50% 上限 ---------------------------------------------
    if not spec.regulation.is_unlicensed:
        out.append(
            ComplianceCheck(
                key="local_quota",
                title="地域枠の上限",
                status=NA,
                actual=f"地域枠 {spec.local_quota_children} 名",
                required="認可外保育施設の要件",
                basis=basis_facility,
                message="地域枠の上限は認可外保育施設の要件です。",
            )
        )
    else:
        limit = spec.capacity // 2
        out.append(
            ComplianceCheck(
                key="local_quota",
                title="地域枠の上限",
                status=OK if spec.local_quota_children <= limit else NG,
                actual=f"地域枠 {spec.local_quota_children} 名／総定員 {spec.capacity} 名",
                required=f"{limit} 名以下（総定員の50%以内）",
                basis=basis_facility,
                message=(
                    ""
                    if spec.local_quota_children <= limit
                    else "地域枠の総定員に対する割合が50%を超えています。"
                    "地域枠で受け入れた児童がある場合は指導・監督上の指摘の対象です。"
                ),
            )
        )

    return out


def _main_hours_check(spec: FacilitySpec) -> ComplianceCheck:
    """主たる開所時間（11時間）を超える時間帯の常時2名ルール。"""
    basis = "認可外保育施設指導監督基準 第1(1)"
    hours = _duration_hours(spec.opening, spec.closing)
    if not spec.regulation.is_unlicensed:
        return ComplianceCheck(
            key="main_hours",
            title="開所時間（11時間ルール）",
            status=NA,
            actual=f"開所 {spec.opening.strftime('%H:%M')}〜{spec.closing.strftime('%H:%M')}"
            f"（{hours:.2f} 時間）",
            required="認可外保育施設の要件",
            basis=basis,
            message="11時間ルールは認可外保育施設の基準です。",
        )
    if hours <= MAIN_OPENING_HOURS:
        return ComplianceCheck(
            key="main_hours",
            title="開所時間（11時間ルール）",
            status=OK,
            actual=f"{hours:.2f} 時間",
            required=f"{MAIN_OPENING_HOURS} 時間以内",
            basis=basis,
            message=(
                f"開所時間が {MAIN_OPENING_HOURS} 時間以下のため、"
                "それを超える時間帯の常時2名ルールは適用されません。"
            ),
        )
    present = [s for s in spec.staff if not s.is_chuubou]
    return ComplianceCheck(
        key="main_hours",
        title="開所時間（11時間ルール）",
        status=OK if len(present) >= 2 else NG,
        actual=(
            f"{hours:.2f} 時間（11時間を "
            f"{hours - MAIN_OPENING_HOURS:.2f} 時間超える）／保育従事者 {len(present)} 名"
        ),
        required="超過時間帯は常時 2 名以上（保育中の児童が 1 名のときは除く）",
        basis=basis,
        message=(
            ""
            if len(present) >= 2
            else "11時間を超える時間帯に2名以上を配置できません。"
            "該当時間帯は閉所時間を早めるか、職員を追加してください。"
        ),
    )


def audit_facility(spec: FacilitySpec) -> ComplianceReport:
    """1 園ぶんの制度適合を判定する。

    :param spec: 申告情報
    :returns: :class:`ComplianceReport`（不適合と未確認を区別して保持する）
    """
    standard = spec.standard_for()
    checks: list[ComplianceCheck] = []

    # 届出対象か（児童福祉法第59条の2第1項は利用定員6人以上の届出施設）
    if spec.regulation.is_unlicensed:
        checks.append(
            ComplianceCheck(
                key="filing",
                title="届出対象か",
                status=OK if spec.capacity >= 6 else NA,
                actual=f"利用定員 {spec.capacity} 名",
                required="利用定員 6 人以上",
                basis="児童福祉法第59条の2第1項",
                message=(
                    "" if spec.capacity >= 6 else "利用定員6人未満は第6条の3第12項の届出対象です。"
                ),
            )
        )
    else:
        checks.append(
            ComplianceCheck(
                key="filing",
                title="届出対象か",
                status=NA,
                actual=f"利用定員 {spec.capacity} 名",
                required="認可保育所",
                basis="児童福祉法第32条",
                message="認可保育所は認可の対象であり、届出施設ではありません。",
            )
        )

    # 算出手法の一致（プリセットを取り違えていないか）
    expected_mode = "facility_formula" if spec.regulation.is_unlicensed else "per_class"
    matches = standard.headcount_mode == expected_mode
    checks.append(
        ComplianceCheck(
            key="standard_mode",
            title="算出手法と制度の整合",
            status=OK if matches else NG,
            actual=(f"{standard.name}／{standard.headcount_mode}／＋{standard.headcount_extra}"),
            required=(
                "認可外保育施設は facility_formula（区分ごとに切捨て→合計→＋1→四捨五入）"
                if spec.regulation.is_unlicensed
                else "認可保育所は per_class（年齢クラス毎に切り上げ）"
            ),
            basis="local_rules.SCOPE_STATEMENT",
            message=(
                ""
                if matches
                else "制度と算出手法が食い違っています。認定外保育施設のプリセットを"
                "使うと 1 時間帯あたり最大 4 名ずれます。"
            ),
        )
    )

    # 面積基準（家庭的保育事業等の設備及び運営に関する基準 第43条／第28条）
    if spec.regulation.is_unlicensed:
        area_note = (
            "利用定員20名以上: 乳児室 1.65㎡/人＋ほふく室 3.3㎡/人、"
            "2 歳以上 保育室・遊戯室 1.98㎡/人、屋外遊技場 3.3㎡/人"
            if spec.capacity >= 20
            else "利用定員19名以下: 乳児室・ほふく室 3.3㎡/人"
        )
        checks.append(
            ComplianceCheck(
                key="area",
                title="面積基準",
                status=UNKNOWN,
                actual=f"利用定員 {spec.capacity} 名",
                required=area_note,
                basis="家庭的保育事業等の設備及び運営に関する基準 第43条／第28条",
                message=(
                    "面積は本ツールでは判定できません（園舎図面の数値が必要です）。"
                    "乳児受入れの場合は医務室・調理室・幼児用便座付き便所が必須です。"
                ),
            )
        )

    checks.extend(_staff_checks(spec, standard))
    return ComplianceReport(
        facility_name=spec.name,
        regulation=spec.regulation,
        checks=tuple(checks),
    )


def to_dataframe(report: ComplianceReport):
    """チェック結果を DataFrame にする。"""
    import pandas as pd

    return pd.DataFrame.from_records([c.to_dict() for c in report.checks])


def to_markdown(report: ComplianceReport) -> str:
    """チェック結果を Markdown にする（月次報告の貼り付け用）。"""
    lines = [
        f"## {report.regulation.value} 適合チェック（{report.facility_name or '名称未設定'}）",
        "",
        report.summary(),
        "",
        "| 項目 | 判定 | 実測 | 基準 | 根拠 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(
            f"| {check.title} | {check.status_label} | {check.actual} | "
            f"{check.required} | {check.basis} |"
        )
    if report.violations:
        lines += ["", "### 不適合"]
        lines += [f"- {c.title}: {c.message}" for c in report.violations]
    if report.unknowns:
        lines += ["", "### 未確認（人が判断する必要がある）"]
        lines += [f"- {c.title}: {c.message}" for c in report.unknowns]
    return "\n".join(lines)
