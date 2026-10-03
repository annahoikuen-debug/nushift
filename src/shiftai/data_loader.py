"""CSV / Excel / JSON からの入力読込と検証。

本モジュールは「1行の不正で全体が落ちない」ことを最優先とし、
読み込めない行は ``LoadIssue`` として積み上げて読み込みを継続する。
UI 層では ``LoadResult.issues`` をそのまま表形式で表示できる。
"""

from __future__ import annotations

import io
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from shiftai import config
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
    StaffPreferences,
    Unavailability,
)

CHILDREN_COLUMNS: list[str] = [
    "園児ID",
    "氏名",
    "年齢",
    "登園日",
    "登園時刻",
    "降園時刻",
    "短時間保育",
    "欠席",
    "欠席理由",
    "早朝保育",
    "延長保育",
    "備考",
]

STAFF_COLUMNS: list[str] = [
    "職員ID",
    "氏名",
    "資格（主）",
    "資格（副）",
    "雇用形態",
    "週契約時間",
    "1日契約時間",
    "月間最小時間",
    "月間最大時間",
    "週最大出勤日数",
    "最大連続勤務日数",
    "最早始業",
    "最遅終業",
    "能力タグ",
    "備考",
]

PREFERENCE_COLUMNS: list[str] = [
    "職員ID",
    "種別",
    "日付",
    "開始",
    "終了",
    "理由",
]

SHIFT_COLUMNS: list[str] = [
    "職員ID",
    "氏名",
    "日付",
    "時間帯",
    "状態",
    "勤務分数",
    "休憩分数",
]

TABLE_COLUMNS: dict[str, list[str]] = {
    "children": CHILDREN_COLUMNS,
    "staff": STAFF_COLUMNS,
    "preferences": PREFERENCE_COLUMNS,
    "shift": SHIFT_COLUMNS,
}

DEFAULT_FILENAMES: dict[str, str] = {
    "children": "children.csv",
    "staff": "staff.csv",
    "preferences": "preferences.csv",
    "shift": "shift.csv",
}

ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp932")

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"}

TRUE_TOKENS = frozenset(
    {"true", "1", "yes", "y", "t", "はい", "ある", "○", "◯", "〇", "有", "利用", "使う"}
)
FALSE_TOKENS = frozenset(
    {"false", "0", "no", "n", "f", "いいえ", "なし", "無し", "×", "✕", "✗", "x", "無", "利用しない"}
)

PREFERENCE_TYPES: dict[str, str] = {
    "希望休": "希望休",
    "希望": "希望休",
    "出勤不可": "出勤不可",
    "不可": "出勤不可",
    "出勤希望": "出勤希望",
    "出勤希望日": "出勤希望",
    "休み希望": "休み希望",
    "希望休希望": "休み希望",
}

_ROLE_ALIASES: dict[str, Role] = {
    "保育士": Role.HOIKUSHI,
    "保母": Role.HOIKUSHI,
    "hoikushi": Role.HOIKUSHI,
    "子育て支援員": Role.SHIENSHIIN,
    "支援員": Role.SHIENSHIIN,
    "しえんしいん": Role.SHIENSHIIN,
    "shienshiin": Role.SHIENSHIIN,
    "幼稚園教諭": Role.YOUCHUIN,
    "教論": Role.YOUCHUIN,
    "ようちゅうきょうゆ": Role.YOUCHUIN,
    "youchuin": Role.YOUCHUIN,
    "看護師": Role.KANGSHI,
    "かんごし": Role.KANGSHI,
    "kangshi": Role.KANGSHI,
    "栄養教諭": Role.EIYOU,
    "eiyou": Role.EIYOU,
    "調理員": Role.CHUUBOU,
    "調理師": Role.CHUUBOU,
    " choosable": Role.CHUUBOU,
    "chuubou": Role.CHUUBOU,
    "薬剤師": Role.YAKUARIN,
    "やくざいん": Role.YAKUARIN,
    "yakuarin": Role.YAKUARIN,
    "園長・主任(配置対象外)": Role.ENJOGAKUIN,
    "園長・主任（配置対象外）": Role.ENJOGAKUIN,
    "園長": Role.ENJOGAKUIN,
    "主任": Role.ENJOGAKUIN,
    "enjugakuin": Role.ENJOGAKUIN,
}

_EMPLOYMENT_ALIASES: dict[str, EmploymentType] = {
    "正職員": EmploymentType.SEI,
    "正規": EmploymentType.SEI,
    "常勤": EmploymentType.SEI,
    "sei": EmploymentType.SEI,
    "パート": EmploymentType.PART,
    "parttime": EmploymentType.PART,
    "ptime": EmploymentType.PART,
    "契約社員": EmploymentType.UKEIYOU,
    "ukeiyou": EmploymentType.UKEIYOU,
    "アルバイト": EmploymentType.BUNKIN,
    "arubaito": EmploymentType.BUNKIN,
    "bunkin": EmploymentType.BUNKIN,
}


_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "園児ID": (
        "園児id",
        "園児no",
        "園児番号",
        "子id",
        "子类id",
        "childid",
        "childno",
        "id",
        "園児コード",
    ),
    "氏名": ("氏名", "名前", "お名前", "name", "園児名", "職員名"),
    "年齢": ("年齢", "歳", "歳児", "年齢歳", "age", "ageyears", "園児年齢"),
    "登園日": ("登園日", "日付", "利用日", "出勤日", "day", "date", "登園日付"),
    "登園時刻": (
        "登園時刻",
        "登園time",
        "登園",
        "到着時刻",
        "到着",
        "arrive",
        "arrival",
        "start",
        "出勤時刻",
    ),
    "降園時刻": (
        "降園時刻",
        "降園time",
        "降園",
        "退園時刻",
        "退園",
        "depart",
        "departure",
        "end",
        "退園時刻",
    ),
    "短時間保育": (
        "短時間保育",
        "短保育",
        "短時間",
        "短時間保育園児",
        "shorttime",
        "short",
        "定時保育",
    ),
    "欠席": ("欠席", "不在", "欠勤", "absent", "欠席予定"),
    "欠席理由": ("欠席理由", "欠席理由備考", "理由", "absentreason", "欠席理由memo"),
    "早朝保育": ("早朝保育", "早朝", "早朝保育利用", "early", "earlycare", "早朝利用"),
    "延長保育": ("延長保育", "延長", "延長保育利用", "late", "latecare", "延長利用"),
    "備考": ("備考", "メモ", "note", "notes", "memo", "コメント", "remarks"),
    "職員ID": (
        "職員id",
        "スタッフid",
        "職員no",
        "職員番号",
        "staffid",
        "staffno",
        "id",
        "職員コード",
    ),
    "資格（主）": ("資格主", "主資格", "資格", "資格1", "primaryrole", "role", "職種", "主職種"),
    "資格（副）": ("資格副", "副資格", "資格2", "副職種", "secondaryrole", "副"),
    "雇用形態": ("雇用形態", "雇用区分", "contracttype", "employment", "employmenttype", "区分"),
    "週契約時間": ("週契約時間", "週契約", "週所定労働時間", "weeklyhours", "weekhours", "週時間"),
    "1日契約時間": (
        "1日契約時間",
        "1日契約",
        "日契約時間",
        "1日所定労働時間",
        "dailyhours",
        "dayhours",
        "日時間",
    ),
    "月間最小時間": (
        "月間最小時間",
        "月最小時間",
        "月間最小",
        "minmonthlyhours",
        "minhours",
        "月最小",
    ),
    "月間最大時間": (
        "月間最大時間",
        "月最大時間",
        "月間最大",
        "maxmonthlyhours",
        "maxhours",
        "月最大",
    ),
    "週最大出勤日数": (
        "週最大出勤日数",
        "週最大日数",
        "週出勤日数上限",
        "maxweeklydays",
        "maxdays",
        "週最大出勤",
    ),
    "最大連続勤務日数": (
        "最大連続勤務日数",
        "連続勤務日数上限",
        "maxconsecutivedays",
        "maxconsecutive",
        "連続出勤上限",
    ),
    "最早始業": ("最早始業", "最早開始", "始業時刻", "earlieststart", "earliest", "最早始業時刻"),
    "最遅終業": ("最遅終業", "最遅終了", "終業時刻", "latestend", "latest", "最遅終業時刻"),
    "能力タグ": (
        "能力タグ",
        "スキル",
        "技能",
        "タグ",
        "保有資格",
        "skills",
        "tags",
        "skill",
        "特技",
    ),
    "種別": ("種別", "種類", "希望種別", "type", "kind", "希望区分"),
    "開始": ("開始", "開始時刻", "start", "starttime", "from"),
    "終了": ("終了", "終了時刻", "end", "endtime", "to"),
    "理由": ("理由", "希望理由", "reason", "備考", "note"),
    "日付": ("日付", "date", "day", "希望日", "対象日"),
    "時間帯": ("時間帯", "時間", "slot", "slotlabel", "時間帯名"),
    "状態": ("状態", "state", "勤務状態", "セル状態"),
    "勤務分数": ("勤務分数", "分数", "workminutes", "worked", "workmin"),
    "休憩分数": ("休憩分数", "休息分数", "breakminutes", "breakmin", "休憩分"),
    "施設名": ("施設名", "園名", "facility", "facilityname"),
}

_CANONICAL: dict[str, str] = {
    unicodedata.normalize("NFKC", name).replace(" ", "").replace("　", "").lower(): name
    for name in (
        *CHILDREN_COLUMNS,
        *STAFF_COLUMNS,
        *PREFERENCE_COLUMNS,
        *SHIFT_COLUMNS,
        "施設名",
    )
}

_NON_WORD = re.compile(r"[\s　_\-()\[\]{}<>\"'、。,./\\:：*#!$%&+=|~@`^]+")


def normalize_header(name: Any) -> str:
    """ヘッダ文字列を照合用に正規化する（全角半角・空白・記号を無視）。"""
    text = unicodedata.normalize("NFKC", str(name)).strip()
    return _NON_WORD.sub("", text).lower()


@dataclass
class LoadIssue:
    """読み込み時に検出した 1 件の問題。"""

    level: str
    row: int
    column: str
    message: str

    def __str__(self) -> str:
        where = f"第{self.row + 1}行" if self.row >= 0 else "全体"
        return f"[{self.level}] {where} {self.column}: {self.message}"


@dataclass
class LoadResult:
    """読込結果のまとめ。"""

    children: list[ChildPlan] = field(default_factory=list)
    staff: list[StaffMember] = field(default_factory=list)
    preferences: dict[str, StaffPreferences] = field(default_factory=dict)
    issues: list[LoadIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[LoadIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[LoadIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        """致命的エラーがなければ True。"""
        return not self.errors

    def summary(self) -> str:
        """UI 表示用の日本語サマリ。"""
        if not self.issues:
            return (
                f"読み込み完了: 園児 {len(self.children)} 名 / "
                f"職員 {len(self.staff)} 名 / 希望休 {len(self.preferences)} 名"
            )
        head = (
            f"読み込み: 園児 {len(self.children)} 名 / "
            f"職員 {len(self.staff)} 名 / 希望休 {len(self.preferences)} 名 "
            f"（エラー {len(self.errors)} 件・警告 {len(self.warnings)} 件）"
        )
        lines = [head]
        lines.extend(f"  - {issue}" for issue in self.issues[:20])
        if len(self.issues) > 20:
            lines.append(f"  - ... 他 {len(self.issues) - 20} 件")
        return "\n".join(lines)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return False
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        flag = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return isinstance(flag, (bool, np.bool_)) and bool(flag)


def _text(value: Any) -> str:
    """値を trimmed な文字列にする（NaN / None は空文字）。"""
    if _is_missing(value):
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date, time)):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _digits(text: str) -> str:
    """全角数字を半角に直し、空白を取り除く。"""
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("　", "")


def parse_date(value: Any) -> date | None:
    """``YYYY-MM-DD`` / ``2026/9/28`` / Excel の datetime / 数値を受け付ける。"""
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().date()
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = float(value)
        if 1.0 <= serial <= 2958465.0:
            return date(1899, 12, 30) + timedelta(days=int(serial))
        return None
    text = _digits(_text(value))
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y年%m月%d日",
        "%y/%m/%d",
        "%y-%m-%d",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    stamp = pd.to_datetime(text, errors="coerce")
    if stamp is not None and not pd.isna(stamp):
        return stamp.date()
    return None


def parse_time(value: Any) -> time | None:
    """``HH:MM`` / ``8時30分`` / Excel の datetime・分数を受け付ける。"""
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if hasattr(value, "to_pydatetime"):
        try:
            return value.to_pydatetime().time()
        except (TypeError, ValueError):
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        frac = float(value)
        if 0.0 <= frac < 1.0:
            total = int(round(frac * 24 * 60))
            return time(hour=total // 60, minute=total % 60)
        return None
    text = _digits(_text(value))
    if not text:
        return None
    text = (
        text.replace("時", ":")
        .replace("分", "")
        .replace("午前", "")
        .replace("午後", "")
        .replace(";", ":")
    )
    text = text.rstrip(":")
    if re.match(r"^\d{1,2}$", text):
        hour = int(text)
        return time(hour=hour) if 0 <= hour < 24 else None
    match = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3) or 0)
        if 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60:
            return time(hour=hour, minute=minute, second=second)
        return None
    match = re.match(r"^(\d{1,2})(\d{2})$", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return time(hour=hour, minute=minute)
    if text.isdigit() and len(text) in (3, 4, 5, 6):
        padded = text.zfill(4)
        hour, minute = int(padded[:2]), int(padded[2:4])
        if 0 <= hour < 24 and 0 <= minute < 60:
            return time(hour=hour, minute=minute)
    return None


def parse_bool(value: Any, default: bool = False) -> bool:
    """真偽値。真偽 both 表現を吸収し、判定不能なら ``default``。"""
    if _is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) != 0.0
    text = unicodedata.normalize("NFKC", _text(value)).strip().lower()
    if not text:
        return default
    if text in TRUE_TOKENS:
        return True
    if text in FALSE_TOKENS:
        return False
    return default


def parse_role_list(value: Any) -> tuple[Role, ...]:
    """``保育士|看護師`` のような資格文字列を ``Role`` 列へ。"""
    text = _text(value)
    if not text:
        return ()
    parts = re.split(r"[|、,，/／;；\n\r\t]+", text)
    roles: list[Role] = []
    for part in parts:
        raw = part.strip()
        if not raw:
            continue
        try:
            role = Role(raw)
        except ValueError:
            role = _ROLE_ALIASES.get(normalize_header(raw), "")
            if not isinstance(role, Role):
                role = ""
        if not isinstance(role, Role):
            continue
        if role not in roles:
            roles.append(role)
    return tuple(roles)


def _parse_employment(value: Any, default: EmploymentType) -> EmploymentType:
    text = _text(value)
    if not text:
        return default
    try:
        return EmploymentType(text)
    except ValueError:
        pass
    key = normalize_header(text)
    for name, member in EmploymentType.__members__.items():
        if normalize_header(name) == key:
            return member
    return _EMPLOYMENT_ALIASES.get(key, default)


def _parse_float(value: Any, default: float) -> float:
    text = _text(value)
    if not text:
        return default
    match = re.search(r"-?\d+(?:\.\d+)?", unicodedata.normalize("NFKC", text))
    if not match:
        return default
    try:
        return float(match.group(0))
    except ValueError:
        return default


def _parse_int(value: Any, default: int | None = None) -> int | None:
    text = _text(value)
    if not text:
        return default
    normalized = unicodedata.normalize("NFKC", text)
    match = re.search(r"-?\d+", normalized)
    if not match:
        return default
    try:
        return int(match.group(0))
    except ValueError:
        return default


def _parse_day_cap(value: Any, column: str) -> int:
    """出勤日数の上限を読む。``0`` は「上限なし」としてそのまま保つ。

    列が無い・空のときは既定の 5 日を返す。``0`` と「未入力」を区別するため
    ``_parse_int(...) or 5`` の形では書かない。``0`` はソルバ側で
    「週あたりの上限なし」と解釈される。
    """
    parsed = _parse_int(value, None)
    if parsed is None:
        return 5
    if parsed < 0:
        raise ValueError(f"{column} は 0 以上の整数で指定してください（入力: {parsed}）")
    return parsed


def _parse_age_class(value: Any) -> AgeClass | None:
    text = _text(value)
    if not text:
        return None
    normalized = unicodedata.normalize("NFKC", text)
    years = _parse_int(normalized, None)
    if years is not None and not re.search(r"[^\d\.\-]", normalized):
        return AgeClass.from_years(years)
    for member in AgeClass:
        if normalized == unicodedata.normalize("NFKC", member.value):
            return member
    for member in AgeClass:
        if normalized == unicodedata.normalize("NFKC", f"{member.years}歳"):
            return member
    if normalized in {"5", "5歳", "5歳児", "5歳児以上", "6", "6歳"}:
        return AgeClass.AGE_5
    key = normalize_header(normalized)
    for member in AgeClass:
        if normalize_header(member.value) == key:
            return member
    return None


def _row_index(label: Any, position: int) -> int:
    try:
        return int(label)
    except (TypeError, ValueError):
        return position


def infer_columns(df: pd.DataFrame, expected: Sequence[str]) -> dict[str, str]:
    """ヘッダのゆれを吸収し ``{実際の列名: 期待列名}`` を返す。"""
    mapping: dict[str, str] = {}
    used: set[str] = set()
    if df is None or len(df.columns) == 0:
        return mapping
    actuals = [str(c) for c in df.columns]
    normalized = {a: normalize_header(a) for a in actuals}

    for actual in actuals:
        key = normalized[actual]
        if key in _CANONICAL and _CANONICAL[key] in expected:
            target = _CANONICAL[key]
            if target not in used:
                mapping[actual] = target
                used.add(target)

    for actual in actuals:
        if actual in mapping:
            continue
        key = normalized[actual]
        if not key:
            continue
        for target in expected:
            if target in used:
                continue
            aliases = _COLUMN_ALIASES.get(target, ())
            if key in aliases or key in {normalize_header(a) for a in aliases}:
                mapping[actual] = target
                used.add(target)
                break

    for actual in actuals:
        if actual in mapping:
            continue
        key = normalized[actual]
        if not key:
            continue
        for target in expected:
            if target in used:
                continue
            if key.startswith(normalize_header(target)) or normalize_header(target).startswith(key):
                mapping[actual] = target
                used.add(target)
                break

    return mapping


class _RowView:
    """列名でアクセスできる 1 行ラッパ。"""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def get(self, column: str, default: Any = "") -> Any:
        return self._data.get(column, default)

    def text(self, column: str) -> str:
        return _text(self._data.get(column, ""))


def _iter_rows(df: pd.DataFrame, expected: Sequence[str], issues: list[LoadIssue]):
    """列名を正規化して ``(row_index, RowView)`` を順に返す。

    対応表に載らない列はユーザーが追加した余分な列なので無視し、
    1 度だけ警告を出す。以前は
    ``{mapping[str(col)]: value ...}`` で無条件に引いており、
    未知の列が 1 つ混ざっただけで ``KeyError`` になり全体が落ちていた。
    """
    mapping = infer_columns(df, expected)
    present = {v for v in mapping.values()}
    for missing in [c for c in expected if c not in present]:
        issues.append(
            LoadIssue(
                "warning", -1, missing, f"列「{missing}」が見つかりません（空として扱います）"
            )
        )
    known = {normalize_header(k) for k in mapping}
    unknown = [str(col) for col in df.columns if normalize_header(str(col)) not in known]
    if unknown:
        issues.append(
            LoadIssue(
                "warning",
                -1,
                ",".join(unknown[:10]),
                f"解釈できない列を無視します: {', '.join(unknown[:10])}",
            )
        )
    rows = []
    for position, (label, series) in enumerate(df.iterrows()):
        data: dict[str, Any] = {}
        for col, value in series.items():
            key = mapping.get(str(col))
            if key is not None:
                data[key] = value
        rows.append((_row_index(label, position), _RowView(data)))
    return rows


def load_children(df: pd.DataFrame) -> tuple[list[ChildPlan], list[LoadIssue]]:
    """園児（登降園予定）CSV を ``ChildPlan`` のリストに変換する。"""
    issues: list[LoadIssue] = []
    plans: list[ChildPlan] = []
    if df is None or len(df.columns) == 0:
        issues.append(
            LoadIssue("error", -1, "children", "園児データを読み込めませんでした（ヘッダが空です）")
        )
        return plans, issues

    seen: dict[tuple[str, str], int] = {}
    for row, view in _iter_rows(df, CHILDREN_COLUMNS, issues):
        child_id = view.text("園児ID")
        if not child_id:
            issues.append(LoadIssue("error", row, "園児ID", "園児ID が空です"))
            continue
        name = view.text("氏名") or f"園児{child_id}"
        age_class = _parse_age_class(view.get("年齢"))
        if age_class is None:
            issues.append(
                LoadIssue("error", row, "年齢", f"年齢を解釈できません: {view.text('年齢')!r}")
            )
            continue
        day = parse_date(view.get("登園日"))
        if day is None:
            issues.append(
                LoadIssue(
                    "error", row, "登園日", f"登園日を解釈できません: {view.text('登園日')!r}"
                )
            )
            continue
        key = (child_id, day.isoformat())
        if key in seen:
            issues.append(
                LoadIssue(
                    "error",
                    row,
                    "園児ID",
                    f"重複しています（{child_id} / {day.isoformat()} は {seen[key] + 1} 行目と重複）",
                )
            )
            continue
        seen[key] = row

        absent = parse_bool(view.get("欠席"))
        arrive = parse_time(view.get("登園時刻"))
        depart = parse_time(view.get("降園時刻"))
        if absent:
            arrive = arrive or time(0, 0)
            depart = depart or time(0, 0)
        else:
            if arrive is None:
                issues.append(
                    LoadIssue(
                        "error",
                        row,
                        "登園時刻",
                        f"登園時刻を解釈できません: {view.text('登園時刻')!r}",
                    )
                )
                continue
            if depart is None:
                issues.append(
                    LoadIssue(
                        "error",
                        row,
                        "降園時刻",
                        f"降園時刻を解釈できません: {view.text('降園時刻')!r}",
                    )
                )
                continue

        try:
            plans.append(
                ChildPlan(
                    child_id=child_id,
                    name=name,
                    day=day,
                    age_class=age_class,
                    arrive=arrive,
                    depart=depart,
                    is_short_time=parse_bool(view.get("短時間保育")),
                    absent=absent,
                    absent_reason=view.text("欠席理由"),
                    uses_early_care=parse_bool(view.get("早朝保育")),
                    uses_late_care=parse_bool(view.get("延長保育")),
                    notes=view.text("備考"),
                )
            )
        except ValueError as exc:
            issues.append(LoadIssue("error", row, "登園時刻", str(exc)))
    return plans, issues


def load_staff(df: pd.DataFrame) -> tuple[list[StaffMember], list[LoadIssue]]:
    """職員CSV を ``StaffMember`` のリストに変換する（希望休は別関数）。"""
    issues: list[LoadIssue] = []
    members: list[StaffMember] = []
    if df is None or len(df.columns) == 0:
        issues.append(
            LoadIssue("error", -1, "staff", "職員データを読み込めませんでした（ヘッダが空です）")
        )
        return members, issues

    seen: dict[str, int] = {}
    for row, view in _iter_rows(df, STAFF_COLUMNS, issues):
        staff_id = view.text("職員ID")
        if not staff_id:
            issues.append(LoadIssue("error", row, "職員ID", "職員ID が空です"))
            continue
        if staff_id in seen:
            issues.append(
                LoadIssue(
                    "error",
                    row,
                    "職員ID",
                    f"職員ID が重複しています（{seen[staff_id] + 1} 行目と重複）",
                )
            )
            continue
        seen[staff_id] = row

        roles = parse_role_list(view.get("資格（主）"))
        if not roles:
            issues.append(
                LoadIssue(
                    "error",
                    row,
                    "資格（主）",
                    f"資格（主）を解釈できません: {view.text('資格（主）')!r}",
                )
            )
            continue
        # parse_role_list は tuple[Role, ...] を返すので、そのまま append はできない
        # （以前は ``roles.append(extra)`` で AttributeError になり、
        #   「資格（副）」のある行を持つ CSV 全体が読めなくなっていた）。
        role_list = list(roles)
        for extra in parse_role_list(view.get("資格（副）")):
            if extra not in role_list:
                role_list.append(extra)
        roles_tuple = tuple(role_list)

        employment = _parse_employment(view.get("雇用形態"), EmploymentType.PART)
        weekly = _parse_float(view.get("週契約時間"), 0.0)
        daily = _parse_float(view.get("1日契約時間"), 0.0)
        if weekly <= 0:
            weekly = max(daily, 1.0) * 2.0
            issues.append(
                LoadIssue("warning", row, "週契約時間", f"週契約時間を既定値 {weekly:g} にしました")
            )
        if daily <= 0:
            daily = max(weekly / 5.0, 1.0)
            issues.append(
                LoadIssue(
                    "warning", row, "1日契約時間", f"1日契約時間を既定値 {daily:g} にしました"
                )
            )

        min_monthly = _parse_float(view.get("月間最小時間"), 0.0)
        max_monthly = _parse_float(view.get("月間最大時間"), 200.0)
        if max_monthly < min_monthly:
            issues.append(
                LoadIssue(
                    "warning",
                    row,
                    "月間最大時間",
                    "月間最大時間が月間最小時間未満のため入れ替えます",
                )
            )
            min_monthly, max_monthly = max_monthly, min_monthly

        earliest = parse_time(view.get("最早始業")) or time(6, 0)
        latest = parse_time(view.get("最遅終業")) or time(22, 0)
        if latest <= earliest:
            issues.append(
                LoadIssue(
                    "warning", row, "最遅終業", "最遅終業が最早始業以前のため 24:00 扱いにします"
                )
            )
            latest = config.DAY_END

        skills = frozenset(
            s.strip() for s in re.split(r"[|、,，/／\n\r]+", view.text("能力タグ")) if s.strip()
        )
        try:
            contract = Contract(
                weekly_hours=weekly,
                daily_hours=daily,
                employment_type=employment,
                min_monthly_hours=min_monthly,
                max_monthly_hours=max_monthly,
                max_weekly_days=_parse_day_cap(view.get("週最大出勤日数"), "週最大出勤日数"),
                max_consecutive_days=_parse_day_cap(
                    view.get("最大連続勤務日数"), "最大連続勤務日数"
                ),
                earliest_start=earliest,
                latest_end=latest,
            )
            members.append(
                StaffMember(
                    staff_id=staff_id,
                    name=view.text("氏名") or staff_id,
                    roles=roles_tuple,
                    contract=contract,
                    skills=skills,
                    memo=view.text("備考"),
                )
            )
        except ValueError as exc:
            issues.append(LoadIssue("error", row, "職員ID", str(exc)))
    return members, issues


def load_preferences(df: pd.DataFrame) -> tuple[dict[str, StaffPreferences], list[LoadIssue]]:
    """希望休CSV を ``職員ID -> StaffPreferences`` に変換する。"""
    issues: list[LoadIssue] = []
    prefs: dict[str, StaffPreferences] = {}
    if df is None or len(df.columns) == 0:
        return prefs, issues

    hard_kinds = {"希望休", "出勤不可"}
    for row, view in _iter_rows(df, PREFERENCE_COLUMNS, issues):
        staff_id = view.text("職員ID")
        if not staff_id:
            issues.append(LoadIssue("error", row, "職員ID", "職員ID が空です"))
            continue
        kind_raw = view.text("種別") or "希望休"
        kind = PREFERENCE_TYPES.get(kind_raw)
        if kind is None:
            issues.append(
                LoadIssue(
                    "warning",
                    row,
                    "種別",
                    f"未知の種別のため「希望休」として扱います: {kind_raw!r}",
                )
            )
            kind = "希望休"
        day = parse_date(view.get("日付"))
        if day is None:
            issues.append(
                LoadIssue("error", row, "日付", f"日付を解釈できません: {view.text('日付')!r}")
            )
            continue

        entry = prefs.setdefault(staff_id, StaffPreferences())
        reason = view.text("理由")
        if kind in hard_kinds:
            start = parse_time(view.get("開始")) or time(0, 0)
            end = parse_time(view.get("終了")) or config.DAY_END
            if end <= start:
                end = config.DAY_END
            entry.unavailable.append(
                Unavailability(day=day, start=start, end=end, reason=reason or kind)
            )
        elif kind == "出勤希望":
            entry.preferred_days = frozenset(entry.preferred_days) | {day}
        else:
            entry.preferred_off_days = frozenset(entry.preferred_off_days) | {day}

    for entry in prefs.values():
        entry.unavailable.sort(key=lambda u: (u.day, u.start))
    return prefs, issues


def load_bundle(
    children_df: pd.DataFrame | None = None,
    staff_df: pd.DataFrame | None = None,
    preferences_df: pd.DataFrame | None = None,
) -> LoadResult:
    """3 つの DataFrame をまとめて読み、問題を issues に積んで返す。"""
    result = LoadResult()
    if children_df is not None:
        children, issues = load_children(children_df)
        result.children = children
        result.issues.extend(issues)
    if staff_df is not None:
        staff, issues = load_staff(staff_df)
        result.staff = staff
        result.issues.extend(issues)
    if preferences_df is not None:
        prefs, issues = load_preferences(preferences_df)
        result.preferences = prefs
        result.issues.extend(issues)

    staff_ids = {m.staff_id for m in result.staff}
    for staff_id, entry in result.preferences.items():
        if staff_ids and staff_id not in staff_ids:
            result.issues.append(
                LoadIssue(
                    "warning", -1, "職員ID", f"希望休のある職員 {staff_id} が職員CSVにいません"
                )
            )
        for note in ("早朝を避けたい", "延長を避けたい"):
            if note in entry.notes:
                setattr(entry, "avoid_early" if "早朝" in note else "avoid_late", True)
    return result


def _decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _dataframe_from_json(payload: Any) -> pd.DataFrame:
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], (list, dict)):
            payload = payload["data"]
        else:
            records = []
            for key, value in payload.items():
                if isinstance(value, list):
                    frame = _dataframe_from_json(value)
                    frame.insert(0, "種別", key)
                    records.append(frame)
                else:
                    records.append(pd.DataFrame([{"種別": key, "値": value}]))
            return pd.concat(records, ignore_index=True) if records else pd.DataFrame()
    if isinstance(payload, list):
        if not payload:
            return pd.DataFrame()
        if all(isinstance(item, dict) for item in payload):
            return pd.DataFrame.from_records(payload)
        if all(isinstance(item, list) for item in payload):
            width = max(len(row) for row in payload)
            padded = [list(row) + [None] * (width - len(row)) for row in payload]
            return pd.DataFrame.from_records(padded[1:], columns=[str(c) for c in padded[0]])
    return pd.DataFrame()


def _is_excel(data: bytes, suffix: str = "") -> bool:
    if data[:4] == b"PK\x03\x04":
        return True
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return True
    return suffix.lower() in EXCEL_SUFFIXES


def _read_excel(data: bytes, sheet: Any = 0) -> pd.DataFrame:
    buffer = io.BytesIO(data)
    try:
        return pd.read_excel(buffer, sheet_name=sheet if sheet is not None else 0)
    except ImportError as exc:  # pragma: no cover - 依存不足時のみ
        raise ValueError(
            "Excel の読み込みには openpyxl（.xlsx）が必要です。.xls は CSV に変換してください"
        ) from exc
    except Exception as exc:
        raise ValueError(f"Excel を読み込めませんでした: {exc}") from exc


def read_table(source: Any, kind: str = "children") -> pd.DataFrame:
    """CSV / Excel / JSON を ``DataFrame`` として読み込む。

    ``source`` には ``Path`` / パス文字列 / ``bytes`` / ``DataFrame`` /
    ファイルライクオブジェクトのいずれかを渡せる。
    """
    if kind not in TABLE_COLUMNS:
        raise ValueError(
            f"kind は {sorted(TABLE_COLUMNS)} のいずれかを指定してください（got {kind!r}）"
        )

    if isinstance(source, pd.DataFrame):
        return source.copy()

    suffix = ""
    data: bytes | None = None

    if isinstance(source, bytes | bytearray):
        data = bytes(source)
    elif isinstance(source, Path):
        data = source.read_bytes()
        suffix = source.suffix
    elif isinstance(source, str):
        path: Path | None = None
        if "\n" not in source and "\r" not in source and len(source) < 4096:
            try:
                candidate = Path(source)
                if candidate.is_file():
                    path = candidate
            except (OSError, ValueError):
                path = None
        if path is not None:
            data = path.read_bytes()
            suffix = path.suffix
        else:
            data = source.encode("utf-8")
    elif hasattr(source, "read"):
        raw = source.read()
        if isinstance(raw, str):
            data = raw.encode("utf-8")
        elif isinstance(raw, bytes | bytearray):
            data = bytes(raw)
        else:
            raise ValueError(f"読み取りオブジェクトの型が不明です: {type(raw)!r}")
    else:
        raise ValueError(f"読み込み元として扱えない型です: {type(source)!r}")

    if not data:
        return pd.DataFrame(columns=TABLE_COLUMNS[kind])

    stripped = data.lstrip()
    if _is_excel(data, suffix):
        return _read_excel(data)

    if stripped[:1] in (b"{", b"["):
        try:
            return _dataframe_from_json(json.loads(_decode(data)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON を解釈できません: {exc}") from exc

    text = _decode(data)
    frame = pd.read_csv(io.StringIO(text))
    return frame


def read_bundle(children: Any = None, staff: Any = None, preferences: Any = None) -> LoadResult:
    """ファイル 3 つを直接渡して ``LoadResult`` を得る。"""
    return load_bundle(
        read_table(children, "children") if children is not None else None,
        read_table(staff, "staff") if staff is not None else None,
        read_table(preferences, "preferences") if preferences is not None else None,
    )


def _write_csv(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_template_csvs(target_dir: Path | str) -> dict[str, Path]:
    """各 1 行サンプルの入った空テンプレート CSV を出力する。"""
    from shiftai import sample_data

    base = Path(target_dir)
    base.mkdir(parents=True, exist_ok=True)
    days = [date.today()]
    children, staff, prefs = sample_data.make_dataset(days, seed=1)
    frames = sample_data.sample_dataframes(children=children, staff=staff, preferences=prefs)
    limits = {"children": 1, "staff": 1, "preferences": 1}
    out: dict[str, Path] = {}
    for key, columns in (
        ("children", CHILDREN_COLUMNS),
        ("staff", STAFF_COLUMNS),
        ("preferences", PREFERENCE_COLUMNS),
    ):
        frame = frames[key].head(limits[key]).reindex(columns=columns)
        out[key] = _write_csv(frame, base / DEFAULT_FILENAMES[key])
    return out
