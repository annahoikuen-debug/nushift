"""UI 層の session_state 管理。

``StaffingStandard.ratios`` が ``Mapping`` フィールドでハッシュ不能、
``list[ChildPlan]`` も ``st.cache_data`` の ``hash_func`` に向かないため、
再計算は ``st.cache_data`` ではなく **session_state + 明示的なボタントリガ** で駆動する。
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

import streamlit as st

from shiftai import local_rules, solver
from shiftai.config import (
    DEFAULT_DAY_CLOSE,
    DEFAULT_DAY_OPEN,
    DEFAULT_GRANULARITY_MIN,
    DEFAULT_RANGE_START,
)
from shiftai.domain import (
    FacilitySettings,
    ObjectiveWeights,
    RequirementTable,
    Slot,
    StaffingStandard,
    build_slots,
    daterange,
)
from shiftai.relaxation import RelaxLevel
from shiftai.shift_patterns import ShiftPattern
from shiftai.ui.edit_history import EditHistory

KEY_SETTINGS = "settings"
KEY_STANDARD_KEY = "standard_key"
KEY_STANDARD = "standard"
KEY_STANDARD_OVERRIDES = "standard_overrides"
KEY_ENFORCE_MIN_TWO = "enforce_min_two"
KEY_CHILDREN = "children"
KEY_STAFF = "staff"
KEY_PREFERENCES = "preferences"
KEY_LOAD_RESULT = "load_result"
KEY_FRAMES = "frames"
KEY_DAYS = "days"
KEY_REQUIREMENTS = "requirements"
KEY_SOLVE_RESULT = "solve_result"
KEY_GAP_REPORT = "gap_report"
KEY_VIOLATIONS = "violations"
KEY_FIXED_ASSIGNMENTS = "fixed_assignments"
KEY_WEIGHTS = "weights"
KEY_GAS_CLIENT = "gas_client"

# 勤務パターンの整列（優先2: 早番・日勤・遅番へのスナップ）
KEY_PATTERNS_ENABLED = "patterns_enabled"
KEY_PATTERNS = "patterns"
KEY_PATTERN_SNAP = "pattern_snap"
KEY_PATTERN_SNAP_REPORT = "pattern_snap_report"
# 緩和モードと原因診断（優先3: Infeasible の特定）
KEY_RELAXATION = "relaxation"
KEY_DIAGNOSIS = "diagnosis"
KEY_DIAGNOSIS_LADDER = "diagnosis_ladder"
# データ投入タブの Undo/Redo とバリデーション（優先1）
KEY_EDIT_HISTORY = "edit_history"
KEY_VALIDATION = "validation"

KEY_PRESETS = "presets"
KEY_SOLVER_NAMES = "solver_names"
KEY_SLOTS = "slots"
KEY_SLOTS_ERROR = "slots_error"
KEY_TIME_LIMIT_SEC = "time_limit_sec"

DEFAULT_KEYS: dict[str, Any] = {
    KEY_SETTINGS: None,
    KEY_STANDARD_KEY: local_rules.DEFAULT_PRESET_KEY,
    KEY_STANDARD: None,
    KEY_STANDARD_OVERRIDES: {},
    KEY_ENFORCE_MIN_TWO: True,
    KEY_CHILDREN: [],
    KEY_STAFF: [],
    KEY_PREFERENCES: {},
    KEY_LOAD_RESULT: None,
    KEY_FRAMES: {},
    KEY_DAYS: [],
    KEY_REQUIREMENTS: None,
    KEY_SOLVE_RESULT: None,
    KEY_GAP_REPORT: None,
    KEY_VIOLATIONS: [],
    KEY_FIXED_ASSIGNMENTS: {},
    KEY_WEIGHTS: None,
    KEY_GAS_CLIENT: None,
    KEY_PRESETS: [],
    KEY_SOLVER_NAMES: [],
    KEY_SLOTS: (),
    KEY_SLOTS_ERROR: "",
    KEY_TIME_LIMIT_SEC: 60,
    KEY_PATTERNS_ENABLED: False,
    KEY_PATTERNS: (),
    KEY_PATTERN_SNAP: True,
    KEY_PATTERN_SNAP_REPORT: None,
    KEY_RELAXATION: int(RelaxLevel.STRICT),
    KEY_DIAGNOSIS: None,
    KEY_DIAGNOSIS_LADDER: None,
    KEY_EDIT_HISTORY: {},
    KEY_VALIDATION: None,
}

WEIGHT_WIDGETS: tuple[tuple[str, str, float, float, float], ...] = (
    ("shortfall_penalty", "配置不足のペナルティ", 100.0, 5000.0, 100.0),
    ("overstaff_penalty", "過剰配置のペナルティ", 0.0, 20.0, 1.0),
    ("preference_miss_penalty", "希望を無視するペナルティ", 0.0, 50.0, 5.0),
    ("hours_imbalance_penalty", "勤務時間偏りのペナルティ", 0.0, 50.0, 4.0),
    ("consecutive_day_penalty", "連続勤務日数ペナルティ", 0.0, 50.0, 8.0),
    ("fairness_early_penalty", "早番の偏りを減らすペナルティ", 0.0, 30.0, 1.0),
    ("fairness_late_penalty", "遅番の偏りを減らすペナルティ", 0.0, 30.0, 1.0),
    ("fairness_saturday_penalty", "土曜出勤の偏りを減らすペナルティ", 0.0, 30.0, 1.0),
)

FAIRNESS_WEIGHT_NAMES: tuple[str, ...] = (
    "fairness_early_penalty",
    "fairness_late_penalty",
    "fairness_saturday_penalty",
)
"""公平性の重みスライダー名（サイドバーの説明と解禁ロジックが共有する）。"""

WIDGET_KEYS: frozenset[str] = frozenset(
    {
        "day_close",
        "day_open",
        "facility_name",
        "gap_editor",
        "granularity_min",
        "labor_cost_per_hour",
        "late_care_relaxed",
        "min_staff_per_room",
        "range_days",
        "range_start",
        "shift_editor",
        "standard_preset",
    }
)
"""ウィジェット自身が所有するキー。生成済みキーは代入せず ``pop`` で破棄する。"""


def _default_settings() -> FacilitySettings:
    """既定の園設定（年間休業日は日曜のみ）。"""
    start = DEFAULT_RANGE_START
    sundays = frozenset(
        d for d in daterange(start, start + timedelta(days=364)) if d.weekday() == 6
    )
    return FacilitySettings(
        day_open=DEFAULT_DAY_OPEN,
        day_close=DEFAULT_DAY_CLOSE,
        granularity_min=DEFAULT_GRANULARITY_MIN,
        closed_days=sundays,
    )


def init_state() -> None:
    """未初期化のキーを既定値で埋める。副作用として GasConfig は作らない。"""
    for key, value in DEFAULT_KEYS.items():
        if key not in st.session_state:
            st.session_state[key] = copy.deepcopy(value)
    if st.session_state[KEY_SETTINGS] is None:
        st.session_state[KEY_SETTINGS] = _default_settings()
    if st.session_state[KEY_WEIGHTS] is None:
        st.session_state[KEY_WEIGHTS] = ObjectiveWeights()
    if st.session_state[KEY_STANDARD] is None:
        st.session_state[KEY_STANDARD] = local_rules.get_standard(
            st.session_state[KEY_STANDARD_KEY]
        )
    if not st.session_state[KEY_PRESETS]:
        st.session_state[KEY_PRESETS] = local_rules.list_presets()
    if not st.session_state[KEY_SOLVER_NAMES]:
        st.session_state[KEY_SOLVER_NAMES] = solver.available_solvers()


def get(key: str, default: Any = None) -> Any:
    """session_state から値を返す。未設定なら既定値（コピー）を返す。"""
    value = st.session_state.get(key, None)
    if value is None:
        fallback = DEFAULT_KEYS.get(key, None)
        if fallback is None:
            return default
        return copy.deepcopy(fallback)
    return value


def set(key: str, value: Any) -> None:
    """session_state に値を書き込む。生成済みウィジェットキーは ``pop`` 後に代入する。"""
    if key in WIDGET_KEYS:
        st.session_state.pop(key, None)
    st.session_state[key] = value


def reset(key: str) -> None:
    """1つのキーを既定値に戻す。未知のキーは削除する。"""
    if key not in DEFAULT_KEYS:
        st.session_state.pop(key, None)
        return
    if key in WIDGET_KEYS:
        st.session_state.pop(key, None)
        return
    st.session_state[key] = copy.deepcopy(DEFAULT_KEYS[key])


def reset_all() -> None:
    """管理下のキーのみを既定値に戻す（ウィジェットキーは破棄して既定値で再生成させる）。"""
    for key in DEFAULT_KEYS:
        reset(key)
    init_state()


@contextmanager
def editing_guard(key: str) -> Iterator[None]:
    """一括更新中に例外が起きたら直前の値へ巻き戻すガード。"""
    previous = copy.deepcopy(st.session_state.get(key, None))
    try:
        yield
    except Exception:
        st.session_state[key] = previous
        raise


def current_standard() -> StaffingStandard:
    """現在の適用基準（プリセット + 上乗せ）を返す。"""
    return get(KEY_STANDARD) or local_rules.get_standard(get(KEY_STANDARD_KEY))


def current_settings() -> FacilitySettings:
    """現在の園設定を返す。"""
    return get(KEY_SETTINGS) or _default_settings()


def current_days() -> list[date]:
    """計画期間の日付リストを返す。"""
    days = get(KEY_DAYS)
    return list(days) if days else []


def current_slots() -> tuple[Slot, ...]:
    """園設定から時間帯を生成して返す。

    開所・閉所時刻が不正（閉所 ≤ 開所など）のときは例外を送出せず
    空タプルを返す。``current_slots_error`` に理由を保持し、
    UI（``sidebar``）がその文言を ``st.error`` で表示する。
    以前はここが ``ValueError`` を握り潰して空だけを返し、
    呼び出し側の ``try/except ValueError`` によるエラー表示が
    **到達不能**になっていた。
    """
    cached = get(KEY_SLOTS)
    if cached:
        return tuple(cached)
    settings = current_settings()
    try:
        slots = build_slots(settings.day_open, settings.day_close, settings.granularity_min)
    except ValueError as exc:
        st.session_state[KEY_SLOTS_ERROR] = str(exc)
        st.session_state[KEY_SLOTS] = ()
        return ()
    st.session_state[KEY_SLOTS_ERROR] = ""
    st.session_state[KEY_SLOTS] = slots
    return slots


def current_slots_error() -> str:
    """直近の ``current_slots()`` が失敗した理由（空文字列なら正常）。"""
    return str(st.session_state.get(KEY_SLOTS_ERROR, "") or "")


def refresh_days(start: date, count: int) -> list[date]:
    """計画期間を再計算して session_state に保存する。"""
    count = max(1, int(count))
    days = list(daterange(start, start + timedelta(days=count - 1)))
    set(KEY_DAYS, days)
    return days


def data_ready() -> bool:
    """職員データが 1 名以上あるか。"""
    return len(get(KEY_STAFF) or []) > 0


def invalidate_pipeline() -> None:
    """データや基準が変わったときに最適化結果を破棄する。"""
    for key in (
        KEY_REQUIREMENTS,
        KEY_SOLVE_RESULT,
        KEY_GAP_REPORT,
        KEY_VIOLATIONS,
        KEY_FIXED_ASSIGNMENTS,
    ):
        reset(key)


def normalize_fixed(
    mapping: dict[tuple[str, Any, str], Any] | None,
) -> dict[tuple[str, date, str], Any]:
    """``fixed_assignments`` のキーを ``(職員ID, date, 時間帯)`` に正規化する。

    UI 側は ISO 文字列の日付を扱いやすいが、ソルバは ``datetime.date`` を要求する。
    """
    from shiftai.domain import CellState

    out: dict[tuple[str, date, str], CellState] = {}
    for key, state in (mapping or {}).items():
        try:
            staff_id, day, label = key
        except (TypeError, ValueError):
            continue
        if not isinstance(day, date):
            try:
                day = date.fromisoformat(str(day))
            except ValueError:
                continue
        cell = state if isinstance(state, CellState) else CellState(str(state))
        out[(str(staff_id), day, str(label))] = cell
    return out


def weights_from_state() -> ObjectiveWeights:
    """スライダーで調整された重みを ``ObjectiveWeights`` に落とし込む。"""
    base = get(KEY_WEIGHTS) or ObjectiveWeights()
    for name, _label, _lo, _hi, _step in WEIGHT_WIDGETS:
        widget_value = st.session_state.get(f"weight_{name}", None)
        if widget_value is not None:
            setattr(base, name, float(widget_value))
    return base


def sync_weights() -> ObjectiveWeights:
    """重みを反映して session_state に保存する。"""
    weights = weights_from_state()
    set(KEY_WEIGHTS, weights)
    return weights


def staff_name_map() -> dict[str, str]:
    """職員ID → 氏名 の辞書。"""
    return {member.staff_id: member.name for member in (get(KEY_STAFF) or [])}


def contract_hours() -> float:
    """対象期間中に供給できる総契約時間（時間単位）の概算。

    以前はこの関数で「週契約時間 × 日数/5」を独自に計算しており、
    ``solver.supply_hours``（実際の配置可能人時）と約 1.2〜1.3 倍乖離していた。
    UI が「必要／供給比 0.85 で充足可能」と緑表示でも、ソルバが不足を返す
    状の表示と実態の乖離が発生していた。ソルバと同一の式を使う。
    """
    table = get(KEY_REQUIREMENTS)
    staff = get(KEY_STAFF) or []
    if not isinstance(table, RequirementTable) or not staff:
        return 0.0
    return solver.supply_hours(staff, table, get(KEY_PREFERENCES) or {}, current_settings())


def supply_demand_ratio(required_hours: float) -> float:
    """必要人時 ÷ 供給可能人時 の比。1.0 を超えると構造的に不足する。

    供給可能人時が 0 のとき、必要人時が正なら比は ``inf``（=構造的に不可能）、
    必要人時も 0 なら ``0.0`` を返す。**0.0 を返してはならない**——
    0.0 は「必要量は満たされている」という意味になり、UI が
    ``st.success`` で「基準を満たせる状態です」と誤表示する。
    """
    supply = contract_hours()
    if required_hours <= 0:
        return 0.0
    if supply <= 0:
        return float("inf")
    return float(required_hours) / float(supply)


# ---------------------------------------------------------------------------
# 勤務パターン（優先2）
# ---------------------------------------------------------------------------


def current_patterns() -> tuple:
    """有効化されている勤務パターンを返す（無効なら空タプル）。"""
    if not get(KEY_PATTERNS_ENABLED):
        return ()
    return tuple(get(KEY_PATTERNS) or ())


def set_patterns(patterns: Sequence[ShiftPattern]) -> None:
    """勤務パターンを保存する（無効のままでも保持はする）。"""
    set(KEY_PATTERNS, tuple(patterns))


# ---------------------------------------------------------------------------
# 編集履歴（優先1）
# ---------------------------------------------------------------------------


def edit_history(kind: str) -> EditHistory:
    """その表の編集履歴を返す（未作成なら新規に作る）。"""
    store = st.session_state.setdefault(KEY_EDIT_HISTORY, {})
    history = store.get(kind)
    if not isinstance(history, EditHistory):
        history = EditHistory()
        store[kind] = history
    return history


def reset_edit_histories() -> None:
    """全表の編集履歴を破棄する（読み込み・サンプル投入のとき）。"""
    st.session_state[KEY_EDIT_HISTORY] = {}
