"""入力表（園児・職員・希望休）のバリデーション。

**このモジュールが解決する問題**

データ投入タブ（タブ1）は ``st.data_editor`` で表を直接編集できる。
そこで「読込は通ったが、後の最適化で原因不明のエラーになる」ケースが頻発する。

    * 登園時刻が降園時刻より後になっている（``ChildPlan`` が ``ValueError`` を投げる）
    * 希望休の職員IDが職員表に存在しない（``load_bundle`` がその行を黙って捨てる）
    * 職員IDが重複している（同じ職員が二重に数えられる）
    * 1日の契約時間が週契約時間を超している（供給人時の計算が破綻する）
    * 最早始業が最遅終業より後になっている（その職員が一度も配置できない）

これらは**すべてタブ1で入力したその場で指摘すべき**ものである。
タブ2以降へ持ち込むと、どの制約が原因か現場が追跡できなくなる。

読み込み後に「黙って直される」ケースとの関係

``data_loader`` は悪い値を例外ではなく ``LoadIssue`` として集める。
一方で次の 3 つは「静かに直す」。

* 週契約時間が 0 以下なら既定値を勝手に当てはめる（``LoadIssue`` は warning）
* 最遅終業 <= 最早始業なら 23:59 に書き換える（warning）
* 希望休の 終了 <= 開始 なら 23:59 に書き換える（warning）

この「直し方」は現場から見て予測できないため、本モジュールは
**同じ入力を「入力したそのままの意味」で検証し直す**責務を持つ。
これは data_loader の仕様変更ではなく、
「この値は運用として成立しているか」を利用者の目に晒す新しいレイヤーである。

設計方針

* 副作用を持たない（Streamlit もソルバも触らない）。
* 深刻度は ``error``（修正しないと最適化できない）と ``warning``
  （最適化はできるが運用上の注意点）の 2 段階に留める。
* 行番号・列名をそのまま :class:`ValidationIssue` に持たせ、UI が表として描けるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from shiftai.data_loader import (
    CHILDREN_COLUMNS,
    PREFERENCE_COLUMNS,
    PREFERENCE_TYPES,
    STAFF_COLUMNS,
    parse_bool,
    parse_date,
    parse_time,
)

__all__ = [
    "ERROR",
    "LEVELS",
    "WARNING",
    "ValidationIssue",
    "ValidationReport",
    "analyze_children",
    "analyze_preferences",
    "analyze_staff",
    "validate_frames",
]

ERROR = "error"
WARNING = "warning"

LEVELS: tuple[str, str] = (ERROR, WARNING)

_TABLE_LABELS: dict[str, str] = {
    "children": "園児",
    "staff": "職員",
    "preferences": "希望休",
}

#: 希望休の種別として ``data_loader`` が解釈できる値（正規化後のもの）。
_KNOWN_PREF_KINDS = frozenset({"希望休", "出勤不可", "出勤希望", "休み希望"})


# ---------------------------------------------------------------------------
# 値のパース（``data_loader`` と同じ解釈に揃える）
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """欠損を空文字に落とした文字列。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and pd.isna(value):
        return ""
    try:
        if value is pd.NaT or pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date, time)):
        return str(value)
    return str(value).strip()


def _number(value: Any) -> float | None:
    """数値へ変換する。解釈できなければ ``None``。"""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    text = _text(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _minutes(value: time | None) -> int | None:
    """時刻を「午前0時からの分」に変換する。解釈できなければ ``None``。"""
    return None if value is None else value.hour * 60 + value.minute


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _columns_of(frame: pd.DataFrame, schema: Sequence[str]) -> set[str]:
    """スキーマに含まれ、かつ実際の表にも存在する列名。"""
    return {name for name in schema if name in frame.columns}


# ---------------------------------------------------------------------------
# 結果の型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationIssue:
    """指摘 1 件。"""

    level: str
    table: str
    row: int
    column: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.level == ERROR

    @property
    def table_label(self) -> str:
        return _TABLE_LABELS.get(self.table, self.table)

    @property
    def row_number(self) -> int:
        """人間に見せる 1 始まりの行番号（``st.data_editor`` はヘッダー込み）。"""
        return self.row + 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "深刻度": self.level,
            "表": self.table_label,
            "行": self.row_number,
            "列": self.column,
            "内容": self.message,
        }


@dataclass(frozen=True)
class ValidationReport:
    """検証結果のまとめ。"""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.is_error)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if not i.is_error)

    @property
    def has_errors(self) -> bool:
        """``error`` が 1 件でもあれば True（読み込みを止める条件）。"""
        return any(i.is_error for i in self.issues)

    def by_table(self, table: str) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.table == table)

    def count_errors(self, table: str | None = None) -> int:
        return sum(1 for i in self.errors if table is None or i.table == table)

    def count_warnings(self, table: str | None = None) -> int:
        return sum(1 for i in self.warnings if table is None or i.table == table)

    def to_dataframe(self) -> pd.DataFrame:
        """指摘を表として返す。指摘が無ければ空の DataFrame。"""
        if not self.issues:
            return pd.DataFrame(columns=["深刻度", "表", "行", "列", "内容"])
        order = {ERROR: 0, WARNING: 1}
        rows = sorted(
            self.issues,
            key=lambda i: (order.get(i.level, 9), i.table, i.row, i.column),
        )
        return pd.DataFrame.from_records([i.to_dict() for i in rows])

    def summary(self) -> str:
        """1 行のサマリー。"""
        if not self.issues:
            return "指摘はありません。"
        return (
            f"エラー {len(self.errors)} 件・注意 {len(self.warnings)} 件"
            f"（全 {len(self.issues)} 件）"
        )


# ---------------------------------------------------------------------------
# 園児
# ---------------------------------------------------------------------------


def analyze_children(
    frame: pd.DataFrame | None,
    *,
    days: Sequence[date] = (),
    day_open: time | None = None,
    day_close: time | None = None,
) -> list[ValidationIssue]:
    """園児表を検証する。

    :param frame: 検証する DataFrame（``None`` / 空なら何もしない）
    :param days: 計画期間（この期間外の登園日は warning）
    :param day_open: 園の開所時刻（この時間を超える登降園は warning）
    :param day_close: 園の閉所時刻
    :returns: 指摘のリスト
    """
    if frame is None or frame.empty:
        return []
    known = _columns_of(frame, CHILDREN_COLUMNS)
    out: list[ValidationIssue] = []
    period = set(days)
    open_min = _minutes(day_open)
    close_min = _minutes(day_close)
    seen: dict[tuple[str, str], int] = {}

    for pos, (_, row) in enumerate(frame.iterrows()):
        col = {name: row.get(name) for name in known}
        child_id = _text(col.get("園児ID"))
        if not child_id:
            out.append(
                ValidationIssue(
                    ERROR, "children", pos, "園児ID",
                    "園児IDが空です。1人1つのIDを設定してください。",
                )
            )

        arrive_day = parse_date(col.get("登園日"))
        if arrive_day is None:
            out.append(
                ValidationIssue(
                    ERROR, "children", pos, "登園日",
                    "登園日が解釈できません。YYYY-MM-DD で入力してください。",
                )
            )
        elif period and arrive_day not in period:
            out.append(
                ValidationIssue(
                    WARNING, "children", pos, "登園日",
                    f"登園日 {arrive_day.isoformat()} は計画期間の外です。"
                    "期間設定か園児データを確認してください。",
                )
            )

        if child_id and arrive_day is not None:
            key = (child_id, arrive_day.isoformat())
            if key in seen:
                out.append(
                    ValidationIssue(
                        ERROR, "children", pos, "園児ID",
                        f"園児ID {child_id} / {arrive_day.isoformat()} が重複しています"
                        f"（{seen[key] + 2} 行目と重複）。必要人員が二重に計算されます。",
                    )
                )
            else:
                seen[key] = pos

        age = _text(col.get("年齢"))
        if age and not age.isdigit():
            out.append(
                ValidationIssue(
                    WARNING, "children", pos, "年齢",
                    f"年齢「{age}」は 0〜5 の整数として解釈します（読み込み時に 0 歳児扱いになります）。",
                )
            )

        absent = parse_bool(col.get("欠席"), False)
        arrive = parse_time(col.get("登園時刻"))
        depart = parse_time(col.get("降園時刻"))
        if absent:
            # 欠席は時刻が無くても構わない（data_loader も 00:00 に丸める）
            pass
        else:
            if arrive is None:
                out.append(
                    ValidationIssue(
                        ERROR, "children", pos, "登園時刻",
                        "登園時刻が空です。HH:MM で入力してください。",
                    )
                )
            if depart is None:
                out.append(
                    ValidationIssue(
                        ERROR, "children", pos, "降園時刻",
                        "降園時刻が空です。HH:MM で入力してください。",
                    )
                )
        arrive_min, depart_min = _minutes(arrive), _minutes(depart)
        if arrive_min is not None and depart_min is not None and depart_min <= arrive_min:
            out.append(
                ValidationIssue(
                    ERROR, "children", pos, "降園時刻",
                    f"降園時刻 {depart.strftime('%H:%M')} が登園時刻 "
                    f"{arrive.strftime('%H:%M')} と同じか後になっています。"
                    "在園時間が 0 分以下となり最適化できません。",
                )
            )
        if open_min is not None and close_min is not None:
            for name, value, minutes in (
                ("登園時刻", arrive, arrive_min),
                ("降園時刻", depart, depart_min),
            ):
                if minutes is None:
                    continue
                if minutes < open_min:
                    out.append(
                        ValidationIssue(
                            WARNING, "children", pos, name,
                            f"{name} {value.strftime('%H:%M')} が園の開所 "
                            f"{day_open.strftime('%H:%M')} より前です。",
                        )
                    )
                elif minutes > close_min:
                    out.append(
                        ValidationIssue(
                            WARNING, "children", pos, name,
                            f"{name} {value.strftime('%H:%M')} が園の閉所 "
                            f"{day_close.strftime('%H:%M')} より後です。",
                        )
                    )

        reason = _text(col.get("欠席理由"))
        if absent and not reason:
            out.append(
                ValidationIssue(
                    ERROR, "children", pos, "欠席理由",
                    "欠席がオンですが理由が空です。欠席理由を入力してください。",
                )
            )
        if not absent and reason:
            out.append(
                ValidationIssue(
                    WARNING, "children", pos, "欠席",
                    "欠席理由が入力されていますが「欠席」がオフです。在園として扱われます。",
                )
            )
        if (
            parse_bool(col.get("短時間保育"), False)
            and parse_bool(col.get("延長保育"), False)
        ):
            out.append(
                ValidationIssue(
                    WARNING, "children", pos, "延長保育",
                    "短時間保育と延長保育の両方がオンです。延長保育が選択されます"
                    "（保育標準時間帯の集計対象から外れます）。",
                )
            )
    return out


# ---------------------------------------------------------------------------
# 職員
# ---------------------------------------------------------------------------


def analyze_staff(
    frame: pd.DataFrame | None,
    *,
    day_open: time | None = None,
    day_close: time | None = None,
) -> list[ValidationIssue]:
    """職員表を検証する。

    供給人時（``solver.supply_hours``）の計算に使われる値
    （週契約時間・1日契約時間・週最大出勤日数・最早始業・最遅終業）が
    破綻していないかを重点的に見る。``data_loader`` が「勝手に直す」値は
    ここでは warning として明示する。
    """
    if frame is None or frame.empty:
        return []
    known = _columns_of(frame, STAFF_COLUMNS)
    out: list[ValidationIssue] = []
    seen: dict[str, int] = {}
    open_min = _minutes(day_open)

    for pos, (_, row) in enumerate(frame.iterrows()):
        col = {name: row.get(name) for name in known}
        staff_id = _text(col.get("職員ID"))
        if not staff_id:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "職員ID",
                    "職員IDが空です。希望休と紐づかないので必ず設定してください。",
                )
            )
        elif staff_id in seen:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "職員ID",
                    f"職員ID {staff_id} が重複しています（{seen[staff_id] + 2} 行目と重複）。"
                    "同じ職員が二重に数えられ、必要人員の判定が変わります。",
                )
            )
        else:
            seen[staff_id] = pos

        weekly = _number(col.get("週契約時間"))
        daily = _number(col.get("1日契約時間"))
        if weekly is None or weekly <= 0:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "週契約時間",
                    f"週契約時間が空または 0 以下です（現在 {_text(col.get('週契約時間'))!r}）。"
                    "読み込み時に既定値へ置き換えられ、勤務時間が意図とズレます。",
                )
            )
        if daily is None or daily <= 0:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "1日契約時間",
                    f"1日の契約時間が空または 0 以下です（現在 {_text(col.get('1日契約時間'))!r}）。"
                    "読み込み時に既定値へ置き換えられ、勤務時間が意図とズレます。",
                )
            )
        if (
            weekly is not None
            and daily is not None
            and weekly > 0
            and daily > 0
            and daily > weekly
        ):
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "1日契約時間",
                    f"1日の契約時間 {daily:g} 時間が週契約時間 {weekly:g} 時間を超えています。"
                    "契約として成立しません。",
                )
            )

        min_month = _number(col.get("月間最小時間"))
        max_month = _number(col.get("月間最大時間"))
        if min_month is not None and max_month is not None and max_month < min_month:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "月間最大時間",
                    f"月間最大時間 {max_month:g} 時間が月間最小時間 {min_month:g} 時間未満です。"
                    "読み込み時に 2 つの値が入れ替わります。",
                )
            )

        weekly_days = _number(col.get("週最大出勤日数"))
        if weekly_days is not None and not 0 <= weekly_days <= 7:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "週最大出勤日数",
                    "週最大出勤日数は 0（上限なし）〜 7 で入力してください"
                    f"（現在 {_text(col.get('週最大出勤日数'))!r}）。",
                )
            )
        consecutive = _number(col.get("最大連続勤務日数"))
        if consecutive is not None and consecutive < 1:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "最大連続勤務日数",
                    "最大連続勤務日数は 1 以上で入力してください"
                    f"（現在 {_text(col.get('最大連続勤務日数'))!r}）。",
                )
            )

        earliest = parse_time(col.get("最早始業"))
        latest = parse_time(col.get("最遅終業"))
        if earliest is None:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "最早始業",
                    "最早始業が空です。HH:MM で入力してください。",
                )
            )
        if latest is None:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "最遅終業",
                    "最遅終業が空です。HH:MM で入力してください。",
                )
            )
        e_min, l_min = _minutes(earliest), _minutes(latest)
        if e_min is not None and l_min is not None and e_min >= l_min:
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "最遅終業",
                    f"最早始業 {earliest.strftime('%H:%M')} が最遅終業 "
                    f"{latest.strftime('%H:%M')} 以上です。この職員は一度も配置できません"
                    "（読み込み時に 23:59 へ置き換えられます）。",
                )
            )
        if (
            day_open is not None
            and open_min is not None
            and l_min is not None
            and l_min <= open_min
        ):
            out.append(
                ValidationIssue(
                    WARNING, "staff", pos, "最遅終業",
                    f"契約時間帯（〜{latest.strftime('%H:%M')}）が園の開所 "
                    f"{day_open.strftime('%H:%M')} と 1 分も重なりません。"
                    "配置可能人時が 0 になります。",
                )
            )

        if not _text(col.get("資格（主）")):
            out.append(
                ValidationIssue(
                    ERROR, "staff", pos, "資格（主）",
                    "資格（主）が空です。保育士・子育て支援員などを 1 つ以上指定してください。",
                )
            )
        elif day_close is not None:
            earliest_txt = _text(col.get("最早始業"))
            latest_txt = _text(col.get("最遅終業"))
            if e_min is not None and l_min is not None and (
                e_min >= _minutes(day_close) or l_min <= open_min
            ):
                out.append(
                    ValidationIssue(
                        WARNING, "staff", pos, "最早始業",
                        f"契約時間帯 {earliest_txt or '-'}〜{latest_txt or '-'} が園の営業時間 "
                        f"{day_open.strftime('%H:%M')}〜{day_close.strftime('%H:%M')} と"
                        "ほぼ重なりません。配置可能人時が 0 に近い値です。",
                    )
                )
    return out


# ---------------------------------------------------------------------------
# 希望休
# ---------------------------------------------------------------------------


def analyze_preferences(
    frame: pd.DataFrame | None,
    *,
    staff_ids: Sequence[str] | Mapping[str, Any] = (),
    days: Sequence[date] = (),
) -> list[ValidationIssue]:
    """希望休表を検証する。

    職員IDが職員表に無いと ``load_bundle`` がその行を warning 付きで捨てるため、
    「入力したのに反映されない」という最も紛らわしい不具合になる。
    """
    if frame is None or frame.empty:
        return []
    known = _columns_of(frame, PREFERENCE_COLUMNS)
    out: list[ValidationIssue] = []
    known_ids = set(staff_ids.keys()) if isinstance(staff_ids, Mapping) else set(staff_ids)
    period = set(days)

    for pos, (_, row) in enumerate(frame.iterrows()):
        col = {name: row.get(name) for name in known}
        staff_id = _text(col.get("職員ID"))
        if not staff_id:
            out.append(ValidationIssue(ERROR, "preferences", pos, "職員ID", "職員IDが空です。"))
        elif known_ids and staff_id not in known_ids:
            listed = "、".join(sorted(known_ids)[:8])
            out.append(
                ValidationIssue(
                    ERROR, "preferences", pos, "職員ID",
                    f"職員ID {staff_id} は職員表にありません（この行は読み込み時に捨てられます）。"
                    f"職員表のID例: {listed}",
                )
            )

        target = parse_date(col.get("日付"))
        if target is None:
            out.append(
                ValidationIssue(
                    ERROR, "preferences", pos, "日付",
                    "日付が空です。YYYY-MM-DD で入力してください。",
                )
            )
        elif period and target not in period:
            out.append(
                ValidationIssue(
                    WARNING, "preferences", pos, "日付",
                    f"日付 {target.isoformat()} は計画期間の外です。期間設定を確認してください。",
                )
            )

        raw_kind = _text(col.get("種別"))
        if raw_kind and raw_kind not in PREFERENCE_TYPES:
            out.append(
                ValidationIssue(
                    WARNING, "preferences", pos, "種別",
                    f"種別「{raw_kind}」は解釈できません。"
                    " 希望休として扱われます（出勤不可・出勤希望・休み希望 も指定できます）。",
                )
            )
        elif not raw_kind:
            out.append(
                ValidationIssue(
                    WARNING, "preferences", pos, "種別",
                    "種別が空です。希望休として扱われます。",
                )
            )
        else:
            normalized = PREFERENCE_TYPES[raw_kind]
            if normalized not in _KNOWN_PREF_KINDS:
                out.append(
                    ValidationIssue(
                        WARNING, "preferences", pos, "種別",
                        f"種別「{raw_kind}」は未対応のため無視されます。",
                    )
                )

        # 開始・終了が空なら data_loader は 00:00〜23:59（全日不在）として扱う。
        # ここでも同じ解釈を採り、空は「全日」の意思表示として通す。
        raw_start, raw_end = _text(col.get("開始")), _text(col.get("終了"))
        start = parse_time(col.get("開始"))
        end = parse_time(col.get("終了"))
        if raw_start and start is None:
            out.append(
                ValidationIssue(
                    ERROR, "preferences", pos, "開始",
                    f"開始時刻「{raw_start}」が解釈できません。HH:MM で入力するか空にしてください。",
                )
            )
        if raw_end and end is None:
            out.append(
                ValidationIssue(
                    ERROR, "preferences", pos, "終了",
                    f"終了時刻「{raw_end}」が解釈できません。HH:MM で入力するか空にしてください。",
                )
            )
        s_min, e_min = _minutes(start), _minutes(end)
        if raw_start and raw_end and s_min is not None and e_min is not None and e_min <= s_min:
            out.append(
                ValidationIssue(
                    ERROR, "preferences", pos, "終了",
                    f"終了 {_hhmm(e_min)} が開始 {_hhmm(s_min)} と同じか前です。"
                    "読み込み時に 23:59 へ書き換わり、当日一日が丸ごと不在になります。",
                )
            )
    return out


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def validate_frames(
    children: pd.DataFrame | None = None,
    staff: pd.DataFrame | None = None,
    preferences: pd.DataFrame | None = None,
    *,
    days: Sequence[date] = (),
    day_open: time | None = None,
    day_close: time | None = None,
) -> ValidationReport:
    """3 表をまとめて検証する。

    :returns: :class:`ValidationReport`（``has_errors`` が True の間は読み込みを止める）
    """
    staff_ids: list[str] = []
    if staff is not None and "職員ID" in getattr(staff, "columns", []):
        staff_ids = [_text(v) for v in staff["職員ID"].tolist()]
    issues: list[ValidationIssue] = []
    issues.extend(
        analyze_children(children, days=days, day_open=day_open, day_close=day_close)
    )
    issues.extend(analyze_staff(staff, day_open=day_open, day_close=day_close))
    issues.extend(analyze_preferences(preferences, staff_ids=staff_ids, days=days))
    return ValidationReport(tuple(issues))