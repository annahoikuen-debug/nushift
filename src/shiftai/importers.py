"""園業務支援システム（CoDMON / キッズリー等）の CSV を本アプリ形式へ変換する。

**このモジュールが解決する問題**

園児の登降園予定は、実運用では園業務支援システム（CoDMON、キッズリー 他）の
CSV 出力から入れる。:mod:`shiftai.data_loader` の ``load_children`` は
本アプリ独自の列名を前提にしているため、そのままだと **列名の付け替えだけで**
取り込める。

プロファイルは「正規列名 -> そのプロファイルでの列名候補」の写像 1 枚で表す。
施設ごとの差異は候補リストに足すだけで済むようにしておく。

責務の境界:

* 変換結果の **型** は :mod:`shiftai.data_loader` に委譲する（`parse_date` / `parse_time`）。
* 本モジュールは「どの列を使うか」と「施設ごとの既定値」だけを担当する。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd

from shiftai.data_loader import CHILDREN_COLUMNS, parse_bool, parse_date, parse_time

__all__ = [
    "ConvertResult",
    "ImportProfile",
    "convert",
    "detect_profile",
    "get_profile",
    "list_profiles",
    "mapping_frame",
    "profile_options",
]

_ERA_BASE_YEAR = 2018
"""和暦の起点年（R6 = 2026）。``parse_date`` は和暦を受け付けないためここで処理する。"""

_NON_WORD = re.compile(r"[\s　_\-()\[\]{}<>\"'、。,./\\:：*#!$%&+=|~@`^]+")
_ERA = re.compile(r"^(?:r|R|令和)(\d+)\.(\d{1,2})\.(\d{1,2})$")


@dataclass(frozen=True)
class ImportProfile:
    """园業務支援システム 1 種分の列マッピング。

    :param column_map: 正規列名 -> そのプロファイルでの列名候補（先頭から順に照合する）
    :param defaults: 入力に無かったときに使う既定値（正規列名をキーに）
    :param infer_short_time: 「短時間保育」を在園時間帯から推定するかどうか
    """

    key: str
    label: str
    column_map: Mapping[str, tuple[str, ...]]
    defaults: Mapping[str, Any] = field(default_factory=dict)
    infer_short_time: bool = False
    notes: str = ""

    def find_column(self, columns: Sequence[str], canonical: str) -> str | None:
        """正規列名に対応する、入力に実在する列名を返す。"""
        lookup = {normalize_header(c): c for c in columns}
        for candidate in self.column_map.get(canonical, ()):
            found = lookup.get(normalize_header(candidate))
            if found is not None:
                return found
        return None


@dataclass(frozen=True)
class ConvertResult:
    """変換結果。変換後の DataFrame と、採用した列の対応を保持する。"""

    frame: pd.DataFrame
    """``data_loader.CHILDREN_COLUMNS`` に揃えた DataFrame。"""
    mapping: dict[str, str] = field(default_factory=dict)
    """正規列名 -> 入力に実在した列名。"""
    missing: tuple[str, ...] = ()
    """入力になく既定値で埋めた正規列。"""
    notes: tuple[str, ...] = ()
    """運用上の注意。"""


def normalize_header(name: Any) -> str:
    """ヘッダ文字列を照合用に正規化する（:func:`data_loader.normalize_header` と同じ規則）。"""
    text = unicodedata.normalize("NFKC", str(name)).strip()
    return _NON_WORD.sub("", text).lower()


_CODEMON = ImportProfile(
    key="codemon",
    label="CoDMON（園業務支援システム）",
    column_map={
        "園児ID": ("園児コード", "園児CD", "園児No", "児童コード"),
        "氏名": ("園児氏名", "児童氏名", "氏名", "名前"),
        "年齢": ("年齢", "歳", "年齢（歳）"),
        "登園日": ("利用日", "登園日", "日付", "出勤日"),
        "登園時刻": ("登園時刻", "登園予定時刻", "登園時間", "出勤時刻"),
        "降園時刻": ("降園時刻", "退園時刻", "降園予定時刻", "退園時間"),
        "短時間保育": ("短時間保育", "短保育", "保育標準時間のみ", "短時間"),
        "欠席": ("欠席", "欠勤", "不出"),
        "欠席理由": ("欠席理由", "不在理由", "理由"),
        "早朝保育": ("早朝保育", "早朝", "早朝保育利用"),
        "延長保育": ("延長保育", "延長", "延長保育利用"),
        "備考": ("備考", "メモ", "特記事項"),
    },
    infer_short_time=True,
    notes=(
        "「園児コード」を本アプリの「園児ID」としてそのまま使います。",
        "短時間保育のフラグが無い場合は、保育標準時間帯のみで利用している行を推定します"
        "（行ごとに実際の在園時間帯が保育標準時間帯と一致する場合のみ）。",
    ),
)

_KIDS_RYU = ImportProfile(
    key="kids_ryu",
    label="キッズリー",
    column_map={
        "園児ID": ("園児ID", "園児No", "児童ID"),
        "氏名": ("氏名", "お名前", "園児名"),
        "年齢": ("年齢", "歳"),
        "登園日": ("登園日", "利用日", "日付"),
        "登園時刻": ("登園予定時刻", "登園時刻", "登園時間"),
        "降園時刻": ("降園予定時刻", "降園時刻", "退園時刻"),
        "短時間保育": ("短時間保育", "短保育"),
        "欠席": ("欠席", "欠勤"),
        "欠席理由": ("欠席理由", "理由"),
        "早朝保育": ("早朝保育利用", "早朝保育"),
        "延長保育": ("延長保育利用", "延長保育"),
        "備考": ("備考", "メモ"),
    },
    infer_short_time=True,
    notes=("キッズリーの CSV は 1 行が「園児×日」です。1 日複数行は自動で結合しません。",),
)

_GENERIC = ImportProfile(
    key="generic",
    label="その他（列名が一般的な園）",
    column_map={
        "園児ID": ("園児ID", "園児コード", "園児No", "児童ID", "ID"),
        "氏名": ("氏名", "お名前", "園児名"),
        "年齢": ("年齢", "歳"),
        "登園日": ("登園日", "利用日", "日付"),
        "登園時刻": ("登園時刻", "登園予定時刻", "到着時刻"),
        "降園時刻": ("降園時刻", "降園予定時刻", "退園時刻"),
        "短時間保育": ("短時間保育", "短保育"),
        "欠席": ("欠席", "不在", "欠勤"),
        "欠席理由": ("欠席理由", "理由"),
        "早朝保育": ("早朝保育", "早朝"),
        "延長保育": ("延長保育", "延長"),
        "備考": ("備考", "メモ"),
    },
    notes=(
        "対応する列が見つからないものは空欄になります。取り込み後に下表で目視確認してください。",
    ),
)

_PROFILES: tuple[ImportProfile, ...] = (_CODEMON, _KIDS_RYU, _GENERIC)


def list_profiles() -> list[ImportProfile]:
    """利用可能な取込プロファイルの一覧を返す。"""
    return list(_PROFILES)


def profile_options() -> list[tuple[str, str]]:
    """``st.selectbox`` にそのまま渡せる ``(key, label)`` の列を返す。"""
    return [(p.key, p.label) for p in _PROFILES]


def get_profile(key: str) -> ImportProfile:
    """プロファイルを取得する（不明キーは ``generic`` にフォールバック）。"""
    for profile in _PROFILES:
        if profile.key == key:
            return profile
    return _GENERIC


def detect_profile(frame: pd.DataFrame) -> str:
    """列名からプロファイルを推測する（判定できなければ ``generic``）。"""
    if frame is None or frame.empty:
        return "generic"
    columns = {normalize_header(c) for c in frame.columns}
    best_key, best_hits = "generic", 0
    for profile in _PROFILES:
        if profile.key == "generic":
            continue
        hits = sum(
            1
            for candidates in profile.column_map.values()
            for candidate in candidates
            if normalize_header(candidate) in columns
        )
        if hits > best_hits:
            best_key, best_hits = profile.key, hits
    return best_key


def _era_date(value: Any) -> date | None:
    """``R6.9.1`` 形式の和暦日付を西暦の日付に変換する（それ以外は ``None``）。"""
    text = str(value).strip()
    matched = _ERA.match(text)
    if matched is None:
        return None
    year, month, day = (int(g) for g in matched.groups())
    try:
        return date(_ERA_BASE_YEAR + year, month, day)
    except ValueError:
        return None


#: 月齢の表記に付く単位。「○ヶ月」「○か月」「○カ月」「○ケ月」「○ヵ月」。
_MONTH_UNIT = re.compile(r"[ヶヵかケカ][ \t]*月")

#: 年齢の表記に付く単位。「○歳」「○才」。現場では「才」も使われる。
_YEAR_UNIT = re.compile(r"[ \t]*[歳才]")

#: 全文から数字だけを取り出す前の、符号付きの数値（``-1`` を 1 に誤読しないため）。
_SIGNED_INT = re.compile(r"^[ \t]*([+-]?[0-9]+)[ \t]*$")


def _age_years(value: Any) -> str:
    """年齢の値を ``0``〜``5`` の文字列に落とす。解釈できなければ ``"0"``。

    園業務支援システムの年齢欄は ``3歳6ヶ月`` のように
    **「年 + 月」の両方を書く**ことがある。ここで数字を全部つなげて
    ``36`` にしてから月判定すると、全園児が 0 歳児になってしまう
    （0 歳児は必要人員が最も大きい区分なので、必要人員が過剰になる）。

    したがって **年の部分を先に確定し**、月表記は
    「年が無い月齢（``11ヶ月`` など）」のときの 0 歳根拠としてのみ使う。
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "0"
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if not text:
        return "0"

    # 年が明示されていれば、その年を採用する（月は無視）
    if _YEAR_UNIT.search(text):
        year_part = _YEAR_UNIT.split(text, maxsplit=1)[0]
        signed_year = _SIGNED_INT.match(year_part)
        if signed_year:
            return str(max(0, min(5, int(signed_year.group(1)))))
        year_digits = re.sub(r"[^0-9]", "", year_part)
        return str(max(0, min(5, int(year_digits)))) if year_digits else "0"

    # 符号付き数値だけならそのまま解釈する（-1 を 1 にしない）
    signed = _SIGNED_INT.match(text)
    if signed:
        return str(max(0, min(5, int(signed.group(1)))))

    # 年が無く月単位の表記（11ヶ月 / 11カ月 …）なら、まだ 0 歳
    if _MONTH_UNIT.search(text):
        return "0"

    # 数値（float / int / Decimal）として渡された場合
    #
    # ``pandas`` は年齢列に空欄が 1 つでもあれば **float64** として推論する
    # （``pd.read_excel`` / ``pd.read_csv`` の既定）。
    # その結果 ``3.0`` のような値が ``_age_years`` に届くと、
    # 文字列化すると ``"3.0"`` になり、**数字だけを取り出す処理が
    # ``"30"`` となって 5 歳（最も低い定員比）に丸められてしまう**。
    # つまり 3 歳の園児が 5 歳扱いになり、必要人員が **過少に算出**される。
    # したがって数値は小数点以下を切り捨てて整数として扱う。
    if isinstance(value, bool):
        return "0"
    if isinstance(value, int):
        return str(max(0, min(5, value)))
    if isinstance(value, (float, Decimal)):
        if value != value:  # NaN
            return "0"
        return str(max(0, min(5, int(value))))

    # 残りは数字だけの表記
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return "0"
    return str(max(0, min(5, int(digits))))


#: 園業務支援システムで「欠席・利用あり」を表す語（``data_loader.parse_bool`` に無いもの）。
_TRUE_TOKENS: frozenset[str] = frozenset({"あり", "あ", "有る", "1", "○", "◯"})
_FALSE_TOKENS: frozenset[str] = frozenset({"なし", "無", "0"})


def _flag(value: Any, default: bool = False) -> bool:
    """真偽値を読む。園業務支援システム固有の表記も吸収する。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return parse_bool(value, default)


def _is_short_time(arrive: Any, depart: Any, standard_window: tuple[int, int] | None) -> bool:
    """在園時間帯が保育標準時間帯と完全に一致するなら短時間保育とみなす。"""
    if standard_window is None or arrive is None or depart is None:
        return False
    window_start, window_end = standard_window
    return (
        arrive.hour * 60 + arrive.minute == window_start
        and depart.hour * 60 + depart.minute == window_end
    )


def convert(
    frame: pd.DataFrame,
    profile_key: str = "generic",
    *,
    standard_time: tuple[Any, Any] | None = None,
) -> ConvertResult:
    """園業務支援システムの DataFrame を本アプリ形式に変換する。

    :param frame: 取込元の DataFrame
    :param profile_key: :func:`get_profile` が解釈するプロファイルキー
    :param standard_time: 保育標準時間帯（``(開始, 終了)``）。
        指定すると短時間保育の推定に使う。
    :returns: 変換結果（DataFrame と採用列の対応）
    """
    profile = get_profile(profile_key)
    if frame is None or frame.empty:
        return ConvertResult(frame=pd.DataFrame(columns=CHILDREN_COLUMNS))

    mapping: dict[str, str] = {}
    missing: list[str] = []
    for canonical in CHILDREN_COLUMNS:
        found = profile.find_column(list(frame.columns), canonical)
        if found is None:
            missing.append(canonical)
        else:
            mapping[canonical] = found

    window = None
    if standard_time is not None:
        start = parse_time(standard_time[0])
        end = parse_time(standard_time[1])
        if start is not None and end is not None:
            window = (start.hour * 60 + start.minute, end.hour * 60 + end.minute)

    def column(canonical: str) -> pd.Series | None:
        found = mapping.get(canonical)
        return None if found is None else frame[found]

    ids = column("園児ID")
    names = column("氏名")
    ages = column("年齢")
    days = column("登園日")
    arrives = column("登園時刻")
    departs = column("降園時刻")
    shorts = column("短時間保育")
    absent = column("欠席")
    reasons = column("欠席理由")
    earlys = column("早朝保育")
    lates = column("延長保育")
    memos = column("備考")

    records: list[dict[str, Any]] = []
    for i in range(len(frame)):
        raw_day = _value(days, i)
        parsed_day = _era_date(raw_day) or parse_date(raw_day)
        raw_arrive = _value(arrives, i)
        raw_depart = _value(departs, i)
        arrive = parse_time(raw_arrive)
        depart = parse_time(raw_depart)
        is_absent = _flag(_value(absent, i), False)
        # 短時間保育の列が「無い」ときだけ在園時間帯から推定する。
        # 「あり/なし」が明示されている場合はその値を優先する（現場の指定が正）。
        short_text = "" if shorts is None else _value(shorts, i)
        short_flag = _flag(short_text, False)
        if profile.infer_short_time and short_text == "" and not is_absent:
            short_flag = _is_short_time(arrive, depart, window)
        record: dict[str, Any] = {
            "園児ID": _text(_value(ids, i)),
            "氏名": _text(_value(names, i)),
            "年齢": _age_years(_value(ages, i)),
            "登園日": parsed_day,
            "登園時刻": arrive,
            "降園時刻": depart,
            "短時間保育": "true" if short_flag else "false",
            "欠席": "true" if is_absent else "false",
            "欠席理由": _text(_value(reasons, i)),
            "早朝保育": "true" if _flag(_value(earlys, i), False) else "false",
            "延長保育": "true" if _flag(_value(lates, i), False) else "false",
            "備考": _text(_value(memos, i)),
        }
        for canonical, value in profile.defaults.items():
            record.setdefault(canonical, value)
        records.append(record)
    return ConvertResult(
        frame=pd.DataFrame.from_records(records, columns=CHILDREN_COLUMNS),
        mapping=mapping,
        missing=tuple(missing),
        notes=tuple(profile.notes),
    )


def _value(column: pd.Series | None, position: int) -> Any:
    if column is None:
        return None
    if position >= len(column):
        return None
    value = column.iloc[position]
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def mapping_frame(result: ConvertResult) -> pd.DataFrame:
    """採用した列の対応を表にする（取り込み後に目視確認するためのもの）。"""
    rows = [
        {"本アプリの列": canonical, "取り込んだ列": result.mapping.get(canonical, "")}
        for canonical in CHILDREN_COLUMNS
    ]
    return pd.DataFrame.from_records(rows, columns=["本アプリの列", "取り込んだ列"])
