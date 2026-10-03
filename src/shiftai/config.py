"""設定値・法定要件の定数。

すべてのモジュールが参照する定数をここに集約する。
"""

from __future__ import annotations

from datetime import date, time

from shiftai import __version__

APP_TITLE = "配置基準連動型 シフト自動作成AI"
APP_ICON = "🧸"
# バージョンの唯一の真実は ``pyproject.toml``。値は ``shiftai.__version__``
# （ディストリビューションのメタデータ）から取る。ここにハードコードしない。
APP_VERSION = __version__

# 既定表示期間
DEFAULT_RANGE_START = date.today()
DEFAULT_RANGE_DAYS = 7

# 休憩の既定
DEFAULT_BREAK_MINUTES = 60
DEFAULT_BREAK_STAGGER_MINUTES = 30
# 労働基準法上、6時間を超える勤務には45分の休憩
STATUTORY_BREAK_MINUTES = 45
# 6時間を超える勤務 / 8時間を超える勤務の閾値（分）
STATUTORY_BREAK_THRESHOLDS = ((8 * 60, 60), (6 * 60, 45))

#: 園基準の休憩を強制する長時間勤務の閾値（分）。
#: :data:`STATUTORY_BREAK_THRESHOLDS` の下位側（6時間超）と同じ値。
STATUTORY_LONG_SHIFT_MINUTES = 6 * 60

# 労働基準法（法定要件）
STATUTORY_DAILY_WORK_HOURS = 8

#: 労働基準法第32条・第34条の1日法定労働時間の上限（10時間）。
#: 時間外労働が認められる場合の ``STATUTORY_OVERTIME_MULTIPLIER`` を使っても
#: この上限は超えられない。``solver`` / ``gap_analysis`` / ``live_validation``
#: がそれぞれ 600（分）や 10.0（時間）で持っていたのをここに集約した。
STATUTORY_MAX_DAILY_WORK_HOURS = 10.0

#: 時間外労働が認められる契約で、1日の労働時間の上限に掛ける倍率（8時間×1.25）。
STATUTORY_OVERTIME_MULTIPLIER = 1.25

#: 休息時間を原則確保したうえで実質的に置ける1日の労働時間。
#: 法定8時間 + 休憩45分 = 8.75時間。``solver._LONG_DAILY_HOURS``（9時間）とは
#: 別の概念で、**両者は意図的に値が違う**。統一していない。
STATUTORY_DAILY_LIMIT_HOURS = STATUTORY_DAILY_WORK_HOURS + 0.75

#: 最適化側で「長すぎる勤務」を罰する内部の目安（法定8時間 + 1時間）。
#: :data:`STATUTORY_DAILY_LIMIT_HOURS`（8.75）とは別の閾値であり、
#: ``gap_analysis`` の適合判定と値を揃える必要はない。
INTERNAL_DAILY_LONG_HOURS = STATUTORY_DAILY_WORK_HOURS + 1.0

STATUTORY_WEEKLY_WORK_HOURS = 44
"""週あたりの上限として用いる内部の目安値。

労働基準法第32条の4 は 2019-04-01 の改正で「月45時間・年360時間」に変更され、
旧来の「週44時間」は法定の上限ではなくなった。本定数は**厳しい方（古い方）**を
使うため法令違反を生じないが、UI で「法定の週労働時間」と断定して表示しては
ならない。法定の枠組みは「月45時間・年360時間」である。
"""
STATUTORY_OVERTIME_LIMIT_HOURS = 45
STATUTORY_MIN_REST_HOURS = 11

#: 同時に休憩してよい在勤者の割合の上限目安（25%）。
#: ``solver`` と ``gap_analysis`` が同名のモジュール定数をそれぞれ持っていた。
BREAK_CONCURRENT_SHARE = 0.25

#: 労働時間・分・制約式を浮動小数点で比較するときの共通許容誤差。
#: ``gap_analysis`` には 6 箇所、``solver`` には 1 箇所に直書きされていた。
FLOAT_TOLERANCE = 1e-6

#: 土曜・休日を判定する ``date.weekday()`` の境界値（日曜=0）。
#: ``solver`` / ``fairness`` / ``live_validation`` の 4 箇所が同じ値を直書き。
WEEKEND_START_WEEKDAY = 5

#: 週あたりの判定に使う窓の日数。
WEEKLY_WINDOW_DAYS = 7

# 園の開設時間の既定
DEFAULT_DAY_OPEN = time(7, 15)
DEFAULT_DAY_CLOSE = time(19, 30)
DEFAULT_GRANULARITY_MIN = 30
DEFAULT_SATURDAY_OPEN = time(7, 30)
DEFAULT_SATURDAY_CLOSE = time(18, 30)

#: 保育標準時間の開始・終了（1歳児の保育標準的な時間帯）。
STANDARD_TIME_START = time(8, 30)
STANDARD_TIME_END = time(17, 15)

#: 早朝保育・延長保育の時間帯。園設定や制度ごとに変わりうるため、
#: ``FacilitySettings`` から上書きできる既定値として置く。
#: ``DEFAULT_DAY_OPEN`` / ``DEFAULT_DAY_CLOSE`` と同値だが、
#: 「園の開所閉所」とは別の意味なので統合しない。
DEFAULT_EARLY_CARE_START = time(7, 15)
DEFAULT_EARLY_CARE_END = time(8, 30)
DEFAULT_LATE_CARE_START = time(17, 15)
DEFAULT_LATE_CARE_END = time(19, 30)

#: 人件費目安（1時間・パート係数）。``FacilitySettings.labor_cost_per_hour``
#: の既定値であり、``exporter`` のフォールバックも同じ値を参照する。
DEFAULT_LABOR_COST_PER_HOUR = 1500.0

#: 園の既定名称。CLI の ``--facility`` と ``FacilitySettings`` の双方で使う。
DEFAULT_FACILITY_NAME = "あさひ保育園"

#: 「終日」を表す番兵時刻。出勤不可・不在時間帯の終端に使う。
DAY_END = time(23, 59)

#: サンプルデータ生成の既定乱数シード。CLI と UI、ライブラリが同じ値を使う。
DEFAULT_SAMPLE_SEED = 42

#: MILP ソルバの既定実行時間上限（秒）。CLI と UI が同じ値を使う。
DEFAULT_TIME_LIMIT_SEC = 60

#: Streamlit の時刻入力ウィジェットに与える step（秒）。15分刻み。
#: UI 固有の設定であり法定要件ではないため ``APP_`` 規約で持つ。
APP_TIME_INPUT_STEP_SECONDS = 900

# 表示用の既定配色
COLOR_WORK = "#2E7D32"
COLOR_BREAK = "#F9A825"
COLOR_OFF = "#EEEEEE"
COLOR_SHORTFALL = "#E53935"
COLOR_OVER = "#1E88E5"

#: 表示用の既定配色（文字色・枠色）。
#: 上記の背景色と対で使い、同じ役割の箇所に散らばっていた値を集めたもの。
COLOR_WORK_INK = "#1b5e20"
COLOR_BREAK_INK = "#7a5200"
COLOR_SHORTFALL_INK = "#8e0000"
COLOR_OVER_INK = "#0d47a1"
#: 手動で確定したセルの枠色。
COLOR_FIXED = "#6a1b9a"
#: アクセント色（ツールチップ・進捗チップの強調）。
COLOR_ACCENT = "#1f6feb"
#: 本文の標準文字色。
COLOR_INK = "#1f2430"
