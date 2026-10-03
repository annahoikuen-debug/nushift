"""UI 層の session_state 管理。

``StaffingStandard.ratios`` が ``Mapping`` フィールドでハッシュ不能、
``list[ChildPlan]`` も ``st.cache_data`` の ``hash_func`` に向かないため、
再計算は ``st.cache_data`` ではなく **session_state + 明示的なボタントリガ** で駆動する。
"""

from __future__ import annotations

import copy
import hashlib
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
    DEFAULT_TIME_LIMIT_SEC,
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
KEY_INPUT_FINGERPRINT = "input_fingerprint"
"""計算結果に影響する入力の指紋。最後に計算したときと同じかを比べる。"""

KEY_FIX_TARGET = "ui_fix_target"
"""修正対象を指定した ``(日付の ISO 文字列, 職員ID, 時間帯ラベル)``。"""

KEY_FIX_REQUESTED = "ui_fix_requested"
"""修正対象の指定が新しい Ones かどうか。読み終えたら ``False`` に戻す。"""

KEY_STALE_REASON = "stale_reason"
"""入力が計算後に変わった理由の日本語短文。空文字なら最新。"""
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
    KEY_TIME_LIMIT_SEC: DEFAULT_TIME_LIMIT_SEC,
    KEY_PATTERNS_ENABLED: False,
    KEY_PATTERNS: (),
    KEY_PATTERN_SNAP: True,
    KEY_PATTERN_SNAP_REPORT: None,
    KEY_RELAXATION: int(RelaxLevel.STRICT),
    KEY_DIAGNOSIS: None,
    KEY_DIAGNOSIS_LADDER: None,
    KEY_EDIT_HISTORY: {},
    KEY_VALIDATION: None,
    KEY_INPUT_FINGERPRINT: "",
    KEY_STALE_REASON: "",
    KEY_FIX_TARGET: None,
    KEY_FIX_REQUESTED: False,
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
    """職員データが 1 名以上あるか。

    園児が 0 名でも職員がいれば真とする。園児だけのセッションでは
    ``children_missing()`` と ``staff_missing()`` で区別する。
    従来は職員しか見ていなかったため、園児だけ投入したセッションで
    精确な案内が出ていなかった。
    """
    return not staff_missing()


def children_missing() -> bool:
    """園児データが 1 件もないか。"""
    return len(get(KEY_CHILDREN) or []) == 0


def staff_missing() -> bool:
    """職員データが 1 名もないか。"""
    return len(get(KEY_STAFF) or []) == 0


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


def _fingerprint_parts() -> list[str]:
    """指紋の材料になる文字列の並びを返す（並べ順が安定したものだけ）。

    ``dict`` や ``set`` をそのまま ``str()`` すると並びが安定しないため、
    集合は ``sorted`` してから ISO 文字列にして渡す。
    """
    settings = current_settings()
    parts: list[str] = [
        f"open={settings.day_open.isoformat()}",
        f"close={settings.day_close.isoformat()}",
        f"gran={int(settings.granularity_min)}",
        f"cost={float(settings.labor_cost_per_hour):.4f}",
        "closed=" + ",".join(sorted(d.isoformat() for d in settings.closed_days)),
        "holiday=" + ",".join(sorted(d.isoformat() for d in settings.holiday_dates)),
        f"name={settings.facility_name}",
    ]
    days = current_days()
    parts.append(f"days={len(days)}")
    if days:
        parts.append(f"from={min(days).isoformat()}")
        parts.append(f"to={max(days).isoformat()}")
    else:
        parts.append("from=")
        parts.append("to=")
    standard = current_standard()
    parts.append(f"std={standard.name}")
    parts.append(
        "ratios="
        + ",".join(
            f"{key}:{standard.ratios[key].children_per_staff:.6f}"
            f":{standard.ratios[key].rounding}"
            for key in sorted(standard.ratios)
        )
    )
    parts.append(f"min_two={get(KEY_ENFORCE_MIN_TWO)}")
    load_result = get(KEY_LOAD_RESULT)
    if load_result is None:
        parts.append("load=0")
    else:
        parts.append(
            "load="
            f"{len(getattr(load_result, 'children', ()) or ())}/"
            f"{len(getattr(load_result, 'staff', ()) or ())}/"
            f"{len(getattr(load_result, 'preferences', ()) or ())}"
        )
    return parts


def compute_fingerprint() -> str:
    """計算結果に影響する入力から安定した指紋を文字列で返す。

    対象は園設定（開所閉所時刻・粒度・休業日・祝日・人件費・園名）、
    計画期間、適用基準（定員比と 2 名ルール）、データ投入結果。

    目的関数の重みは含めない。重みは解の形を変えるが、必要人員表を
    無効化する必要はないため。

    同じ入力からは必ず同じ文字列を返す。
    """
    payload = "|".join(_fingerprint_parts())
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def mark_inputs_changed(reason: str) -> None:
    """入力が変わったときに呼ぶ。計算結果を破棄し理由を記録する。

    ``reason`` はそのまま画面に出す日本語の短文（例: 計画期間を変更しました）。
    """
    set(KEY_STALE_REASON, str(reason))
    invalidate_pipeline()
    set(KEY_INPUT_FINGERPRINT, compute_fingerprint())


def clear_stale() -> None:
    """シフト作成が完了したので古い印を外す。"""
    set(KEY_STALE_REASON, "")


def mark_inputs_baseline() -> None:
    """読み込み直後など、計算結果を破棄せずに基準の指紋だけを記録する。

    これがないと同じセッションで **最初の 1 回だけ** 入力変更が
    検知されない（指紋が空なので初回とみなされる）。読み込み成功の
    タイミングで必ず呼ぶこと。
    """
    set(KEY_INPUT_FINGERPRINT, compute_fingerprint())
    set(KEY_STALE_REASON, "")


def set_fix_target(target: tuple[str, str, str] | None) -> None:
    """修正対象を指定する。

    ``target`` は ``(日付の ISO 文字列, 職員ID, 時間帯ラベル)``。
    ``None`` で解除する。
    """
    set(KEY_FIX_TARGET, target)


def take_fix_target() -> tuple[str, str, str] | None:
    """指定された修正対象を 1 回だけ取り出して ``None`` にする。

    Streamlit は再描画のたびにスクリプトを再実行する。消さないと同じ
    修正が毎回指示され、意図しないスクロール眼花を起こす。
    """
    target = get(KEY_FIX_TARGET)
    set(KEY_FIX_TARGET, None)
    return target


STAGE_EMPTY = "empty"
"""データがまだ投入されていない。"""

STAGE_LOADED = "loaded"
"""データは投入済みだが、必要人員もシフトも計算していない。"""

STAGE_STALE = "stale"
"""入力が計算後に変わっている。理由は ``KEY_STALE_REASON`` にある。"""

STAGE_READY = "ready"
"""必要人員が入力と整合している。シフトは未作成。"""

STAGE_SOLVED = "solved"
"""シフトが入力と整合している。"""

#: 表示側（``theme``）が使う並び順。``empty`` が先頭。
STAGE_ORDER: tuple[str, ...] = (
    STAGE_EMPTY,
    STAGE_LOADED,
    STAGE_STALE,
    STAGE_READY,
    STAGE_SOLVED,
)


def pipeline_stage() -> str:
    """現在の状態を :data:`STAGE_EMPTY` などのいずれかで返す。

    判定順は固定する。順序を変えると同じ画面状態に対して違う文言が出る。

    1. ``KEY_STALE_REASON`` が非空なら ``stale``
    2. ``KEY_LOAD_RESULT`` が ``None`` なら ``empty``
    3. ``KEY_SOLVE_RESULT`` が ``None`` かつ ``KEY_REQUIREMENTS`` が ``None``
       なら ``loaded``
    4. ``KEY_SOLVE_RESULT`` が ``None`` なら ``ready``
    5. それ以外は ``solved``

    指紋の比較はこの関数では行わない。不一致の検出は
    ``mark_inputs_changed`` が ``KEY_STALE_REASON`` に残すことに依存している。
    """
    if get(KEY_STALE_REASON):
        return STAGE_STALE
    if get(KEY_LOAD_RESULT) is None:
        return STAGE_EMPTY
    if get(KEY_SOLVE_RESULT) is None and get(KEY_REQUIREMENTS) is None:
        return STAGE_LOADED
    if get(KEY_SOLVE_RESULT) is None:
        return STAGE_READY
    return STAGE_SOLVED


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
