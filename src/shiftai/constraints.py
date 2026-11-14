"""宣言的制約レジストリ (Timefold Constraint Streams / Registry 思想).

シフト最適化における全制約（法令・契約・希望・公平性・運用）を宣言的に一元管理し、
園別の実行時重み設定や What-if 分析を可能にする。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ConstraintCategory(str, Enum):
    """制約のカテゴリ分類。"""

    LEGAL_STATUTORY = "LEGAL_STATUTORY"  # 法令・自治体配置基準 (Hard)
    STAFF_CONTRACT = "STAFF_CONTRACT"    # 雇用契約・労使協定 (Hard)
    PREFERENCE = "PREFERENCE"            # 職員の希望休・シフト希望 (Soft)
    FAIRNESS = "FAIRNESS"                # 公平性・平準化 (Soft)
    OPERATIONAL = "OPERATIONAL"          # 園運用・コスト・勤務パターン (Soft)


@dataclass(frozen=True)
class ConstraintDefinition:
    """宣言的制約定義。"""

    id: str
    name: str
    description: str
    category: ConstraintCategory
    is_hard: bool
    default_weight: float
    configurable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "is_hard": self.is_hard,
            "default_weight": self.default_weight,
            "configurable": self.configurable,
        }


class ConstraintRegistry:
    """制約定義のグローバルレジストリ。"""

    _registry: dict[str, ConstraintDefinition] = {}

    @classmethod
    def register(cls, definition: ConstraintDefinition) -> None:
        cls._registry[definition.id] = definition

    @classmethod
    def get(cls, constraint_id: str) -> ConstraintDefinition | None:
        return cls._registry.get(constraint_id)

    @classmethod
    def all(cls) -> list[ConstraintDefinition]:
        return list(cls._registry.values())

    @classmethod
    def by_category(cls, category: ConstraintCategory) -> list[ConstraintDefinition]:
        return [c for c in cls._registry.values() if c.category == category]

    @classmethod
    def hard_constraints(cls) -> list[ConstraintDefinition]:
        return [c for c in cls._registry.values() if c.is_hard]

    @classmethod
    def soft_constraints(cls) -> list[ConstraintDefinition]:
        return [c for c in cls._registry.values() if not c.is_hard]


# ---------------------------------------------------------------------------
# 全 21 種の標準制約定義の登録
# ---------------------------------------------------------------------------

_BUILTIN_CONSTRAINTS = [
    # 1. 法令・自治体配置基準 (Hard)
    ConstraintDefinition(
        id="SHORTFALL_STAFF",
        name="必要人員配置基準",
        description="時間帯ごとの園児数に対する必要最低人員（児童福祉法基準）を満たす",
        category=ConstraintCategory.LEGAL_STATUTORY,
        is_hard=True,
        default_weight=1000.0,
        configurable=False,
    ),
    ConstraintDefinition(
        id="SHORTFALL_QUALIFIED",
        name="保育士比率配置基準",
        description="時間帯ごとの必要保育士数（常時2名以上の有資格者等）を満たす",
        category=ConstraintCategory.LEGAL_STATUTORY,
        is_hard=True,
        default_weight=1200.0,
        configurable=False,
    ),
    ConstraintDefinition(
        id="BREAK_INSUFFICIENT",
        name="法定休憩時間確保",
        description="労働基準法第34条に基づく休憩時間（6h超で45分、8h超で60分）を確保する",
        category=ConstraintCategory.LEGAL_STATUTORY,
        is_hard=True,
        default_weight=5000.0,
        configurable=False,
    ),
    ConstraintDefinition(
        id="SPLIT_SHIFT",
        name="1日勤務の3分割禁止",
        description="中抜け勤務を防止し、1日の勤務を最大2ブロック（勤務-休憩-勤務）までに制限する",
        category=ConstraintCategory.LEGAL_STATUTORY,
        is_hard=True,
        default_weight=5000.0,
        configurable=False,
    ),
    ConstraintDefinition(
        id="REST_HOURS_SHORT",
        name="勤務間インターバル不足",
        description="前日退勤から翌日出勤までの間に契約で定めた休息時間（例: 11時間）を確保する",
        category=ConstraintCategory.LEGAL_STATUTORY,
        is_hard=True,
        default_weight=2000.0,
    ),
    # 2. 雇用契約・労使協定 (Hard)
    ConstraintDefinition(
        id="DAILY_HOURS_EXCEEDED",
        name="1日労働時間上限",
        description="労働基準法および契約で定められた1日の上限労働時間を超えない",
        category=ConstraintCategory.STAFF_CONTRACT,
        is_hard=True,
        default_weight=2000.0,
        configurable=False,
    ),
    ConstraintDefinition(
        id="CONSECUTIVE_DAYS",
        name="連続勤務日数上限",
        description="就業規則で定められた連続勤務日数（例: 最大5日）を超えない",
        category=ConstraintCategory.STAFF_CONTRACT,
        is_hard=True,
        default_weight=1500.0,
    ),
    ConstraintDefinition(
        id="OUTSIDE_CONTRACT_HOURS",
        name="契約可能時間帯の遵守",
        description="雇用契約で合意された勤務可能時間帯（最早始業〜最遅終業）の範囲内で勤務する",
        category=ConstraintCategory.STAFF_CONTRACT,
        is_hard=True,
        default_weight=1000.0,
    ),
    ConstraintDefinition(
        id="SEI_DAILY_HOURS_RANGE",
        name="正社員勤務時間調整幅",
        description="正社員の1日勤務時間を4〜8時間の間で柔軟に調整する",
        category=ConstraintCategory.STAFF_CONTRACT,
        is_hard=True,
        default_weight=500.0,
    ),
    # 3. 職員希望 (Soft)
    ConstraintDefinition(
        id="WORK_ON_UNAVAILABLE",
        name="希望休・不可日の尊重",
        description="職員から提出された希望休および出勤不可時間帯にシフトを割り当てない",
        category=ConstraintCategory.PREFERENCE,
        is_hard=False,
        default_weight=50.0,
    ),
    ConstraintDefinition(
        id="AVOID_EARLY_CONFLICT",
        name="早番回避希望の尊重",
        description="早番を避けたい職員への早番割り当てを抑制する",
        category=ConstraintCategory.PREFERENCE,
        is_hard=False,
        default_weight=20.0,
    ),
    ConstraintDefinition(
        id="AVOID_LATE_CONFLICT",
        name="遅番回避希望の尊重",
        description="遅番を避けたい職員への遅番割り当てを抑制する",
        category=ConstraintCategory.PREFERENCE,
        is_hard=False,
        default_weight=20.0,
    ),
    ConstraintDefinition(
        id="PREF_PREFERRED_DAY",
        name="出勤希望日の優先配置",
        description="職員が出勤を希望した日に優先的に勤務を割り当てる",
        category=ConstraintCategory.PREFERENCE,
        is_hard=False,
        default_weight=15.0,
    ),
    # 4. 公平性・平準化 (Soft)
    ConstraintDefinition(
        id="HOLIDAY_TARGET_DIFF",
        name="月間公休目標の平準化",
        description="各職員の月間公休取得数が園の目標日数から乖離しないように平準化する",
        category=ConstraintCategory.FAIRNESS,
        is_hard=False,
        default_weight=15.0,
    ),
    ConstraintDefinition(
        id="FAIRNESS_SPREAD_EARLY",
        name="早番回数の公平化",
        description="特定の職員に早番が集中しないよう全職員間で平準化する",
        category=ConstraintCategory.FAIRNESS,
        is_hard=False,
        default_weight=10.0,
    ),
    ConstraintDefinition(
        id="FAIRNESS_SPREAD_LATE",
        name="遅番回数の公平化",
        description="特定の職員に遅番が集中しないよう全職員間で平準化する",
        category=ConstraintCategory.FAIRNESS,
        is_hard=False,
        default_weight=10.0,
    ),
    ConstraintDefinition(
        id="FAIRNESS_SPREAD_SATURDAY",
        name="土曜出勤回数の公平化",
        description="土曜日の出勤回数が特定の職員に偏らないよう平準化する",
        category=ConstraintCategory.FAIRNESS,
        is_hard=False,
        default_weight=10.0,
    ),
    # 5. 園運用・コスト・勤務パターン (Soft)
    ConstraintDefinition(
        id="OVERSTAFF",
        name="余剰人員配置の抑制",
        description="必要人員を超過する余剰人員の配置を抑え、人件費と配置を適正化する",
        category=ConstraintCategory.OPERATIONAL,
        is_hard=False,
        default_weight=1.0,
    ),
    ConstraintDefinition(
        id="PATTERN_MATCHING",
        name="実務シフトパターンへの引き寄せ",
        description="勤務の開始・終了時刻を園規定の勤務パターン（早番・日勤・遅番等）に揃える",
        category=ConstraintCategory.OPERATIONAL,
        is_hard=False,
        default_weight=5.0,
    ),
    ConstraintDefinition(
        id="BREAK_FRAGMENTED",
        name="休憩の分割防止",
        description="1日の休憩が細切れに分かれることを防ぎ、連続した休憩を確保する",
        category=ConstraintCategory.OPERATIONAL,
        is_hard=False,
        default_weight=20.0,
    ),
    ConstraintDefinition(
        id="BREAK_OVERLAP",
        name="同時休憩人数の集中防止",
        description="同一時間帯に多くの職員が同時に休憩に入ることを防ぎ、現場保育力を維持する",
        category=ConstraintCategory.OPERATIONAL,
        is_hard=False,
        default_weight=10.0,
    ),
]

for _c in _BUILTIN_CONSTRAINTS:
    ConstraintRegistry.register(_c)
