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

#: 園ルールとして使う1日の上限時間（10.0 時間）。
#: 労働基準法に 1日10時間という条文は無い。**第32条の5** が存在するが、保育施設がその要件（協定・届出・業種/規模）を満たすかは本ツールでは確認できないため、法定根拠として主張しない。
#: 第32条の5 は著しい繁閑差・常時使用労働者数要件・労使協定・前日通知を条件とする1日10時間上限。
#: 変形労働時間制（第32条の3）の上限は1日9時間・週40時間以下であり、10時間の根拠にはならない。
#: ``solver`` / ``gap_analysis`` / ``live_validation`` がそれぞれ 600（分）や 10.0（時間）で持っていたのをここに集約した。
STATUTORY_MAX_DAILY_WORK_HOURS = 10.0

#: 時間外労働が認められる契約で、1日の労働時間の上限に掛ける倍率（8時間×1.25）。
STATUTORY_OVERTIME_MULTIPLIER = 1.25

#: 旧「法定日勤上限」。判定には使わない（誤っているため FD-01 で廃止する）。
#: 8時間 + 休憩45分 = 8.75時間 という計算は、労働時間を休憩を含まない実働時間と比較する gap_analysis 側では成立しない（第34条の休憩義務は第32条の労働時間と独立した要件であり、8時間に足し込むものではない）。
#: 残置するのは後方参照のためだけで、DeprecationWarning 相当として扱う。
STATUTORY_DAILY_LIMIT_HOURS = STATUTORY_DAILY_WORK_HOURS + 0.75

#: 最適化側で「長すぎる勤務」を罰する内部の目安（法定8時間 + 1時間）。
#: :data:`STATUTORY_DAILY_LIMIT_HOURS`（8.75）とは別の閾値であり、
#: ``gap_analysis`` の適合判定と値を揃える必要はない。
INTERNAL_DAILY_LONG_HOURS = STATUTORY_DAILY_WORK_HOURS + 1.0

STATUTORY_WEEKLY_WORK_HOURS = 44
"""週あたりの上限として用いる**社内目安値**（44 時間）。
旧来の「週44時間」は法定の上限ではなくなったが、2024-04-01 改正後は従業者数規模別に上限が異なる（中規模事業所は2027年4月から月30時間、小規模事業所は2029年4月から月40時間など）。
本定数は厳しい方（古い方）を使うため法令違反を生じないが、UI で「法定の週労働時間」と断定して表示してはならない。
"""
STATUTORY_OVERTIME_LIMIT_HOURS = 45
"""時間外労働（特別条項の上限）の**社内目安値**（月45時間）。
2024-04-01 改正後は法定の上限が律令で定められている（2026年4月から月40時間、
2029年4月から月20時間）。本定数は最も厳しい（古い）側の 45 時間を使うため
法令違反を生じないが、UI で「法定の上限」と断定して表示してはならない。
:data:`STATUTORY_WEEKLY_WORK_HOURS` と同じ扱い。
"""
STATUTORY_MIN_REST_HOURS = 11

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
