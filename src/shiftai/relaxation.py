"""Infeasible（解なし）時の「緩和モード」。

**このモジュールが解決する問題**

配置基準を満たせないとき、素朴な実装は「解なし → 貪欲法へ落ちる」だけで終わる。
現場から見ると「動かない／使えない」だけで、**なぜ最適化しできなかったのか**が分からない。
本来は「どの日のどの時間帯で何名が不足していたか」を提示し、
不足を許容してでも解を求める選択肢を出したい。

そこで「どこまで緩めるか」を段階として定義する。

===========================  ==========================================
レベル                        緩めるもの
===========================  ==========================================
``STRICT`` (0)               緩めない。配置基準はハード制約。
``SOFT_COVERAGE`` (1)        配置基準を「不足人数 × 罰変数」に置き換える。
``RELAX_WEEKLY`` (2)         ＋ 週所定出勤日数・連続勤務日数を罰変数化のみ。
``RELAX_HOURS`` (3)          ＋ 休憩時間・休息時間・長時間勤務を罰変数化のみ。
``IGNORE_UNAVAILABLE`` (4)   ＋ 希望休・休園日・休日の出勤不可を無視（最終手段）。
===========================  ==========================================

**緩めないもの（意図的な設計）**

* 1日の上限時間（法定10時間）
* セル状態の排他、在勤ブロック1つ、手動確定セル

これらは法令・契約の骨格であり、緩めると「見た目は解けたが運用できない
シフト」になる。緩和は「法令を守ったまま現場agin的实现が可能になる範囲」に限る。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "MAX_RELAX_LEVEL",
    "RELAX_LEVELS",
    "RelaxLevel",
    "RelaxSpec",
    "describe_relaxations",
    "normalize_level",
    "relax_level_index",
    "relaxation_options",
    "relaxed_constraint_labels",
    "relaxed_weight_names",
]


class RelaxLevel(IntEnum):
    """緩和の段階。値は ``solve_shift(relaxation=...)`` にそのまま渡せる。"""

    STRICT = 0
    SOFT_COVERAGE = 1
    RELAX_WEEKLY = 2
    RELAX_HOURS = 3
    IGNORE_UNAVAILABLE = 4


MAX_RELAX_LEVEL: int = int(RelaxLevel.IGNORE_UNAVAILABLE)


@dataclass(frozen=True)
class RelaxSpec:
    """緩和段階のメタデータ。UI の選択肢生成とメッセージに使う。"""

    level: RelaxLevel
    label: str
    description: str

    @property
    def value(self) -> int:
        return int(self.level)

    @property
    def weight(self) -> str:
        """この段階までに無効化する目的関数のペナルティ名（UI の説明用）。"""
        return "、".join(relaxed_weight_names(self.value)) or "なし"


RELAX_LEVELS: tuple[RelaxSpec, ...] = (
    RelaxSpec(
        RelaxLevel.STRICT,
        "厳格（既定）",
        "配置基準をハード制約として扱います。満たせなければ不足を許容する2パス目へ進みます。",
    ),
    RelaxSpec(
        RelaxLevel.SOFT_COVERAGE,
        "配置基準を罰変数化",
        "配置基準の不足を許容して解きます。不足した時間帯と人数は明示して報告します。",
    ),
    RelaxSpec(
        RelaxLevel.RELAX_WEEKLY,
        "＋ 出勤日数・連続勤務を罰変数化",
        "週所定出勤日数と最大連続勤務日数を「超過してもよいが罰される」に緩めます。",
    ),
    RelaxSpec(
        RelaxLevel.RELAX_HOURS,
        "＋ 休憩・休息時間を罰変数化",
        "最低休憩時間・勤務間の休息時間・長時間勤務のペナルティを無効化します。"
        "法令の上限時間（1日10時間）は緩めません。",
    ),
    RelaxSpec(
        RelaxLevel.IGNORE_UNAVAILABLE,
        "＋ 希望休・休園日を無視",
        "希望休・不在時間帯・休園日・休日勤務不可を無視して解きます。"
        "運用上の合意がなければ使わないでください（最終手段）。",
    ),
)

#: 段階ごとに無効化する ``ObjectiveWeights`` のフィールド名。
_WEIGHT_GROUPS: dict[int, tuple[str, ...]] = {
    int(RelaxLevel.RELAX_WEEKLY): (
        "consecutive_day_penalty",
        "fairness_early_penalty",
        "fairness_late_penalty",
        "fairness_saturday_penalty",
    ),
    int(RelaxLevel.RELAX_HOURS): (
        "break_conflict_penalty",
        "rest_violation_penalty",
        "max_shift_length_penalty",
    ),
}


def normalize_level(value: object) -> int:
    """任意の入力（``RelaxLevel`` / 数値 / ``None``）を段階番号に正規化する。"""
    if value is None:
        return int(RelaxLevel.STRICT)
    try:
        level = int(value)
    except (TypeError, ValueError):
        return int(RelaxLevel.STRICT)
    return max(int(RelaxLevel.STRICT), min(MAX_RELAX_LEVEL, level))


def relaxed_weight_names(level: int) -> tuple[str, ...]:
    """その段階で無効化されるペナルティ名の集合。"""
    level = normalize_level(level)
    out: list[str] = []
    for threshold in sorted(_WEIGHT_GROUPS):
        if level >= threshold:
            out.extend(_WEIGHT_GROUPS[threshold])
    return tuple(out)


def relaxed_constraint_labels(level: int) -> tuple[str, ...]:
    """その段階で緩められる制約の日本語ラベル。"""
    level = normalize_level(level)
    out: list[str] = []
    if level >= int(RelaxLevel.SOFT_COVERAGE):
        out.append("配置基準（必要人員・必要保育士数）")
    if level >= int(RelaxLevel.RELAX_WEEKLY):
        out.append("週所定出勤日数")
        out.append("最大連続勤務日数")
    if level >= int(RelaxLevel.RELAX_HOURS):
        out.append("最低休憩時間")
        out.append("勤務間の休息時間")
        out.append("長時間勤務の内部目安")
    if level >= int(RelaxLevel.IGNORE_UNAVAILABLE):
        out.append("希望休・不在時間帯")
        out.append("休園日・休日の出勤不可")
    return tuple(out)


def relaxation_options() -> list[str]:
    """``st.selectbox`` にそのまま渡せる表示用ラベルの列を返す。

    ``(ラベル, 値)`` のタプルを返すと ``st.selectbox.options`` がタプルのままになり、
    Streamlit の ``AppTest`` から文字列で ``set_value`` できない。
    UI には文字列だけを見せ、値は :func:`relax_level_index` で解決する。
    """
    return [f"L{spec.value} {spec.label}" for spec in RELAX_LEVELS]


def relax_level_index(level: int) -> int:
    """段階番号を ``relaxation_options()`` の添字に変換する。"""
    value = normalize_level(level)
    for i, spec in enumerate(RELAX_LEVELS):
        if spec.value == value:
            return i
    return 0


def describe_relaxations(level: int) -> list[str]:
    """その段階で緩めた制約の日本語説明（結果メッセージ用）。"""
    labels = relaxed_constraint_labels(level)
    if not labels:
        return []
    return [f"緩めた制約: {'、'.join(labels)}"]