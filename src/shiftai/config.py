"""設定値・法定要件の定数。

すべてのモジュールが参照する定数をここに集約する。
"""

from __future__ import annotations

from datetime import date, time

APP_TITLE = "配置基準連動型 シフト自動作成AI"
APP_ICON = "🧸"
APP_VERSION = "0.1.0"

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

# 労働基準法（法定要件）
STATUTORY_DAILY_WORK_HOURS = 8
STATUTORY_WEEKLY_WORK_HOURS = 44
STATUTORY_OVERTIME_LIMIT_HOURS = 45
STATUTORY_MIN_REST_HOURS = 11

# 園の開設時間の既定
DEFAULT_DAY_OPEN = time(7, 15)
DEFAULT_DAY_CLOSE = time(19, 30)
DEFAULT_GRANULARITY_MIN = 30
DEFAULT_SATURDAY_OPEN = time(7, 30)
DEFAULT_SATURDAY_CLOSE = time(18, 30)

# 表示用の既定配色
COLOR_WORK = "#2E7D32"
COLOR_BREAK = "#F9A825"
COLOR_OFF = "#EEEEEE"
COLOR_SHORTFALL = "#E53935"
COLOR_OVER = "#1E88E5"
