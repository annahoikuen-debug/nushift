"""自治体別ローカルルール／プリセット。

全国基準（厚生労働省）および主要自治体の配置基準を ``StaffingStandard`` として
保持し、UI の選択ボックス・比較表・設定説明カードに使う。

数値は一般的な実務水準に基づく目安であり、自治体ごとに運用が異なる。
**実際の運用では必ず自治体の告示・条例・要綱への個別確認を行うこと。**

対象制度（**このモジュールの適用範囲**）
--------------------------------------
本プリセットが扱うのは **認可保育所（児童福祉法第32条の認可）** の配置基準である。
``_LEGAL_BASE`` / ``_SUMMARIES`` / ``_SOURCES`` の出典はすべてこの制度の文書のみ。

次の制度は **法令の階層・算出手法・資格要件がいずれも異なる** ため、
プリセットを足すだけでは対応できない。

* **認可外保育施設**（企業主導型保育事業・小規模保育事業・事業所内保育事業など）
  - 根拠: 認可外保育施設指導監督基準／「企業主導型保育事業費補助金実施要綱」第3の2(4)
  - 算出手法: 年齢区分ごとに小数第2位以下を切り捨て → **合計に +1** → 小数第1位で四捨五入
    （本モジュール既定の「年齢クラス毎に切り上げ」とは別体系。
    ``StaffingStandard.headcount_mode="facility_formula"`` で切り替える）
  - 資格: 保育従事者の半数以上が保育士／利用定員20人以上の保育事業者型事業は4分の3以上
  - 11時間を超える時間帯は常時2人以上、嘱託医・調理員の必置、
    看護師のみなし保育士（1人に限り）などの固有要件がある

認可外保育施設向けのプリセットを既存ルールと同一の ``remarks`` 構造で追加すると、
**未検証の仮定値が制度の解釈として流通する**。追加する場合は必ず
``_SOURCES`` に要綱の条項番号まで書き、``_PRESET_EXTRA_NOTES`` に
「認可保育所とは異なる点」を明記すること。

法令の主な根拠（認可保育所）:

* 児童福祉法第18条の4（保育所の職員配置）
* 「保育所の職員配置基準」昭和52年厚生省告示第49号
* 労働基準法第9条（労働時間）・第32条の3（休息日）
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from datetime import time
from typing import Any

from shiftai import config
from shiftai.domain import AgeClass, AgeRatio, Role, StaffingStandard


def _t(hour: int, minute: int = 0) -> time:
    """時刻リテラルを短く書くためのヘルパ。

    モジュール先頭の定数（``_FACILITY_OPEN`` など）からも使うので、
    **定数ブロックより前**に定義する（後ろに置くと import 時に NameError）。
    """
    return time(hour, minute)


#: 認可外保育施設の主たる開所時間（11時間）。開所 7:15 なら 18:15 まで。
_FACILITY_MAIN_HOURS = 11
_FACILITY_OPEN = config.DEFAULT_EARLY_CARE_START
#: 企業主導型保育事業の実施要綱 第3の2(4)② の定員比。
#: 乳児3人:1人／満1歳以上満3歳未満6人:1人／満3歳以上満4歳未満20人:1人／満4歳以上30人:1人。
_FACILITY_RATIO_KWARGS: dict[str, float] = {
    "infant": 3.0,
    "age_1": 6.0,
    "age_2": 6.0,
    "age_3": 20.0,
    "age_4": 30.0,
    "age_5": 30.0,
}
#: 1・2歳児と4歳以上児は**合算**してから定員比を割る。
_FACILITY_AGE_GROUPS = (
    (AgeClass.INFANT,),
    (AgeClass.AGE_1, AgeClass.AGE_2),
    (AgeClass.AGE_3,),
    (AgeClass.AGE_4, AgeClass.AGE_5),
)
_FACILITY_SOURCE = (
    "「企業主導型保育事業費補助金実施要綱」第3の2(4)②（職員）／"
    "認可外保育施設指導監督基準（令和6年3月29日こ成保第206号）第1（保育に従事する者の数及び資格）"
)

__all__ = [
    "MUNICIPAL_PRESETS",
    "DEFAULT_PRESET_KEY",
    "SCOPE_STATEMENT",
    "LocalRuleNote",
    "default_ratios",
    "get_standard",
    "list_presets",
    "build_standard",
    "register_preset",
    "standard_to_dataframe",
    "local_rule_notes",
    "preset_summary",
    "preset_source",
    "headcount_mode_label",
    "qualified_mode_label",
]

_LEGAL_BASE = (
    "児童福祉法第18条の4（保育所の職員配置）／"
    "保育所の職員配置基準（昭和52年厚生省告示第49号）／"
    "労働基準法第9条（労働時間）・第32条の3（休息日）"
)

#: プリセットが**扱っていない制度**を明示する文言。
#: 認可外保育施設（企業主導型保育事業・小規模保育事業・事業所内保育事業）は
#: 算出手法・資格要件・必置職員がいずれも異なるため、認可保育所のプリセットを
#: 流用すると「満たしていると誤認する」危険がある。
SCOPE_STATEMENT = (
    "本プリセットは認可保育所（児童福祉法第32条の認可）の配置基準のみを扱う。"
    "認可外保育施設（企業主導型保育事業・小規模保育事業・事業所内保育事業）は"
    "算出手法・資格要件・必置職員が異なるため本プリセットを流用できない。"
)

_DISCLAIM = "（要確認。実際の運用では自治体基準への個別確認を推奨）"

_AGE_ORDER: tuple[AgeClass, ...] = (
    AgeClass.INFANT,
    AgeClass.AGE_1,
    AgeClass.AGE_2,
    AgeClass.AGE_3,
    AgeClass.AGE_4,
    AgeClass.AGE_5,
)


def default_ratios() -> dict[AgeClass, AgeRatio]:
    """全国基準（厚生省告示第49号）の定員比を新規辞書で返す。"""
    return {
        AgeClass.INFANT: AgeRatio(
            age_class=AgeClass.INFANT, children_per_staff=3.0, rounding="ceil"
        ),
        AgeClass.AGE_1: AgeRatio(age_class=AgeClass.AGE_1, children_per_staff=6.0, rounding="ceil"),
        AgeClass.AGE_2: AgeRatio(age_class=AgeClass.AGE_2, children_per_staff=6.0, rounding="ceil"),
        AgeClass.AGE_3: AgeRatio(age_class=AgeClass.AGE_3, children_per_staff=8.0, rounding="ceil"),
        AgeClass.AGE_4: AgeRatio(
            age_class=AgeClass.AGE_4, children_per_staff=20.0, rounding="ceil"
        ),
        AgeClass.AGE_5: AgeRatio(
            age_class=AgeClass.AGE_5, children_per_staff=20.0, rounding="ceil"
        ),
    }


def _ratios(
    *,
    infant: float,
    age_1: float,
    age_2: float,
    age_3: float,
    age_4: float,
    age_5: float,
) -> dict[AgeClass, AgeRatio]:
    """``default_ratios()`` のコピーへ指定した定員比を差し替える。"""
    ratios = default_ratios()
    for age_class, value in zip(
        _AGE_ORDER, (infant, age_1, age_2, age_3, age_4, age_5), strict=True
    ):
        ratios[age_class] = AgeRatio(
            age_class=age_class, children_per_staff=float(value), rounding="ceil"
        )
    return ratios


# ---------------------------------------------------------------------------
# プリセット本体
# ---------------------------------------------------------------------------


MUNICIPAL_PRESETS: dict[str, StaffingStandard] = {
    "全国基準（厚労省）": StaffingStandard(
        name="全国基準（厚労省）",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=20.0, age_5=20.0),
        min_staff_per_room=2,
        min_qualified_ratio=1.0,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, config.DEFAULT_LATE_CARE_END),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=False,
        remarks=(
            "保育標準時間（8時間30分〜17時15分）を基本とし、早朝7:15〜8:30／延長17:15〜19:30は"
            "全時間帯保育士を配置。延長保育の代替措置は用いない。"
            "出典: 保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針" + _DISCLAIM
        ),
    ),
    "東京都": StaffingStandard(
        name="東京都",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "3歳児8:1、4・5歳児12:1。延長保育（17:15〜18:30）は「保育士1名＋他資格者」による"
            "代替措置を認める。4・5歳児12:1は東京都独自の厳しい定員比である。"
            "出典: 東京都「保育所の職員配置基準」東京都告示・東京都保育已基本方針" + _DISCLAIM
        ),
    ),
    "横浜市": StaffingStandard(
        name="横浜市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(19, 0)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(19, 0),
        is_short_time_only=False,
        remarks=(
            "東京都系の定員比（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児12:1）を採用し、"
            "延長保育は19:00まで代替措置を認める（延長は最大2時間程度を想定）。"
            "出典: 横浜市「保育所の職員配置に関する基準」横浜市告示・横浜市保育已基本方針"
            + _DISCLAIM
        ),
    ),
    "大阪市": StaffingStandard(
        name="大阪市",
        ratios=_ratios(infant=4.0, age_1=5.0, age_2=7.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "0歳児4:1、1歳児5:1、2歳児7:1と全国基準より厳しい年齢クラスがある。"
            "延長保育（17:15〜18:30）は「保育士1名＋他資格者」による代替措置を認める。"
            "出典: 大阪市「保育所の職員配置基準」大阪市告示・大阪市保育已基本方針" + _DISCLAIM
        ),
    ),
    "福岡市": StaffingStandard(
        name="福岡市",
        ratios=_ratios(infant=3.0, age_1=4.0, age_2=6.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, config.DEFAULT_LATE_CARE_END),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "保育園型利用者支援（保育を必要とする障害児支援）を行う施設を想定。1歳児4:1、"
            "4・5歳児12:1。延長保育は18:30まで「保育士1名＋子育て支援員／幼稚園教諭／"
            "配置的保育支援員」の代替を認め、18:30以降は保育士のみを配置する。"
            "出典: 福岡市「保育所の職員配置基準」福岡市告示・福岡市保育已基本方針" + _DISCLAIM
        ),
    ),
    "名古屋市": StaffingStandard(
        name="名古屋市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=20.0, age_5=20.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(19, 0)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(19, 0),
        is_short_time_only=False,
        remarks=(
            "定員比は全国基準準拠（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児20:1）。"
            "延長保育は19:00まで「保育士1名＋他資格者」の代替措置を認める。"
            "出典: 名古屋市「保育所の職員配置基準」名古屋市告示・名古屋市保育已基本方針" + _DISCLAIM
        ),
    ),
    "京都市": StaffingStandard(
        name="京都市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=20.0, age_5=20.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児20:1。"
            "延長保育（17:15〜18:30）は「保育士1名＋他資格者」による代替措置を認める。"
            "出典: 京都市「保育所の職員配置基準」京都市告示・京都市保育已基本方針" + _DISCLAIM
        ),
    ),
    "札幌市": StaffingStandard(
        name="札幌市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=20.0, age_5=20.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=False,
        remarks=(
            "定員比は全国基準準拠。延長保育（17:15〜18:30）の代替措置については"
            "本プリセットでは「緩和なし」と仮定している（要確認）。"
            "出典: 札幌市「保育所の職員配置基準」札幌市告示・札幌市保育已基本方針" + _DISCLAIM
        ),
    ),
    "神戸市": StaffingStandard(
        name="神戸市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "東京都系（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児12:1）を採用。"
            "延長保育（17:15〜18:30）は「保育士1名＋他資格者」による代替措置を認める。"
            "出典: 神戸市「保育所の職員配置基準」神戸市告示・神戸市保育已基本方針" + _DISCLAIM
        ),
    ),
    "川崎市": StaffingStandard(
        name="川崎市",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=12.0, age_5=12.0),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.DEFAULT_EARLY_CARE_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(config.DEFAULT_LATE_CARE_START, _t(18, 30)),
        late_care_relaxed=True,
        late_care_min_qualified=1,
        late_care_after_relax_time=_t(18, 30),
        is_short_time_only=False,
        remarks=(
            "東京都系（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児12:1）を採用。"
            "延長保育（17:15〜18:30）は「保育士1名＋他資格者」による代替措置を認める。"
            "出典: 川崎市「保育所の職員配置基準」川崎市告示・川崎市保育已基本方針" + _DISCLAIM
        ),
    ),
    "保育標準時間のみ園": StaffingStandard(
        name="保育標準時間のみ園",
        ratios=_ratios(infant=3.0, age_1=6.0, age_2=6.0, age_3=8.0, age_4=20.0, age_5=20.0),
        min_staff_per_room=2,
        min_qualified_ratio=1.0,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=config.STANDARD_TIME_START,
        standard_time=(config.STANDARD_TIME_START, config.STANDARD_TIME_END),
        early_care_window=(config.STANDARD_TIME_START, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(_t(17, 0), _t(17, 0)),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=True,
        remarks=(
            "短時間保育（3時間保育）園。保育標準時間8:30〜17:15のみを扱い、早朝保育・"
            "延長保育の窓口は空区間（未設定）とする。保育標準時間は法定11時間のうち"
            "8時間45分であり、短時間保育児はこの時間帯の在園者数にだけ計上する。"
            "出典: 保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針" + _DISCLAIM
        ),
    ),
    "認可外保育施設（指導監督基準）": StaffingStandard(
        name="認可外保育施設（指導監督基準）",
        ratios=_ratios(**_FACILITY_RATIO_KWARGS),
        min_staff_per_room=2,
        min_qualified_ratio=1.0 / 3.0,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=_FACILITY_OPEN,
        standard_time=(_FACILITY_OPEN, _t(_FACILITY_OPEN.hour + _FACILITY_MAIN_HOURS, 15)),
        early_care_window=(_FACILITY_OPEN, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(_t(18, 15), config.DEFAULT_LATE_CARE_END),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=False,
        headcount_mode="facility_formula",
        headcount_extra=1,
        age_groups=_FACILITY_AGE_GROUPS,
        qualified_mode="ratio",
        min_qualified_floor=1,
        nurse_as_qualified_cap=0,
        qualified_extra_roles=frozenset({Role.KANGSHI}),
        remarks=(
            "認可外保育施設のうち、助成金の対象でないもの（小規模保育事業・"
            "事業所内保育事業・認定こども園型でない単独施設など）の基準。"
            "乳児3:1、1・2歳児6:1、3歳児20:1、4歳以上児30:1 の合計に1を加え、"
            "各区分は小数第2位以下を切り捨ててから合計し、小数第1位で四捨五入する。"
            "保育従事者の**おおむね3分の1以上**が保育士・准看護師・看護師。"
            "この基準では看護師は「1人に限るみなし保育士」ではなく"
            "比率の分子にそのまま入る（企業主導型保育事業と異なる点）。"
            "嘱託医・調理員の必置、常勤換算、月極めの基礎乳幼児数による確認は "
            "compliance.py のチェックリストで行う。"
            "出典: 認可外保育施設指導監督基準（令和6年3月29日こ成保第206号）"
            "第1(1)・(2)" + _DISCLAIM
        ),
    ),
    "企業主導型保育事業（単独枠）": StaffingStandard(
        name="企業主導型保育事業（単独枠）",
        ratios=_ratios(**_FACILITY_RATIO_KWARGS),
        min_staff_per_room=2,
        min_qualified_ratio=0.5,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=_FACILITY_OPEN,
        # 認可外保育施設に保育標準時間は無い。開所〜開所+11時間を
        # 「主たる開所時間」として扱う（認可外保育施設指導監督基準 第1(1)）。
        standard_time=(_FACILITY_OPEN, _t(_FACILITY_OPEN.hour + _FACILITY_MAIN_HOURS, 15)),
        early_care_window=(_FACILITY_OPEN, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(_t(18, 15), config.DEFAULT_LATE_CARE_END),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=False,
        # 認可外の算出手法（要綱 第3の2(4)②）
        headcount_mode="facility_formula",
        headcount_extra=1,
        age_groups=_FACILITY_AGE_GROUPS,
        qualified_mode="ratio",
        min_qualified_floor=1,
        nurse_as_qualified_cap=1,
        remarks=(
            "認可外保育施設（企業主導型保育事業・単独枠）。"
            "乳児3:1、1・2歳児6:1、3歳児20:1、4歳以上児30:1 の合計に1を加え、"
            "各区分は小数第2位以下を切り捨ててから合計し、小数第1位で四捨五入する。"
            "1・2歳児と4歳以上児は合算してから割る。"
            "保育従事者の半数以上が保育士（看護師・准看護師は1人に限り保育士とみなせる）。"
            "在園児がいる時間帯は最低2名、うち1名以上は保育士。"
            "開所7:15〜18:15を主たる開所時間（11時間）とし、それ以降は常時2人以上。"
            "保育標準時間の制度（認可保育所）ではなく、保育標準時間認定は使わない。"
            "嘱託医・調理員の必置、常勤換算、月極めの基礎乳幼児数による確認は "
            "compliance.py のチェックリストで行う。"
            "出典: " + _FACILITY_SOURCE + _DISCLAIM
        ),
    ),
    "企業主導型保育事業（保育事業者型・20名以上）": StaffingStandard(
        name="企業主導型保育事業（保育事業者型・20名以上）",
        ratios=_ratios(**_FACILITY_RATIO_KWARGS),
        min_staff_per_room=2,
        min_qualified_ratio=0.75,
        break_minutes=config.DEFAULT_BREAK_MINUTES,
        break_stagger_minutes=config.DEFAULT_BREAK_STAGGER_MINUTES,
        work_start_base=_FACILITY_OPEN,
        standard_time=(_FACILITY_OPEN, _t(_FACILITY_OPEN.hour + _FACILITY_MAIN_HOURS, 15)),
        early_care_window=(_FACILITY_OPEN, config.DEFAULT_EARLY_CARE_END),
        late_care_window=(_t(18, 15), config.DEFAULT_LATE_CARE_END),
        late_care_relaxed=False,
        late_care_min_qualified=1,
        late_care_after_relax_time=None,
        is_short_time_only=False,
        headcount_mode="facility_formula",
        headcount_extra=1,
        age_groups=_FACILITY_AGE_GROUPS,
        qualified_mode="ratio",
        min_qualified_floor=1,
        nurse_as_qualified_cap=1,
        remarks=(
            "認可外保育施設（企業主導型保育事業・利用定員20人以上の保育事業者型事業＝共同利用枠）。"
            "定員比と算出手法は単独枠と同じだが、"
            "**保育従事者の4分の3以上を保育士とする**（単独枠は半数以上）。"
            "令和元年度までに助成決定を受けた施設の経過措置（半数で可）は"
            "令和4年度末に終了しているため適用しない。"
            "出典: " + _FACILITY_SOURCE + _DISCLAIM
        ),
    ),
}

DEFAULT_PRESET_KEY: str = "全国基準（厚労省）"


# ---------------------------------------------------------------------------
# プリセット一覧・参照
# ---------------------------------------------------------------------------

_SUMMARIES: dict[str, str] = {
    "全国基準（厚労省）": "厚労省告示第49号の標準。4・5歳児20:1・延長は保育士のみ",
    "東京都": "東京都告示。4・5歳児12:1、延長18:30まで代替措置可",
    "横浜市": "東京都系＋延長19:00まで代替措置可",
    "大阪市": "0歳児4:1・1歳児5:1・2歳児7:1と厳しめ、延長18:30まで代替措置可",
    "福岡市": "1歳児4:1、延長18:30まで保育士1名＋他資格者で代替可（19:00以降は保育士のみ）",
    "名古屋市": "全国基準の定員比＋延長19:00まで代替措置可",
    "京都市": "全国基準の定員比＋延長18:30まで代替措置可",
    "札幌市": "全国基準の定員比。代替措置は「なし」と仮定（要確認）",
    "神戸市": "東京都系（3歳児8:1、4・5歳児12:1）、延長18:30まで代替措置可",
    "川崎市": "東京都系（3歳児8:1、4・5歳児12:1）、延長18:30まで代替措置可",
    "保育標準時間のみ園": "3時間保育園。8:30〜17:15のみ、早朝・延長の窓口なし",
    "認可外保育施設（指導監督基準）": (
        "認可外保育施設。3:1/6:1/20:1/30:1＋1。3分の1以上が保育士・看護師"
    ),
    "企業主導型保育事業（単独枠）": ("認可外保育施設。3:1/6:1/20:1/30:1＋1。保育士は半数以上"),
    "企業主導型保育事業（保育事業者型・20名以上）": (
        "認可外保育施設・保育事業者型。保育士は4分の3以上"
    ),
}

_SOURCES: dict[str, str] = {
    "全国基準（厚労省）": "保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針",
    "東京都": "東京都「保育所の職員配置基準」告示／東京都保育已基本方針",
    "横浜市": "横浜市「保育所の職員配置に関する基準」告示／横浜市保育已基本方針",
    "大阪市": "大阪市「保育所の職員配置基準」告示／大阪市保育已基本方針",
    "福岡市": "福岡市「保育所の職員配置基準」告示／福岡市保育已基本方針",
    "名古屋市": "名古屋市「保育所の職員配置基準」告示／名古屋市保育已基本方針",
    "京都市": "京都市「保育所の職員配置基準」告示／京都市保育已基本方針",
    "札幌市": "札幌市「保育所の職員配置基準」告示／札幌市保育已基本方針",
    "神戸市": "神戸市「保育所の職員配置基準」告示／神戸市保育已基本方針",
    "川崎市": "川崎市「保育所の職員配置基準」告示／川崎市保育已基本方針",
    "保育標準時間のみ園": "保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針",
    "認可外保育施設（指導監督基準）": (
        "認可外保育施設指導監督基準（令和6年3月29日こ成保第206号）第1"
    ),
    "企業主導型保育事業（単独枠）": _FACILITY_SOURCE,
    "企業主導型保育事業（保育事業者型・20名以上）": _FACILITY_SOURCE,
}


def _window_text(window: tuple[time, time]) -> str:
    start, end = window
    if start == end:
        return "設定なし"
    return f"{start.strftime('%H:%M')}〜{end.strftime('%H:%M')}"


_HEADCOUNT_MODE_LABELS: dict[str, str] = {
    "per_class": "年齢クラス毎に切り上げ（認可保育所）",
    "facility_formula": "区分ごとに小数第2位以下切捨て→合計→＋1→四捨五入（認可外保育施設）",
}
_QUALIFIED_MODE_LABELS: dict[str, str] = {
    "per_class": "必要人員＝必要保育士数（認可保育所）",
    "ratio": "必要人員に対する保育士比率で決める（認可外保育施設）",
}


def headcount_mode_label(standard: StaffingStandard) -> str:
    """必要人員の算出手法を日本語で返す（比較表・UI 用）。"""
    return _HEADCOUNT_MODE_LABELS.get(standard.headcount_mode, standard.headcount_mode)


def qualified_mode_label(standard: StaffingStandard) -> str:
    """必要保育士数の決め方を日本語で返す（比較表・UI 用）。"""
    return _QUALIFIED_MODE_LABELS.get(standard.qualified_mode, standard.qualified_mode)


def _derived_summary(standard: StaffingStandard) -> str:
    ratios = "、".join(
        f"{age_class.value}{standard.ratio_for(age_class).children_per_staff:g}:1"
        for age_class in _AGE_ORDER
        if age_class in standard.ratios
    )
    relax = "延長は代替措置可" if standard.late_care_relaxed else "延長は保育士のみ"
    return f"保育標準時間{_window_text(standard.standard_time)}／{ratios}／{relax}"


def preset_summary(standard: StaffingStandard) -> str:
    """プリセットの1行サマリーを返す。"""
    for key, value in MUNICIPAL_PRESETS.items():
        if value is standard or value.name == standard.name:
            return _SUMMARIES.get(key, _derived_summary(value))
    return _derived_summary(standard)


def preset_source(standard: StaffingStandard) -> str:
    """プリセットの出典典拠を返す。"""
    for key, value in MUNICIPAL_PRESETS.items():
        if value is standard or value.name == standard.name:
            return _SOURCES.get(key, value.remarks)
    return standard.remarks


def get_standard(key: str) -> StaffingStandard:
    """プリセットキーから ``StaffingStandard`` を取得する。不明キーは ``KeyError``。"""
    try:
        return MUNICIPAL_PRESETS[key]
    except KeyError as exc:
        raise KeyError(
            f"不明なプリセットキーです: {key!r}（選択肢: {', '.join(MUNICIPAL_PRESETS)}）"
        ) from exc


def list_presets() -> list[dict]:
    """UI の selectbox 用のプリセット一覧を返す。

    各要素は ``{"key", "name", "summary", "source"}`` の辞書。
    """
    out: list[dict] = []
    for key, standard in MUNICIPAL_PRESETS.items():
        out.append(
            {
                "key": key,
                "name": standard.name,
                "summary": _SUMMARIES.get(key, _derived_summary(standard)),
                "source": _SOURCES.get(key, standard.remarks),
            }
        )
    return out


def _normalize_ratios(value: Any) -> dict[AgeClass, AgeRatio]:
    """``ratios`` オーバーライドを ``{AgeClass: AgeRatio}`` に正規化する。"""
    if not isinstance(value, Mapping):
        raise TypeError("ratios オーバーライドは AgeClass をキーにした Mapping で指定してください")
    result: dict[AgeClass, AgeRatio] = {}
    for key, raw in value.items():
        age_class = key if isinstance(key, AgeClass) else AgeClass(key)
        if isinstance(raw, AgeRatio):
            result[age_class] = raw
        else:
            result[age_class] = AgeRatio(
                age_class=age_class, children_per_staff=float(raw), rounding="ceil"
            )
    return result


def build_standard(key: str, overrides: dict | None = None) -> StaffingStandard:
    """プリセットに園ごとの上乗せを適用した ``StaffingStandard`` を返す。

    ``overrides`` の主なキー:

    * ``ratios``: ``{AgeClass: float}`` または ``{AgeClass: AgeRatio}``
      （差分のみ渡すと既存比を保ったまま併合される）
    * ``min_staff_per_room`` / ``late_care_relaxed`` / ``late_care_min_qualified``
      / ``min_qualified_ratio`` / ``is_short_time_only`` / ``name`` / ``remarks`` など
      ``StaffingStandard`` の任意フィールド

    指定外のフィールド名は ``KeyError``。
    """
    standard = get_standard(key)
    if not overrides:
        return standard
    valid = {f.name for f in fields(StaffingStandard)}
    unknown = [k for k in overrides if k not in valid]
    if unknown:
        raise KeyError(f"StaffingStandard に無い項目です: {unknown}")
    patch: dict[str, Any] = {}
    for name, value in overrides.items():
        if name == "ratios":
            base = dict(standard.ratios)
            base.update(_normalize_ratios(value))
            patch["ratios"] = base
        else:
            patch[name] = value
    return replace(standard, **patch)


def register_preset(
    key: str,
    standard: StaffingStandard,
    *,
    summary: str | None = None,
    source: str | None = None,
) -> None:
    """プリセットを追加・上書き登録する。UI からの動的登録にも使う。"""
    MUNICIPAL_PRESETS[key] = standard
    _SUMMARIES[key] = summary or _derived_summary(standard)
    _SOURCES[key] = source or standard.remarks


def standard_to_dataframe() -> Any:
    """プリセット一覧の比較表 DataFrame を返す。"""
    import pandas as pd

    records = []
    for key, standard in MUNICIPAL_PRESETS.items():
        record: dict[str, Any] = {
            "キー": key,
            "名称": standard.name,
        }
        for age_class in _AGE_ORDER:
            ratio = standard.ratios.get(age_class)
            record[age_class.value] = ratio.children_per_staff if ratio is not None else None
        record.update(
            {
                "保育標準時間": _window_text(standard.standard_time),
                "早朝保育": _window_text(standard.early_care_window),
                "延長保育": _window_text(standard.late_care_window),
                "延長緩和措置": "可" if standard.late_care_relaxed else "不可",
                "延長最低保育士数": standard.late_care_min_qualified,
                "延長緩和の期限": (
                    standard.late_care_after_relax_time.strftime("%H:%M")
                    if standard.late_care_after_relax_time is not None
                    else "—"
                ),
                "最低配置人数": standard.min_staff_per_room,
                "保育士割合の目安": standard.min_qualified_ratio,
                "必要保育士数の決め方": _QUALIFIED_MODE_LABELS[standard.qualified_mode],
                "必要人員の算出手法": _HEADCOUNT_MODE_LABELS[standard.headcount_mode],
                "合計に加える定数": standard.headcount_extra,
                "看護師のみなし保育士の上限": standard.nurse_as_qualified_cap,
                "休憩(分)": standard.break_minutes,
                "短時間保育のみ園": "はい" if standard.is_short_time_only else "いいえ",
                "出典": _SOURCES.get(key, standard.remarks),
                "備考": standard.remarks,
            }
        )
        records.append(record)
    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# 自治体別ローカルルール（設定説明カード）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalRuleNote:
    """自治体のローカルルールを1枚のカードにした説明。"""

    key: str
    title: str
    detail: str
    source: str
    legal_reference: str


def _note_hours(standard: StaffingStandard, source: str) -> LocalRuleNote:
    detail = (
        f"保育標準時間: {_window_text(standard.standard_time)}\n\n"
        f"早朝保育（保育標準時間前）: {_window_text(standard.early_care_window)}\n\n"
        f"延長保育（保育標準時間外）: {_window_text(standard.late_care_window)}\n\n"
        f"園の開所〜閉所の時間帯を{standard.standard_time[0].strftime('%H:%M')}起点の"
        f"30分単位時間帯へ分割し、各時間帯ごとに必要人員を算出する。"
    )
    if standard.is_short_time_only:
        detail += (
            "\n\n本園は短時間保育（3時間保育）園のため、早朝保育・延長保育の窓口は"
            "設けない（設定なし）。"
        )
    return LocalRuleNote(
        key="hours",
        title="適用時間帯",
        detail=detail,
        source=source,
        legal_reference=(
            "児童福祉法第18条の4／保育所の職員配置基準（昭和52年厚生省告示第49号）／保育所保育指針"
        ),
    )


def _note_headcount(standard: StaffingStandard, source: str) -> LocalRuleNote:
    lines = [
        f"{age_class.value}: 在園児{a}人 ÷ {b:g} = {standard.headcount_for(age_class, a)}名"
        for age_class, a in ((ac, 10) for ac in _AGE_ORDER)
        if age_class in standard.ratios
        for b in [standard.ratio_for(age_class).children_per_staff]
    ]
    detail = (
        "必要人員は「定員比（園児:職員）」で求め、割り上げ（切り上げ）て整数名とする。\n\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n\n（在園児0名の時間帯は行を作らない。計算例は在園児10名の場合。）"
    )
    return LocalRuleNote(
        key="headcount",
        title="必要人数の算定方法（定員比の分子/分母）",
        detail=detail,
        source=source,
        legal_reference=(
            "保育所の職員配置基準（昭和52年厚生省告示第49号）"
            "（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児20:1が標準）"
        ),
    )


def _note_late_care(standard: StaffingStandard, source: str) -> LocalRuleNote:
    if not standard.late_care_relaxed:
        detail = (
            f"延長保育（{_window_text(standard.late_care_window)}）は"
            "「保育士1名＋他資格者」による代替措置を用いず、"
            "必要人員すべてを保育士（必要なら幼稚園教諭）とする。\n\n"
            "このため必要保育士数＝必要人員数となる。"
        )
    else:
        after = (
            f"{standard.late_care_after_relax_time.strftime('%H:%M')}以降は"
            "緩和措置を使わず、保育士のみで満たす（延長保育（緩和措置なし）として扱う）。"
            if standard.late_care_after_relax_time is not None
            else "緩和措置の上限時刻は設けない。"
        )
        detail = (
            f"延長保育（{_window_text(standard.late_care_window)}）では"
            "「保育士1名＋他資格者」で代替できる。必要保育士数は"
            f"必要人員の半数（切り上げ、最低{standard.late_care_min_qualified}名）とする。\n\n"
            "代替できる他資格者は次のいずれか。\n"
            "- 子育て支援員（幼稚園教諭・保育士課程の修了者を除く）\n"
            "- 幼稚園教諭（2歳児クラスの担任経験がある者）\n"
            "- 配置的保育支援員（保育補助者）\n\n"
            f"{after}"
        )
    return LocalRuleNote(
        key="late_care",
        title="延長保育の代替要件",
        detail=detail,
        source=source,
        legal_reference=(
            "保育所の職員配置基準（昭和52年厚生省告示第49号）"
            "（法第18条の4第2項の規定の読み替えにより適用する基準）／"
            "児童福祉法第18条の4"
        ),
    )


def _note_standard_time(standard: StaffingStandard, source: str) -> LocalRuleNote:
    start = standard.standard_time[0]
    end = standard.standard_time[1]
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    detail = (
        f"保育標準時間は{start.strftime('%H:%M')}〜{end.strftime('%H:%M')}"
        f"（{minutes // 60}時間{minutes % 60}分＝法定11時間のうち8時間45分）。\n\n"
        "この時間帯が保育標準時間であり、園児数（定員比の分子）は"
        "この時間帯の在園者数を使う。早朝保育・延長保育は保育標準時間外の扱いとなり、"
        "同一基準でも時間帯により必要人員が変わることがある。"
    )
    return LocalRuleNote(
        key="standard_time",
        title="保育標準時間の定義",
        detail=detail,
        source=source,
        legal_reference="児童福祉法第18条の4／保育所の職員配置基準／保育所保育指針",
    )


def _note_min_two(standard: StaffingStandard, source: str) -> LocalRuleNote:
    detail = (
        f"在園児が1名でもいる時間帯は、保育室に最低{standard.min_staff_per_room}名を"
        "配置する（2名ルール）。定員比で算出した合計が下回る場合、"
        "最も年少の年齢クラスの行を加算して底上げする。\n\n"
        "そのため、園児が1名しかいない時間帯でも必要人員は"
        f"{standard.min_staff_per_room}名になる。"
    )
    return LocalRuleNote(
        key="min_two",
        title="2名ルール（保育室の最低配置人数）",
        detail=detail,
        source=source,
        legal_reference="児童福祉法第18条の4第1項第2号／保育所の職員配置基準",
    )


def _note_short_time(standard: StaffingStandard, source: str) -> LocalRuleNote:
    if standard.is_short_time_only:
        detail = (
            "本園は保育標準時間のみ（3時間保育）の短時間保育園。\n\n"
            "- 保育標準時間（8:30〜17:15）以外の在園は受け入れない。\n"
            "- 園児は全て短時間保育児（is_short_time=True）として扱い、"
            "保育標準時間帯の在園者数にだけ計上する。\n"
            "- 早朝保育・延長保育の窓口は空区間（未設定）とし、"
            "延長保育の代替措置も用いない。"
        )
    else:
        detail = (
            "本園は保育標準時間を超えた開所を行うため、短時間保育児"
            "（is_short_time=True）を含む園児のうち、"
            "保育標準時間のみを利用する園児は"
            "保育標準時間帯の在園者数にだけ計上する。\n\n"
            "- 短時間保育児は早朝保育・延長保育時間帯の在園者数には含めない。\n"
            "- 保育標準時間を利用しない（長時間型）園児は在園した時間帯すべてで計上する。\n"
            "- 同一時間帯に両者が存在する場合、必要人員は年齢クラスごとに合算する。"
        )
    return LocalRuleNote(
        key="short_time",
        title="短時間保育（3時間保育）園の扱い",
        detail=detail,
        source=source,
        legal_reference="保育所保育指針／保育所の職員配置基準／児童福祉法第18条の4",
    )


def _note_legal(standard: StaffingStandard) -> LocalRuleNote:
    return LocalRuleNote(
        key="legal",
        title="法令・告示の根拠",
        detail=(
            "本プリセットの配置基準は主に次の法令・告示に基づく。\n\n"
            "1. 児童福祉法第18条の4第1項第2号："
            "保育室に乳幼児がいる場合の基準は「保育士」"
            "（ただし、幼稚園教諭にはクラス担任経験等の条件付き）。\n"
            "2. 保育所の職員配置基準（昭和52年厚生省告示第49号）："
            "年齢ごとの定員比と、保育標準時間・延長保育の規定。\n"
            "3. 労働基準法第9条・第32条の3："
            "労働時間・休息日の原則（本アプリの休憩・連続勤務制約に対応）。"
        ),
        source=standard.remarks,
        legal_reference=_LEGAL_BASE,
    )


def _note_scope(standard: StaffingStandard) -> LocalRuleNote:
    """このプリセットが**扱っていない制度**を明示するカード。"""
    return LocalRuleNote(
        key="scope",
        title="適用できる制度の範囲（重要）",
        detail=(
            SCOPE_STATEMENT + "\n\n認可外保育施設の基準（認可外保育施設指導監督基準／"
            "「企業主導型保育事業費補助金実施要綱」第3の2(4)）は、"
            "算出手法そのものが異なる。認可外では\n"
            "- 年齢区分ごとに小数第2位以下を切り捨て\n"
            "- 上記を合計し **1 を加える**\n"
            "- 小数第1位で四捨五入する\n\n"
            "という順序になるため、本プリセットの「年齢クラス毎に切り上げ」と結果が食い違う"
            "（実測で 1 時間帯あたり最大 4 名ずれる）。認可外保育施設で本プリセットを"
            "使うと、基準を満たしていると誤認する可能性がある。\n\n"
            "認可外保育施設は `compliance.py` のチェックリストと、"
            '`headcount_mode="facility_formula"` のプリセット在对すること。'
        ),
        source=standard.remarks,
        legal_reference=_LEGAL_BASE,
    )


def _note_caveat(standard: StaffingStandard) -> LocalRuleNote:
    return LocalRuleNote(
        key="caveat",
        title="留意点",
        detail=(
            "1. 本プリセットの数値は一般的な実務水準に基づく目安であり、"
            "自治体の告示・条例・要綱・運用と一致する保証はない。\n"
            "2. 園ごとの上乗せ（定員の制限、年齢クラスの定員、共用保育室、"
            "混合編成、行事日の一時的な人員確保）がある場合は、"
            "build_standard の overrides で上書きすること。\n"
            "3. 祝日・行事日は is_binding=False として超過配置を許容するが、"
            "実際の勤務に際しては園長・保育主事の判断を優先する。\n"
            "4. 必要人員は「在園児が在園している時間帯」に対して算出する。"
            "登降園予定が未定の場合は平均や最大値で代替すること。"
        ),
        source=standard.remarks,
        legal_reference=_LEGAL_BASE,
    )


def _note_facility(qualified_ratio: float) -> LocalRuleNote:
    """認可外保育施設（指導監督基準・企業主導型保育事業）固有の説明カード。"""
    if qualified_ratio >= 0.75:
        numerator = "4分の3"
    elif qualified_ratio >= 0.5:
        numerator = "半数"
    else:
        numerator = "3分の1"
    return LocalRuleNote(
        key="unlicensed",
        title="認可外保育施設の基準（認可保育所との違い）",
        detail=(
            "**このプリセットは認可外保育施設（企業主導型保育事業）の基準である。**\n"
            "認可保育所（他11プリセット）とは法令の階層・算出手法・資格要件がすべて異なる。\n\n"
            "1. 必要人員の算出手法\n"
            "   認可外は「乳児3人:1／1・2歳児6人:1／3歳児20人:1／4歳以上児30人:1」を\n"
            "   合計し **1 を加え**、各区分は小数第2位以下を切り捨ててから合計し、\n"
            "   小数第1位で四捨五入する。1・2歳児と4歳以上児は**合算**してから割る。\n"
            "   認可保育所の「年齢クラス毎に切り上げ」と結果は一致せず、\n"
            "   実測で 1 時間帯あたり最大 4 名ずれる。\n\n"
            f"2. 資格要件（{numerator}）\n"
            f"   保育従事者の**{numerator}以上**が保育士。\n"
            "   その他の保育従事者は「子育て支援員研修（地域保育コースのうち地域型保育）」\n"
            "   修了者、または市町村研修の修了者（当該年度中に受講予定者を含む）。\n"
            "   保健師・看護師・准看護師は**1人に限り保育士とみなせる**。\n\n"
            "3. 最低配置\n"
            "   必要数が 1 名と計算される時間帯でも最低2名、うち1名以上は保育士。\n"
            "   開所〜開所+11時間を主たる開所時間とし、それを超える時間帯は\n"
            "   現に保育されている児童が1名である場合を除き常時2人以上。\n\n"
            "4. このプリセットが扱っていない要件\n"
            "   - 嘱託医・調理員の必置（調理業務の全部委託または食事の搬入で調理員を免除できる）\n"
            "   - 短時間勤務職員の常勤換算（有資格者・その他別に勤務延べ時間÷8時間）\n"
            "   - 月極めの基礎乳幼児数と日極め等の平均加算\n"
            "   - 地域枠は総定員の50%以内\n"
            "   - 基本分単価・処遇改善等加算などの補助金算定\n"
            "   これらは `compliance.py` のチェックリスト（月次報告・巡回指導の場面）で確認する。\n\n"
            "5. 保育標準時間について\n"
            "   認可外保育施設に保育標準時間の制度は無い。本プリセットの「保育標準時間」は\n"
            "   認可外保育施設指導監督基準 第1(1) の**主たる開所時間（11時間）**である。\n"
            "   短時間保育（保育標準時間認定）を使う運用はしないこと。"
        ),
        source=_FACILITY_SOURCE,
        legal_reference=_FACILITY_SOURCE,
    )


_PRESET_EXTRA_NOTES: dict[str, list[LocalRuleNote]] = {
    "全国基準（厚労省）": [
        LocalRuleNote(
            key="national",
            title="全国基準の位置づけ",
            detail=(
                "保育所の職員配置基準（厚生省告示第49号）の最低基準に対応する。"
                "0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児20:1、"
                "保育標準時間8時間30分〜17時15分。\n\n"
                "この基準は「最低限度」であり、待機児童を受け付けない園では"
                "実質これより厳しい基準が適用される。なお保育標準時間内の"
                "早暁・延長（7:15〜8:30／17:15〜19:30）は必要人数を定員比で求め、"
                "保育士のみを配置する（代替措置なし）。"
            ),
            source="保育所の職員配置基準（昭和52年厚生省告示第49号）",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "東京都": [
        LocalRuleNote(
            key="tokyo",
            title="東京都固有の基準",
            detail=(
                "東京都は3歳児8:1、4・5歳児12:1と、全国基準の4・5歳児20:1より"
                "厳しい定員比を定める。\n\n"
                "延長保育（17:15〜18:30）には「保育士1名＋子育て支援員／"
                "配置的保育支援員」の代替措置を認める（代替者には2歳児クラスの"
                "保育補助の経験が求められる場合がある）。"
            ),
            source="東京都「保育所の職員配置基準」告示／東京都保育已基本方針",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "横浜市": [
        LocalRuleNote(
            key="yokohama",
            title="横浜市固有の基準",
            detail=(
                "東京都系の定員比を採用しつつ、延長保育を19:00まで"
                "代替措置付きで認可する運用が一般的。\n\n"
                "園の閉所時刻が19:00を超える場合、19:00以降は延長保育"
                "（緩和措置なし）として保育士のみを配置する必要がある。"
            ),
            source="横浜市「保育所の職員配置に関する基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "大阪市": [
        LocalRuleNote(
            key="osaka",
            title="大阪市固有の基準",
            detail=(
                "0歳児4:1、1歳児5:1、2歳児7:1と、全国基準"
                "（0歳児3:1、1・2歳児6:1）より厳しい。\n\n"
                "在園児数が小さい園では必要人員の総数が増え、"
                "必要人員が全国基準より増えることに注意する。"
                " 延長保育（17:15〜18:30）には代替措置を認める。"
            ),
            source="大阪市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "福岡市": [
        LocalRuleNote(
            key="fukuoka",
            title="福岡市固有の基準",
            detail=(
                "保育園型利用者支援（保育を必要とする障害児支援）を行う施設を想定。\n\n"
                "- 延長保育（17:15〜18:30）は「保育士1名＋子育て支援員／"
                "幼稚園教諭（2歳児クラスの担任経験がある者）／配置的保育支援員」で"
                "代替できる。\n"
                "- 18:30以降は保育士のみで満たす。\n"
                "- 1歳児4:1と厳格な定員比を持つ。"
            ),
            source="福岡市「保育所の職員配置基準」告示／福岡市保育已基本方針",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "名古屋市": [
        LocalRuleNote(
            key="nagoya",
            title="名古屋市固有の基準",
            detail=(
                "定員比は全国基準（厚生省告示第49号）準拠。\n\n"
                "延長保育は19:00まで「保育士1名＋他資格者」の代替措置を認める。"
            ),
            source="名古屋市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "京都市": [
        LocalRuleNote(
            key="kyoto",
            title="京都市固有の基準",
            detail=(
                "定員比は全国基準準拠（0歳児3:1、1・2歳児6:1、3歳児8:1、4・5歳児20:1）。\n\n"
                "延長保育（17:15〜18:30）は「保育士1名＋他資格者」の代替措置を認める。"
            ),
            source="京都市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "札幌市": [
        LocalRuleNote(
            key="sapporo",
            title="札幌市固有の基準",
            detail=(
                "定員比は全国基準準拠。\n\n"
                "本プリセットでは延長保育の代替措置を「なし」と仮定した"
                "（札幌市は条件付きで認める場合があるため要確認）。"
            ),
            source="札幌市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "神戸市": [
        LocalRuleNote(
            key="kobe",
            title="神戸市固有の基準",
            detail=(
                "東京都系（3歳児8:1、4・5歳児12:1）を採用。\n\n"
                "延長保育（17:15〜18:30）は「保育士1名＋他資格者」の代替措置を認める。"
            ),
            source="神戸市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "川崎市": [
        LocalRuleNote(
            key="kawasaki",
            title="川崎市固有の基準",
            detail=(
                "東京都系（3歳児8:1、4・5歳児12:1）を採用。\n\n"
                "延長保育（17:15〜18:30）は「保育士1名＋他資格者」の代替措置を認める。"
            ),
            source="川崎市「保育所の職員配置基準」告示",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "保育標準時間のみ園": [
        LocalRuleNote(
            key="short_only",
            title="短時間保育園の運用",
            detail=(
                "保育標準時間のみ（3時間保育）を行う園。早朝保育・延長保育は行わないため、"
                "開所時刻は保育標準時間の開始時刻（8:30）に合わせる。\n\n"
                "全園児が短時間保育児（is_short_time=True）となり、"
                "必要人員は保育標準時間帯のみで算出される。\n\n"
                "保育標準時間は法定11時間のうち8時間45分に相当し、"
                "残りの2時間15分は家庭的保育時間として確保される。"
            ),
            source="保育所保育指針／保育所の職員配置基準",
            legal_reference=_LEGAL_BASE,
        )
    ],
    "認可外保育施設（指導監督基準）": [_note_facility(0.0)],
    "企業主導型保育事業（単独枠）": [_note_facility(0.5)],
    "企業主導型保育事業（保育事業者型・20名以上）": [_note_facility(0.75)],
}


def local_rule_notes(standard: StaffingStandard) -> list[LocalRuleNote]:
    """設定画面に出す「自治体別ローカルルール」説明カードを返す。"""
    source = preset_source(standard)
    notes = [
        _note_hours(standard, source),
        _note_headcount(standard, source),
        _note_late_care(standard, source),
        _note_standard_time(standard, source),
        _note_min_two(standard, source),
        _note_short_time(standard, source),
    ]
    notes.extend(_PRESET_EXTRA_NOTES.get(standard.name, ()))
    notes.append(_note_legal(standard))
    notes.append(_note_scope(standard))
    notes.append(_note_caveat(standard))
    return notes
