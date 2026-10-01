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

# 労働基準法（法定要件）
STATUTORY_DAILY_WORK_HOURS = 8

STATUTORY_WEEKLY_WORK_HOURS = 44
"""週あたりの上限として用いる内部の目安値。

労働基準法第32条の4 は 2019-04-01 の改正で「月45時間・年360時間」に変更され、
旧来の「週44時間」は法定の上限ではなくなった。本定数は**厳しい方（古い方）**を
使うため法令違反を生じないが、UI で「法定の週労働時間」と断定して表示しては
ならない。法定の枠組みは「月45時間・年360時間」である。
"""
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
