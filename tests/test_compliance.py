"""制度別コンプライアンスチェック（:mod:`shiftai.compliance`）の検証。

**なぜこのテストが要るか**

シフト作成の機能だけで認可外保育施設の届出・報告は出せない。
要綱と指導監督基準が求める下列の要件は、いずれも
「その時間帯の在園人数」から導けない。

* 月極めの基礎乳幼児数 + 日極めの平均加算
* 短時間勤務者の**常勤換算**（週契約時間 ÷ 8 時間）
* 保育従事者の**半分以上**（保育事業者型は4分の3以上）が保育士であること
* **みなし保育士（看護師）は1人に限り**
* **嘱託医・調理員の必置**（調理委託・食事搬入で調理員を免除できる）
* 11時間を超える時間帯の**常時2人以上**
* **地域枠は総定員の50%以内**

さらに重要なのは、**機械で判定できない項目を勝手に「適合」にしない**こと。
嘱託医の有無や調理業務の委託形態は本ツールの情報からは判らないため
``UNKNOWN``（未確認）を返し、報告可能かどうかを
:attr:`~shiftai.compliance.ComplianceReport.is_filing_ready` で示す。
これを固定しないと「適合」と言い切って巡回指導で、信用を失う。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import time

import pytest

from shiftai import local_rules
from shiftai.compliance import (
    NA,
    NG,
    OK,
    UNKNOWN,
    ComplianceReport,
    FacilitySpec,
    Regulation,
    StaffRecord,
    audit_facility,
    to_dataframe,
    to_markdown,
)
from shiftai.domain import AgeClass, Role

FACILITY_KEY = "企業主導型保育事業（単独枠）"
SHARED_KEY = "企業主導型保育事業（保育事業者型・20名以上）"


def spec(**overrides) -> FacilitySpec:
    base = FacilitySpec(
        regulation=Regulation.CORPORATE_LED,
        name="サンプル園",
        capacity=40,
        opening=time(7, 15),
        closing=time(19, 30),
        monthly_children={
            AgeClass.INFANT: 8,
            AgeClass.AGE_1: 6,
            AgeClass.AGE_2: 6,
            AgeClass.AGE_3: 4,
            AgeClass.AGE_4: 3,
            AgeClass.AGE_5: 3,
        },
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("H2", "B", (Role.HOIKUSHI,), 40.0),
            StaffRecord("H3", "C", (Role.HOIKUSHI,), 40.0),
            StaffRecord("H4", "D", (Role.HOIKUSHI,), 40.0),
            StaffRecord("N1", "E", (Role.KANGSHI,), 40.0),
            StaffRecord("S1", "F", (Role.SHIENSHIIN,), 30.0, True),
            StaffRecord("K1", "G", (Role.CHUUBOU,), 40.0),
        ),
        has_contract_doctor=True,
        cooking_outsourced=False,
        local_quota_children=10,
    )
    return replace(base, **overrides)


def check(report: ComplianceReport, key: str):
    return next(c for c in report.checks if c.key == key)


# ---------------------------------------------------------------------------
# 1. 正常系の判定
# ---------------------------------------------------------------------------


def test_基準を満たす園は不適合にならない() -> None:
    report = audit_facility(spec())
    assert report.violations == (), [c.to_dict() for c in report.violations]


def test_報告可能かどうかを判定できる() -> None:
    """面積は機械で判定できないため、最初は必ず未確認になる。"""
    report = audit_facility(spec())
    assert check(report, "area").status == UNKNOWN
    assert report.is_filing_ready is False
    assert "報告不可" in report.summary()


def test_算手法が制度と食い違えば不適合になる() -> None:
    """認可保育所のプリセットを認可外に使った状態を検出できる。"""
    wrong = local_rules.get_standard("全国基準（厚労省）")
    report = audit_facility(spec(standard=wrong))
    result = check(report, "standard_mode")
    assert result.status == NG
    assert "最大 4 名" in result.message


def test_正しいプリセットなら算手法は適合する() -> None:
    report = audit_facility(spec(standard=local_rules.get_standard(FACILITY_KEY)))
    assert check(report, "standard_mode").status == OK


# ---------------------------------------------------------------------------
# 2. 必要保育従事者数（常勤換算）
# ---------------------------------------------------------------------------


def test_必要数は要綱の算式に一致する() -> None:
    report = audit_facility(spec())
    result = check(report, "headcount")
    # 乳児8→2.6／1・2歳児12→2.0／3歳児4→0.2／4歳以上児6→0.2 合計5.0＋1＝6.0 → 6名
    assert result.required == "6 名以上"


def test_常勤換算で足りなければ不適合になる() -> None:
    thin = spec(
        capacity=20,
        monthly_children={
            AgeClass.INFANT: 12,
            AgeClass.AGE_1: 10,
            AgeClass.AGE_2: 10,
            AgeClass.AGE_3: 20,
        },
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("S1", "F", (Role.SHIENSHIIN,), 20.0, True),
        ),
    )
    result = check(audit_facility(thin), "headcount")
    assert result.status == NG
    assert "不足しています" in result.message


def test_短時間勤務者は8時間換算される() -> None:
    """週20時間の支援員は 2.5 人と数える（1人とは数えない）。"""
    record = StaffRecord("S1", "F", (Role.SHIENSHIIN,), 20.0, True)
    assert record.full_time_equivalent() == 2.5
    assert StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0).full_time_equivalent() == 5.0
    assert StaffRecord("X", "Y", (), 0.0).full_time_equivalent() == 0.0
    assert StaffRecord("X", "Y", (), -10.0).full_time_equivalent() == 0.0


def test_調理員は保育従事者数に含めない() -> None:
    report = audit_facility(spec())
    result = check(report, "headcount")
    assert "保育士 20.00 人" in result.actual
    assert "調理員" not in result.actual


def test_日極めの平均加算を足せる() -> None:
    """指導監督基準 第1(1) の「日々の平均的な人員を加える」。"""
    base = spec(monthly_children={AgeClass.INFANT: 8})
    plus = spec(
        monthly_children={AgeClass.INFANT: 8},
        average_daily_children={AgeClass.INFANT: 6},
    )
    assert (
        check(audit_facility(base), "headcount").required
        != check(audit_facility(plus), "headcount").required
    )
    assert spec(monthly_children={AgeClass.INFANT: 8}).total_children() == 8
    assert (
        spec(
            monthly_children={AgeClass.INFANT: 8},
            average_daily_children={AgeClass.INFANT: 6},
        ).total_children()
        == 14
    )


def test_在園児がいなければ該当なし() -> None:
    empty = spec(monthly_children={}, staff=())
    report = audit_facility(empty)
    assert check(report, "headcount").status == NA
    assert check(report, "min_two").status == NA


# ---------------------------------------------------------------------------
# 3. 保育士比率・みなし保育士
# ---------------------------------------------------------------------------


def test_保育士比率が半分を下回ると不適合になる() -> None:
    weak = spec(
        capacity=20,
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("S1", "F", (Role.SHIENSHIIN,), 40.0, True),
            StaffRecord("S2", "G", (Role.SHIENSHIIN,), 40.0, True),
        ),
    )
    result = check(audit_facility(weak), "qualified_ratio")
    assert result.status == NG
    assert result.required == "2分の1以上"


def test_保育事業者型は四分の三が基準になる() -> None:
    ratio = spec().qualified_ratio_required()
    assert ratio == 0.5
    shared = spec(is_shared_operator=True, standard=local_rules.get_standard(SHARED_KEY))
    assert shared.qualified_ratio_required() == 0.75
    result = check(audit_facility(shared), "qualified_ratio")
    assert result.required == "4分の3以上"


def test_保育事業者型でも定員20人未満は半数() -> None:
    """4分の3以上は「利用定員20名以上」の保育事業者型事業に限られる。

    定員20人未満の施設に 3/4 を課すと、法令上は適合している園を
    「不適合」と判定してしまう。共同利用枠（is_shared_operator）を
    立てたうえで定員だけを 20 人未満にして、定員ゲートが機能する
    ことを固定する。
    """
    small_shared = spec(
        capacity=10,
        is_shared_operator=True,
        standard=local_rules.get_standard(SHARED_KEY),
    )
    assert small_shared.qualified_ratio_required() == 0.5
    assert check(audit_facility(small_shared), "qualified_ratio").required == "2分の1以上"


def test_定員20人以上の単独認可外は半数のまま() -> None:
    """定員が 20 人でも、共同利用枠でなければ 3/4 にはならないこと。"""
    assert spec(capacity=25).qualified_ratio_required() == 0.5


def test_看護師は1人にしか数えない() -> None:
    """看護師2名でも保育士換算は1名。"""
    two_nurses = spec(
        capacity=20,
        monthly_children={AgeClass.INFANT: 12},
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("N1", "E", (Role.KANGSHI,), 40.0),
            StaffRecord("N2", "F", (Role.KANGSHI,), 40.0),
            StaffRecord("S1", "G", (Role.SHIENSHIIN,), 40.0, True),
        ),
    )
    result = check(audit_facility(two_nurses), "qualified_count")
    assert "みなし保育士として看護師は最大 1 人を計上" in result.actual
    # 保育士 1 人（5.0）＋看護師 1 人分（5.0）＝6.0。看護師2人目は数えない。
    assert "保育士 6.00 人" in result.actual


def test_指導監督基準では看護師に上限がない() -> None:
    """認可外保育施設指導監督基準 第1(2) は看護師を比率の分子にそのまま入れる。"""
    many_nurses = spec(
        regulation=Regulation.UNLICENSED_HOIKUSHI,
        standard=local_rules.get_standard("認可外保育施設（指導監督基準）"),
        capacity=40,
        staff=(
            StaffRecord("N1", "E", (Role.KANGSHI,), 40.0),
            StaffRecord("N2", "F", (Role.KANGSHI,), 40.0),
            StaffRecord("S1", "G", (Role.SHIENSHIIN,), 40.0, True),
        ),
    )
    result = check(audit_facility(many_nurses), "qualified_count")
    assert "看護師・准看護師は比率の分子にそのまま計上" in result.actual
    # 看護師 2 名（週40時間×2 → 10.0）。支援員は数えない。
    assert "保育士 10.00 人" in result.actual


def test_みなし保育士の上限は別制度で効く() -> None:
    """同じ看護師2名でも、企業主導型（みなし1人）では10.0→6.0 になる。"""
    base = local_rules.get_standard("認可外保育施設（指導監督基準）")
    staff = (
        StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
        StaffRecord("N1", "E", (Role.KANGSHI,), 40.0),
        StaffRecord("N2", "F", (Role.KANGSHI,), 40.0),
    )
    capped = replace(base, qualified_extra_roles=frozenset(), nurse_as_qualified_cap=1)
    for standard, expected, note in (
        (base, "保育士 15.00 人", "比率の分子にそのまま計上"),
        (capped, "保育士 6.00 人", "みなし保育士として看護師は最大 1 人を計上"),
    ):
        result = check(
            audit_facility(
                spec(
                    regulation=Regulation.UNLICENSED_HOIKUSHI,
                    standard=standard,
                    staff=staff,
                )
            ),
            "qualified_count",
        )
        assert expected in result.actual, standard.name
        assert note in result.actual, standard.name


def test_認可外以外は比率判定を対象外にする() -> None:
    licensed = spec(
        regulation=Regulation.LICENSED_HOIKUSHI,
        standard=local_rules.get_standard("全国基準（厚労省）"),
    )
    report = audit_facility(licensed)
    assert check(report, "qualified_ratio").status == NA
    assert check(report, "local_quota").status == NA
    assert check(report, "main_hours").status == NA
    assert check(report, "filing").status == NA


# ---------------------------------------------------------------------------
# 4. 必置職員（嘱託医・調理員）
# ---------------------------------------------------------------------------


def test_嘱託医と調理員は未確認になる() -> None:
    """機械では判れないので「適合」にしてはいけない。"""
    report = audit_facility(spec(has_contract_doctor=None, cooking_outsourced=None))
    assert check(report, "contract_doctor").status == UNKNOWN
    assert check(report, "cook").status == UNKNOWN
    assert report.is_filing_ready is False


def test_調理員がいなければ不適合になる() -> None:
    report = audit_facility(spec(staff=tuple(s for s in spec().staff if not s.is_chuubou)))
    assert check(report, "cook").status == NG


def test_調理委託なら調理員は免除される() -> None:
    report = audit_facility(
        spec(staff=tuple(s for s in spec().staff if not s.is_chuubou), cooking_outsourced=True)
    )
    assert check(report, "cook").status == NA


def test_食事搬入でも調理員は免除される() -> None:
    report = audit_facility(
        spec(staff=tuple(s for s in spec().staff if not s.is_chuubou), meals_imported=True)
    )
    assert check(report, "cook").status == NA


def test_嘱託医がいなければ不適合になる() -> None:
    assert check(audit_facility(spec(has_contract_doctor=False)), "contract_doctor").status == NG


def test_保育従事者の資格が未確認なら不適合になる() -> None:
    """研修修了か受講予定かが不明な職員は数えられない。"""
    unknown = spec(
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("S1", "F", (Role.SHIENSHIIN,), 40.0, None),
        ),
    )
    result = check(audit_facility(unknown), "staff_qualification")
    assert result.status == NG
    assert "受講予定" in result.message


def test_研修受講予定の者は数えられる() -> None:
    planned = spec(
        capacity=20,
        monthly_children={AgeClass.INFANT: 12},
        staff=(
            StaffRecord("H1", "A", (Role.HOIKUSHI,), 40.0),
            StaffRecord("S1", "F", (Role.SHIENSHIIN,), 20.0, None),
            StaffRecord("S2", "G", (Role.SHIENSHIIN,), 20.0, None),
        ),
    )
    result = check(audit_facility(planned), "staff_qualification")
    assert result.status == NG, "未受講の職員が1人以上いるので不適合が正しい"


# ---------------------------------------------------------------------------
# 5. 11時間ルール・地域枠・届出対象
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("opening", "closing", "expected"),
    [
        (time(7, 15), time(18, 15), OK),
        (time(7, 15), time(19, 30), OK),
        (time(7, 15), time(19, 30), NG),
    ],
)
def test_11時間を超える時間帯の常時2名(opening: time, closing: time, expected: str) -> None:
    staff = spec().staff if expected == OK else spec().staff[:1]
    report = audit_facility(spec(opening=opening, closing=closing, staff=staff))
    assert check(report, "main_hours").status == expected


def test_11時間以内なら超過ルールは適用されない() -> None:
    report = audit_facility(spec(opening=time(7, 15), closing=time(18, 15), staff=spec().staff[:1]))
    result = check(report, "main_hours")
    assert result.status == OK
    assert "適用されません" in result.message


def test_地域枠は総定員の半分まで() -> None:
    assert check(audit_facility(spec(local_quota_children=20)), "local_quota").status == OK
    result = check(audit_facility(spec(local_quota_children=21)), "local_quota")
    assert result.status == NG
    assert "50%を超えて" in result.message


def test_利用定員6人未満は届出対象外の扱いになる() -> None:
    report = audit_facility(spec(capacity=5))
    assert check(report, "filing").status == NA


def test_利用定員6人以上は届出対象になる() -> None:
    assert check(audit_facility(spec(capacity=6)), "filing").status == OK


# ---------------------------------------------------------------------------
# 6. 出力
# ---------------------------------------------------------------------------


def test_DataFrameとMarkdownに落とせる() -> None:
    report = audit_facility(spec())
    frame = to_dataframe(report)
    assert len(frame) == len(report.checks)
    assert set(frame.columns) == {"項目", "判定", "実測", "基準", "根拠", "内容"}
    text = to_markdown(report)
    assert "企業主導型保育事業 適合チェック" in text
    assert "### 未確認" in text
    assert "### 不適合" not in text


def test_不適合があるときはMarkdownにそれが出る() -> None:
    text = to_markdown(audit_facility(spec(local_quota_children=30)))
    assert "### 不適合" in text
    assert "地域枠" in text


def test_判定ラベルが日本語になる() -> None:
    from shiftai.compliance import STATUS_LABELS

    assert STATUS_LABELS[OK] == "適合"
    assert STATUS_LABELS[NG] == "不適合"
    assert STATUS_LABELS[NA] == "該当なし"
    assert STATUS_LABELS[UNKNOWN] == "未確認"


def test_制度とプリセットの対応が明示されている() -> None:
    assert Regulation.LICENSED_HOIKUSHI.preset_key == "全国基準（厚労省）"
    assert Regulation.CORPORATE_LED.preset_key == FACILITY_KEY
    assert Regulation.SMALL_SCALE.preset_key is None
    assert Regulation.SMALL_SCALE.is_unlicensed is True
    assert Regulation.LICENSED_HOIKUSHI.is_unlicensed is False


def test_プリセットが無い制度は明示しなければエラーになる() -> None:
    with pytest.raises(ValueError, match="プリセットがありません"):
        audit_facility(spec(regulation=Regulation.SMALL_SCALE))
