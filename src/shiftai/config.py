"""設定値・法定要件の定数。

すべてのモジュールが参照する定数をここに集約する。
"""

from __future__ import annotations

from datetime import date, time

from shiftai import __version__
from shiftai.statutory_rules import (  # noqa: F401
    DEFAULT_BREAK_MINUTES,  # noqa: F401
    INTERNAL_DAILY_LONG_HOURS,  # noqa: F401
    STATUTORY_BREAK_MINUTES,  # noqa: F401
    STATUTORY_BREAK_THRESHOLDS,  # noqa: F401
    STATUTORY_DAILY_WORK_HOURS,  # noqa: F401
    STATUTORY_LONG_SHIFT_MINUTES,  # noqa: F401
    STATUTORY_MAX_DAILY_WORK_HOURS,  # noqa: F401
    STATUTORY_MIN_REST_HOURS,  # noqa: F401
    STATUTORY_OVERTIME_MULTIPLIER,  # noqa: F401
    STATUTORY_WEEKLY_WORK_HOURS,  # noqa: F401
)

APP_TITLE = "配置基準連動型 シフト自動作成AI"
APP_ICON = "🧸"
# バージョンの唯一の真実は ``pyproject.toml``。値は ``shiftai.__version__``
# （ディストリビューションのメタデータ）から取る。ここにハードコードしない。
APP_VERSION = __version__

# 既定表示期間
DEFAULT_RANGE_START = date.today()
DEFAULT_RANGE_DAYS = 7

# 休憩の既定（statutory_rules から再エクスポート）
DEFAULT_BREAK_STAGGER_MINUTES = 30

# ---------------------------------------------------------------------------
# 区間列挙・区間制約
# ---------------------------------------------------------------------------

MAX_INTERVAL_CANDIDATES = 100
"""在勤/休憩区間列挙の候補上限（この値を超えた場合はソフトペナルティへフォールバック）。

CBC の既知挙動: 300 で 7日28人は1パス目解なし、150 なら概ね動く。
"""

STATUTORY_DAILY_LIMIT_HOURS = STATUTORY_DAILY_WORK_HOURS + 0.75

STATUTORY_OVERTIME_LIMIT_HOURS = 45
"""時間外労働（特別条項の上限）の**社内目安値**（月45時間）。
2024-04-01 改正後は法定の上限が律令で定められている（2026年4月から月40時間、
2029年4月から月20時間）。本定数は最も厳しい（古い）側の 45 時間を使うため
法令違反を生じないが、UI で「法定の上限」と断定して表示してはならない。
:data:`STATUTORY_WEEKLY_WORK_HOURS` と同じ扱い。
"""

#: 同時に休憩してよい在勤者の割合の上限目安（25%）。
#: ``solver`` と ``gap_analysis`` が同名のモジュール定数をそれぞれ持っていた。
BREAK_CONCURRENT_SHARE = 0.25

#: 労働時間・分・制約式を浮動小数点で比較するときの共通許容誤差。
#: ``gap_analysis`` には 6 箇所、``solver`` には 1 箇所に直書きされていた。
FLOAT_TOLERANCE = 1e-6

#: 1日の勤務（勤務＋休憩）が2つ以上の**連続ブロック**に分かれたとき、
#: ブロック間の下班として確保する最小の分数（分）。
#:
#: 園の運用では出退勤は1日1回である。「勤務 → 30分の穴 → 勤務」は
#: 帰宅と再出勤を伴うため時間としては成立しても運用できない。
#: 最短のántoんの穴であれば劳动合同上ありえるか、という判断は園ごとの運用規定で
#: あるため法定値ではなく**園ルール**として扱い、UI で設定できるようにする。
MIN_CONTIGUOUS_DUTY_MINUTES = 120

#: 「勤務1日1ブロック」「休憩1日1回」を**ハード制約**として課すかどうか。
#:
#: ``False``（既定）にすると旧来のソフトペナルティ方式（分割を許容する）に戻る。
#: ``True`` のときは下 2 項目が効く。
ENFORCE_SINGLE_DUTY_BLOCK = True

#: 勤務ブロック1日1個と休憩ブロック1日1個を**ソフトペナルティ**に戻してもよいか。
#: ``True`` のときは分割を許す（``relaxation=RELAX_HOURS`` で自動的に有効になる）。
ALLOW_DUTY_BLOCK_SPLIT = False

#: 週の上限時間を**拘束時間（勤務＋休憩）**で判定するかどうか。
#:
#: 分断された休憩は労働時間として扱われるため、拘束時間で見ないと
#: 週44時間の社内目安を実質的に回避できてしまう。ただし既定は ``False`` のままにして、
#: 園が運用として採用してから ON にできるようにする（ON にすると警告が急増する）。
CHECK_BOUND_HOURS_WEEKLY = False

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

#: MILP ソルバの既定エンジン（"auto" | "HiGHS" | "PULP_CBC_CMD"）。
DEFAULT_SOLVER = "PULP_CBC_CMD"

#: Streamlit の時刻入力ウィジェットに与える step（秒）。15分刻み。
#: UI 固有の設定であり法定要件ではないため ``APP_`` 規約で持つ。
APP_TIME_INPUT_STEP_SECONDS = 900

# 表示用の既定配色（Emerald / Amber / Rose / Indigo 統一パレット）
COLOR_WORK = "#059669"
COLOR_BREAK = "#D97706"
COLOR_OFF = "#F1F5F9"
COLOR_SHORTFALL = "#E11D48"
COLOR_OVER = "#4F46E5"

#: 表示用の既定配色（文字色・枠色）。
#: 上記の背景色と対で使い、同じ役割の箇所に散らばっていた値を集めたもの。
COLOR_WORK_INK = "#064E3B"
COLOR_BREAK_INK = "#78350F"
COLOR_SHORTFALL_INK = "#881337"
COLOR_OVER_INK = "#312E81"
#: 手動で確定したセルの枠色。
COLOR_FIXED = "#7C3AED"
#: アクセント色（ツールチップ・進捗チップの強調）。
COLOR_ACCENT = "#059669"
#: 本文の標準文字色。
COLOR_INK = "#1E293B"
