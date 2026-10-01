"""現場固有の勤務パターン（早番・日勤・遅番）への整列（スナップ）。

**このモジュールが解決する問題**

MILP は「時間帯ごとに何人」を最適化するため、境界の必要性を意識しない結果、
「8:45〜17:15」のような半端な勤務枠を出しやすい。
実際の保育現場では早番・日勤・遅番の枠が決まっており、
主任保育士が生成後に手で直す手数が増えている。

そこで 2 段構えの対応を取る。

1. **最適化フェーズ（``solver._add_pattern_alignment``）**
   勤務ブロックの開始・終了がパターン境界から離れるほど目的関数が大きくなる
   ソフトペナルティを足し、ソルバ自体に境界へ引き寄せる。
   ハード制約ではないため、揃う職員がいなくても解は消えない。
2. **後処理フェーズ（:func:`shiftai.solver.snap_to_patterns`）**
   生成済みの解を、境界を丸める形で整列させる。配置基準を破る差し替えは行わない。

**パターンの定義**

パターンは ``(開始時刻, 終了時刻)`` を持つだけでよい。
時間帯の粒度（slot granularity）に合わせるのは境界を slot index へ変換する段階で行う。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

from shiftai.domain import Slot, to_minutes

__all__ = [
    "DEFAULT_PATTERN_KEYS",
    "PatternKey",
    "ShiftPattern",
    "DEFAULT_PATTERNS",
    "default_patterns",
    "describe_pattern",
    "match_pattern",
    "nearest_pattern_window",
    "normalize_patterns",
    "parse_pattern_spec",
    "pattern_cost_per_slot",
]


@dataclass(frozen=True)
class ShiftPattern:
    """勤務パターン 1 枠（例: 早番 07:30〜16:30）。"""

    key: str
    label: str
    start: time
    end: time

    @property
    def start_minutes(self) -> int:
        return to_minutes(self.start)

    @property
    def end_minutes(self) -> int:
        return to_minutes(self.end)

    @property
    def minutes(self) -> int:
        return self.end_minutes - self.start_minutes

    @property
    def hours(self) -> float:
        return self.minutes / 60.0

    def span(self) -> str:
        """``07:30〜16:30（9.0h）`` のような表示用文字列。"""
        return f"{self.start.strftime('%H:%M')}〜{self.end.strftime('%H:%M')}（{self.hours:.1f}h）"

    def covers(self, minutes: int) -> bool:
        """その時刻をパターンが覆っているか。"""
        return self.start_minutes <= minutes < self.end_minutes


PatternKey = str
DEFAULT_PATTERN_KEYS: tuple[str, ...] = ("early", "day", "late")

DEFAULT_PATTERNS: tuple[ShiftPattern, ...] = (
    ShiftPattern("early", "早番", time(7, 30), time(16, 30)),
    ShiftPattern("day", "日勤", time(8, 30), time(17, 30)),
    ShiftPattern("late", "遅番", time(10, 30), time(19, 30)),
)
"""工場既定の 3 枠（開所 7:15〜閉所 19:30 の園を想定）。

早番は開所と同時に、日勤は保育標準時間の中心、遅番は閉所と同時に終わるように配置している。
園の開所・閉所時間が違う場合は :func:`default_patterns` で自動生成する。
"""


def default_patterns(
    day_open: time | None = None,
    day_close: time | None = None,
    *,
    shift_hours: float = 9.0,
    gran_min: int = 15,
) -> tuple[ShiftPattern, ...]:
    """園の開所・閉所時間に合わせた既定パターンを生成する。

    * 早番: 開所 〜 開所 + ``shift_hours``
    * 日勤: 開所 + 1時間 〜 開所 + 1時間 + ``shift_hours``
    * 遅番: 閉所 - ``shift_hours`` 〜 閉所

    **枠の長さは園の営業時間で頭打ちにする。** 開所 9:00〜閉所 14:00 の園に
    9 時間枠を作ると、境界が営業時間の外にはみ出して**どの勤務とも一致しない**。
    ``--patterns auto`` を渡した場合に整合し、5 時間の枠が生成される。

    :param day_open: 園の開所時刻（省略時は 07:30）
    :param day_close: 園の閉所時刻（省略時は 19:30）
    :param shift_hours: 1枠の長さ（時間）
    :param gran_min: 境界を丸める粒度（分）
    :returns: 開始時刻順に並べたパターン
    """
    open_m = to_minutes(day_open) if day_open is not None else 7 * 60 + 30
    close_m = to_minutes(day_close) if day_close is not None else 19 * 60 + 30
    window = max(60, close_m - open_m)
    span = max(60, min(int(round(shift_hours * 60)), window))
    gran = max(1, int(gran_min))

    def align(value: int) -> int:
        return int((value + gran // 2) // gran) * gran

    early_start = align(open_m)
    day_start = align(open_m + 60)
    late_end = align(close_m)
    patterns: list[ShiftPattern] = []
    for key, label, start in (
        ("early", "早番", early_start),
        ("day", "日勤", day_start),
        ("late", "遅番", max(day_start, late_end - span)),
    ):
        end = min(start + span, late_end)
        if end > start:
            patterns.append(ShiftPattern(key, label, _clock(start), _clock(end)))
    return normalize_patterns(patterns)


def _clock(minutes: int) -> time:
    """分 → :class:`datetime.time`（0:00 を 24:00 扱いできるよう 1440 を許容）。"""
    value = max(0, min(24 * 60, int(minutes)))
    return time(value // 60, value % 60)


def normalize_patterns(patterns: Sequence[ShiftPattern] | None) -> tuple[ShiftPattern, ...]:
    """開始時刻順に並べ、終了が開始と同じパターンを落とす。"""
    if not patterns:
        return ()
    cleaned = [p for p in patterns if p.end_minutes > p.start_minutes]
    seen: set[tuple[int, int]] = set()
    out: list[ShiftPattern] = []
    for pattern in sorted(cleaned, key=lambda p: (p.start_minutes, p.end_minutes)):
        key = (pattern.start_minutes, pattern.end_minutes)
        if key in seen:
            continue
        seen.add(key)
        out.append(pattern)
    return tuple(out)


def parse_pattern_spec(spec: str) -> ShiftPattern:
    """``"早番=07:30-16:30"`` 形式のパターン定義文字列を解釈する。

    ラベル omitted 時は「枠1」「枠2」…を割り当てる。

    :raises ValueError: 形式が不正なとき
    """
    raw = spec.strip()
    if not raw:
        raise ValueError("パターンの定義が空です")
    label = raw
    if "=" in raw:
        label, _, raw = raw.partition("=")
        label = label.strip()
        raw = raw.strip()
    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError(f"パターンの定義が不正です: {spec!r}（例: 早番=07:30-16:30）")
    try:
        start = time.fromisoformat(parts[0].strip())
        end = time.fromisoformat(parts[1].strip())
    except ValueError as exc:
        raise ValueError(f"パターンの時刻が不正です: {spec!r}（例: 早番=07:30-16:30）") from exc
    if not label:
        label = "枠"
    return ShiftPattern(label=label, key=label, start=start, end=end)


def nearest_pattern_window(
    start_minutes: int,
    end_minutes: int,
    patterns: Sequence[ShiftPattern],
) -> ShiftPattern | None:
    """勤務ブロックに最も近いパターンを返す（境界のずれが小さい順）。"""
    best: tuple[int, ShiftPattern] | None = None
    for pattern in patterns:
        distance = abs(pattern.start_minutes - start_minutes) + abs(
            pattern.end_minutes - end_minutes
        )
        if best is None or distance < best[0] or (
            distance == best[0] and pattern.start_minutes < best[1].start_minutes
        ):
            best = (distance, pattern)
    return None if best is None else best[1]


def match_pattern(
    start_minutes: int,
    end_minutes: int,
    patterns: Sequence[ShiftPattern],
    *,
    tol_minutes: int = 1,
) -> ShiftPattern | None:
    """勤務ブロックがパターンと**厳密に一致**するときだけ、そのパターンを返す。

    「ほぼ一致」は一致扱いにしない。Excel への出力で境界がずれていると
    現場が度を越すため、判定は厳密に行う。
    """
    for pattern in patterns:
        if (
            abs(pattern.start_minutes - start_minutes) <= tol_minutes
            and abs(pattern.end_minutes - end_minutes) <= tol_minutes
        ):
            return pattern
    return None


def describe_pattern(
    start_minutes: int,
    end_minutes: int,
    patterns: Sequence[ShiftPattern],
) -> str:
    """勤務ブロックを「早番（07:30〜16:30）」のような表示へ変換する。"""
    exact = match_pattern(start_minutes, end_minutes, patterns)
    if exact is not None:
        return f"{exact.label}（{exact.span()}）"
    if not patterns:
        return f"{_clock(start_minutes).strftime('%H:%M')}〜{_clock(end_minutes).strftime('%H:%M')}"
    nearest = nearest_pattern_window(start_minutes, end_minutes, patterns)
    assert nearest is not None
    offset = (start_minutes - nearest.start_minutes) + (end_minutes - nearest.end_minutes)
    sign = "+" if offset >= 0 else "-"
    return (
        f"{_clock(start_minutes).strftime('%H:%M')}〜{_clock(end_minutes).strftime('%H:%M')}"
        f"（{nearest.label}から{sign}{abs(offset)}分）"
    )


def pattern_cost_per_slot(
    slots: Sequence[Slot],
    patterns: Sequence[ShiftPattern],
) -> tuple[list[float], list[float]]:
    """各時間帯について「開始境界」「終了境界」のずれ（時間帯長さを単位とする）を返す。

    境界がパターンと一致する時間帯は 0 になる。UI で「どの境界がどれだけずれているか」を
    説明するときに使う。
    """
    if not patterns:
        n = len(slots)
        return ([0.0] * n, [0.0] * n)
    starts = [p.start_minutes for p in patterns]
    ends = [p.end_minutes for p in patterns]
    gran = max(1, int(slots[0].minutes)) if slots else 1
    start_cost: list[float] = []
    end_cost: list[float] = []
    for slot in slots:
        start_cost.append(min(abs(slot.start_minutes - s) for s in starts) / gran)
        end_cost.append(min(abs(slot.end_minutes - e) for e in ends) / gran)
    return (start_cost, end_cost)