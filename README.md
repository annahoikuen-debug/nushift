# 🧸 shiftai — 配置基準連動型 シフト自動作成

園児の登降園予定と保育所の**職員配置基準**を入力すると、法令と基準を満たす勤務表を
MILP（混合整数線形計画）で自動作成する CLI / Web アプリです。

配置基準の「なぜこの人数なのか」が文字列として残るため、現場で確認・説明できます。

> この README の数値と出力例はすべて実際に実行した結果です。
> 記載したコマンドはそのまま実行できます（Windows / macOS / Linux 共通）。

---

## 目次

- [3 分でわかるデモ](#3-分でわかるデモ)
- [Python API の最小例](#python-api-の最小例)
- [画面（Streamlit）](#画面streamlit)
- [何ができるか](#何ができるか)
- [制約の扱い（ハード / ソフト）](#制約の扱いハード--ソフト)
- [CLI リファレンス](#cli-リファレンス)
- [入出力](#入出力)
- [開発](#開発)
- [既知の制約と注意点](#既知の制約と注意点)
- [詳細ドキュメント](#詳細ドキュメント)

---

## 3 分でわかるデモ

### 0) インストール

```bash
git clone <このリポジトリ>
cd nushift

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e .
```

開発用（テスト・lint）も含める場合:

```bash
pip install -e ".[dev]"
```

### 1) 画面を開く

```bash
python -m streamlit run streamlit_app.py
# 同じ動作: python -m shiftai ui
```

ブラウザに `http://localhost:8501` が開きます。最初に「ウィザードで順に入力する / まとめて入力する」のどちらが尋ねられます（サイドバーの「入力方法」でいつでも変更できます）。さらにサイドバーのトグルで
**シンプルモード（3 タブ）/ 上級者モード（5 タブ）**を切り替えられます。

### 2) コマンドだけで完結させる

配置基準の一覧:

```bash
$ python -m shiftai presets
[全国基準（厚労省）] 全国基準（厚労省）
    厚労省告示第49号の標準。4・5歳児20:1・延長は保育士のみ
    出典: 保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針
[東京都] 東京都
    東京都告示。4・5歳児12:1、延長18:30まで代替措置可
    …
[保育標準時間のみ園] 保育標準時間のみ園
    3時間保育園。8:30〜17:15のみ、早朝・延長の窓口なし
    出典: 保育所の職員配置基準（昭和52年厚生省告示第49号）・保育所保育指針

既定の基準は 全国基準（厚労省） です（--standard で変更できます）。
```

サンプルデータで 1 週間分を解いて成果物を揃える:

```bash
$ python -m shiftai solve --sample --out out --time-limit 60 --zip
必要人員: 6 日 / 25 時間帯 / 505.5 人時（ピーク 9 名）
最適化: 実行可能解（28.394 秒）
  時間制限内で実行可能解を得ました（目的関数=10942.82、所要28.39秒）。
配置カバー率: 100.0%（不足 0 時間帯）
  [要調整] MONTHLY_HOURS_EXCEEDED: S05 の勤務時間が 40.50 時間（6日間）で、…
  [要調整] BREAK_INSUFFICIENT: S04 は 2026-10-02 の休憩が 30 分しかなく、必要な 60 分に届いていません（基準 45 分）。
  [要調整] CONSECUTIVE_DAYS: S14 は 2026-10-02 から 6 日連続勤務しており、契約上限（5 日）を超えています。
  …
  [参考] HOURS_IMBALANCE: 勤務時間の偏りが大きいです（最大 40.5 時間 / 最小 0.0 時間）。
出力先: out
  shift.csv / payroll.csv / shift_matrix.csv / requirements.csv
  gap.csv / gap_daily.csv / violations.csv
  shift.ics / summary.md / bundle.zip
```

> サンプルは意図的に厳しい設定です（職員 28 名、必要人員のピークは 9 名、
> 1 日契約 8 時間）。そのため「配置カバー率 100%」でも要調整項目が残ります。
> 小さい入力でのクリーンな結果は次の Python API の例を見てください。

`summary.md` の中身（実際の出力）:

```markdown
# シフトサマリー: あさひ保育園

- 対象期間: 2026年10月2日(金) 〜 2026年10月8日(木)（6 日）
- 最適化状態: 実行可能解
- 目的関数値: 10,942.82
- 配置基準の不足セル: **0** 件
- 法令違反（ブロッカー）: **0** 件 / 要調整: 109 件
- 総実働時間: 514.00 時間
- 推定人件費合計: **1,049,064** 円（実働時間ベース。休憩は含まない）
```

実データで実行する場合は CSV を渡します:

```bash
python -m shiftai solve \
  --children    data/children.csv \
  --staff       data/staff.csv \
  --preferences data/preferences.csv \
  --standard    福岡市 \
  --out out --zip --strict
```

空のテンプレートから実データを作る場合:

```bash
python -m shiftai template --out template/
```

---

## Python API の最小例

3 歳児 5 名・保育士 4 名・1 日分です。**このコードは実行して出力を確認済み**です。

```python
import datetime as dt

from shiftai import local_rules, solver, standards
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    Role,
    StaffMember,
    StaffPreferences,
)
from shiftai.gap_analysis import check_violations

DAY = dt.date(2026, 10, 2)  # 金
DAY_OPEN, DAY_CLOSE, GRAN = dt.time(9, 0), dt.time(15, 0), 30

# --- 園児 5 名が 9:00-15:00 在園 ---------------------------------------
children = [
    ChildPlan(
        f"C{i:03d}",
        f"園児{i}",
        DAY,
        AgeClass.AGE_3,
        arrive=DAY_OPEN,
        depart=DAY_CLOSE,
        is_short_time=False,
        absent=False,
        absent_reason="",
        uses_early_care=False,
        uses_late_care=False,
        notes="",
    )
    for i in range(1, 6)
]

# --- 配置基準のプリセットを選ぶ ------------------------------------------
standard = local_rules.get_standard("全国基準（厚労省）")

# --- 「何人必要か」とその根拠を時間帯ごとに算出 --------------------------
table = standards.build_requirements(
    children,
    [DAY],
    standard,
    day_open=DAY_OPEN,
    day_close=DAY_CLOSE,
    granularity_min=GRAN,
)
print(f"必要人員: {len(table.slots)} 時間帯 / {sum(len(v) for v in table.rows.values())} 要件行")
for row in table.rows[DAY][:3]:
    print(
        f"  {row.slot.start:%H:%M}-{row.slot.end:%H:%M}  "
        f"必要 {row.needed_staff} 名（保育士 {row.needed_qualified} 名）"
    )
print(f"  根拠: {table.rows[DAY][0].basis}")

# --- 職員を用意して解を求める --------------------------------------------
contract = Contract(
    weekly_hours=40.0,
    daily_hours=8.0,
    employment_type=EmploymentType.SEI,
    min_monthly_hours=150.0,
    max_monthly_hours=185.0,
    max_weekly_days=5,
    max_consecutive_days=5,
    min_rest_hours=11.0,
    granularity_min=GRAN,
    earliest_start=dt.time(8, 0),
    latest_end=dt.time(17, 0),
)
staff = [
    StaffMember(f"S{i:02d}", name, (Role.HOIKUSHI,), contract)
    for i, name in enumerate(["井上", "小林", "斎藤", "松本"], 1)
]
prefs = {s.staff_id: StaffPreferences() for s in staff}

result = solver.solve_shift(children, staff, table, prefs, time_limit_sec=30, standard=standard)
print(f"\n解の状態: {result.status.value}  目的関数={result.objective_value}")

# --- 結果を見る ---------------------------------------------------------
shift_day = result.day(DAY)
required = {r.slot.label: r.needed_staff for r in table.rows[DAY]}
for slot in table.slots:
    worked = [s.staff_id for s in staff if shift_day.get(s.staff_id, slot).name == "WORK"]
    who = " ".join(worked) if worked else "(全員休息)"
    print(
        f"  {slot.start:%H:%M}-{slot.end:%H:%M}  "
        f"必要 {required[slot.label]} 名 → {len(worked)} 名  {who}"
    )

violations = check_violations(table, result, staff, prefs, standard=standard)
blocking = [v for v in violations if v.severity.name == "BLOCKER"]
print(f"\n違反: BLOCKER {len(blocking)} 件 / 全体 {len(violations)} 件")

# --- 職員ごとの勤務時間 -------------------------------------------------
print("\n職員ごとの勤務:")
for member in staff:
    minutes = sum(
        int(
            (
                dt.datetime.combine(DAY, slot.end) - dt.datetime.combine(DAY, slot.start)
            ).total_seconds()
            // 60
        )
        for slot in table.slots
        if shift_day.get(member.staff_id, slot).name == "WORK"
    )
    print(f"  {member.staff_id} {member.name}: {minutes / 60:.1f} 時間（{minutes} 分）")
```

**実出力**:

```text
必要人員: 12 時間帯 / 12 要件行
  09:00-09:30  必要 2 名（保育士 1 名）
  09:30-10:00  必要 2 名（保育士 1 名）
  10:00-10:30  必要 2 名（保育士 1 名）
  根拠: 3歳児 5名 ÷ 8 = 1名（定員比8:1、切り上げ）／保育標準時間／2名ルールによる底上げ

解の状態: 最適解  目的関数=12.0

  09:00-09:30  必要 2 名 → 3 名  S02 S03 S04
  09:30-10:00  必要 2 名 → 3 名  S02 S03 S04
  10:00-10:30  必要 2 名 → 3 名  S01 S02 S03
  10:30-11:00  必要 2 名 → 3 名  S01 S02 S03
  11:00-11:30  必要 2 名 → 3 名  S01 S02 S04
  11:30-12:00  必要 2 名 → 3 名  S01 S02 S04
  12:00-12:30  必要 2 名 → 4 名  S01 S02 S03 S04
  12:30-13:00  必要 2 名 → 3 名  S01 S03 S04
  13:00-13:30  必要 2 名 → 3 名  S01 S03 S04
  13:30-14:00  必要 2 名 → 4 名  S01 S02 S03 S04
  14:00-14:30  必要 2 名 → 4 名  S01 S02 S03 S04
  14:30-15:00  必要 2 名 → 4 名  S01 S02 S03 S04

違反: BLOCKER 0 件 / 全体 0 件

職員ごとの勤務:
  S01 井上: 5.0 時間（300 分）
  S02 小林: 5.0 時間（300 分）
  S03 斎藤: 5.0 時間（300 分）
  S04 松本: 5.0 時間（300 分）
```

この例が示している点:

- 必要人員は **「3歳児 5名 ÷ 8 = 1名（定員比8:1、切り上げ）／保育標準時間／2名ルールによる底上げ」**
  という根拠つきで算出されます。`basis` の文字列をそのまま説明に使えます。
- 必要 2 名に対して 3〜4 名を配置しているのは、**2 名ルールと休憩の制約**のためです
  （同時に休憩に入れる人員が限られます）。
- 小規模なら CBC が最適性を証明して `最適解` になります。本番規模は
  `実行可能解` 止まりが通常です。

---

## 画面（Streamlit）

| モード | タブ |
| --- | --- |
| シンプル（既定） | 1. データ → 2. シフト作成 → 3. 出力 |
| 上級者 | 1. データ投入 → 2. 必要人員 → 3. シフト作成 → 4. シフト表・微調整 → 5. 出力 |

```bash
streamlit run streamlit_app.py
```

操作の要点:

- **データ** … 最初に「ウィザードで順に入力する / まとめて入力する」を選びます。
  - *ウィザード* … (1) 園の条件 / (2) 園児 / (3) 職員 / (4) 希望休 / (5) 確認 の 5 ステップ。
    ステップごとに「ファイルを選ぶ / サンプルを使う / 直接入力する」を選べます。
    サンプルで流れを通したうえで、実データに差し替えてください。
  - *まとめて入力* … CSV / Excel / JSON をアップロード、または「サンプルデータを読み込む」。
    取り込んだ表はそのまま編集でき、Undo / Redo が使えます。
- **必要人員** … 時間帯ごとの必要人数と、その根拠文字列。配置基準を切り替えられます。
- **シフト作成** … 目的関数の重みをスライダーで調整してから実行。
  解けなかった場合は Slack / IIS 診断と緩和ラダーを表示します。
- **シフト表・微調整** … 生成後の表を直接編集。**変更が配置基準に抵触すると確定ボタンが
  無効化**され、即時検証が理由を示します。
- **出力** … CSV / Excel / ical / ZIP をダウンロード。

---

## 何ができるか

- **配置基準の自動適用** … 保育所の職員配置基準（定員比・資格内数・代替措置）を
  12 種類のプリセットで適用します。基準ごとに必要人数が変わります。
- **MILP での最適化** … PuLP + CBC。勤務セルと休憩セルを整数変数として扱い、
  必要人員・休憩・契約時間・希望休などを同時に満たす勤務表を解きます。
- **根拠の可追跡性** … 必要人員の判断根拠を文字列で保持し、出力にも含めます。
- **過不足と違反の可視化** … 20 種類の違反コード
  （`SHORTFALL_STAFF`、`BREAK_INSUFFICIENT`、`REST_HOURS_SHORT`、
  `MONTHLY_HOURS_EXCEEDED` など）で検査します。
- **公平性の考慮** … 早番・遅番・土曜出勤の偏りを目的関数で抑えます（既定の重み 4.0）。
- **勤務パターンの整列** … 早番 / 日勤 / 遅番のパターンを定義し、各職員に割り当て直します。
- **解けないときの原因分析** … 緩和ラダーと Slack / IIS 診断を提供します。
- **複数の出力形式** … CSV（UTF-8 BOM）/ Excel / ical / ZIP バンドル。
- **GAS 連携** … Google スプレッドシート（Apps Script）との出勤連携（任意）。

---

## 制約の扱い（ハード / ソフト）

**ハード制約**（解は必ず満たします。満たせなければ「解なし」として報告します）:

- 配置基準の必須行（必要人員・必要保育士数）
- 希望休・出勤不可 / 年間休業日 / 祝日の出勤不可
- 契約時間帯（最早始業〜最遅終業）/ 1 日の上限時間
- 勤務と休憩のセル排他とブロック構造

**ソフトペナルティ**（重みで調整します。違反しても解は出ますが、
**検査で警告**します）:

- 連続勤務日数 / 週の最大出勤日数
- 最低休憩時間 / 勤務間の休息時間
- 早朝・延長の回避 / 勤務時間の偏り / 月間時間
- 未使用職員 / 勤務希望

**週契約時間は MILP の制約ではありません。** 最適化後の検査で警告する項目です
（供給量の不足は事前に診断します）。
詳細は [ソルバ仕様](docs/02_ソルバ仕様.md) §4.3 を参照してください。

違反の重大度は 2 段階です:

- `BLOCKER` … 配置基準の不足や出勤不可への出勤。UI では赤く表示されます。
- `WARNING` / 参考 … 上記のソフト項目の超過。

`shiftai solve` の終了コード:

| コード | 意味 |
| ---: | --- |
| 0 | ブロッカーなし（要調整は許容） |
| 1 | エラー |
| 2 | ブロッカーあり |
| 3 | ブロッカーはないが要調整あり（`--strict` 指定時のみ） |

---

## CLI リファレンス

```bash
python -m shiftai --help
python -m shiftai ui          # Streamlit アプリ
python -m shiftai presets     # 配置基準のプリセット一覧
python -m shiftai compliance  # 制度別の適合チェック（届出・月次報告向け）
python -m shiftai sample      # サンプル CSV を出力
python -m shiftai template    # 空テンプレート CSV を出力
python -m shiftai solve       # ヘッドレス実行
```

### 制度別の適合チェック（`compliance`）

シフト作成とは別に、**届出・月次報告・巡回指導で出す判定**を行う。
1 日の常勤換算人数と施設の属性（利用定員・開所時間・保育事業者型の有無）で
12 項目を判定し、「適合／不適合／該当なし／未確認」を**根拠つきで**出す。

```bash
python -m shiftai compliance \
  --children children.csv --staff staff.csv \
  --regulation 認可外保育施設 --capacity 40 --support-certified
```

| オプション | 既定 | 意味 |
| --- | --- | --- |
| `--regulation` | `認可外保育施設` | 制度名（認可保育所／認可外保育施設／企業主導型保育事業／小規模保育事業／事業所内保育事業） |
| `--capacity` | 0 | 利用定員 |
| `--shared-operator` | （無効） | 保育事業者型事業（共同利用枠）を実施しているとする |
| `--open` / `--close` | 07:15 / 19:30 | 開所・閉所時刻（11 時間ルールに使う） |
| `--support-certified` | （無効） | 保育士・看護師・調理員以外を研修修了者・受講予定者として扱う |
| `--out` / `--json` | （なし）/（無効） | 結果を CSV / JSON で出力 |

終了コード: **0**（報告可）／**3**（不適合または未確認あり）／**1**（入力エラー）。
嘱託医・調理業務の委託形態・面積は本ツールでは判れないため**未確認**で出し、
`0`（報告可）にはしません。詳細は
[`docs/03_配置基準と自治体ルール.md`](docs/03_配置基準と自治体ルール.md) §10。

### 対応している制度

| 制度 | プリセット | 算出手法 |
| --- | --- | --- |
| 認可保育所（11 自治体） | 全国基準・東京都ほか | 年齢クラス毎に切り上げ |
| 認可外保育施設 | 認可外保育施設（指導監督基準） | 区分ごとに切捨て → 合計 → ＋1 → 四捨五入 |
| 企業主導型保育事業 | 単独枠 / 保育事業者型 | 同上 |

**認可保育所のプリセットを認可外保育施設に使わないでください。** 算手法・資格要件が
異なり、1 時間帯あたり最大 4 名ずれます（0.5% の確率で基準を満たしていると誤判定）。
小規模保育事業・事業所内保育事業は未対応です。

`shiftai solve` の主なオプション:

| オプション | 既定値 | 意味 |
| --- | --- | --- |
| `--children` / `--staff` / `--preferences` | なし | 入力 CSV / Excel / JSON |
| `--standard` | `全国基準（厚労省）` | 配置基準のキー |
| `--out` | `out` | 出力先ディレクトリ |
| `--start` / `--end` | 入力データの日付から自動決定 | 対象期間 |
| `--closed` | なし | 年間休業日（1 回 1 日。繰り返す） |
| `--holiday` | なし | 祝日・行事日（1 回 1 日。繰り返す） |
| `--open` / `--close` | 7:15 / 19:30 | 開所・閉所時刻 |
| `--granularity` | 30 | 時間帯の粒度（分） |
| `--time-limit` | 60 | ソルバの時間上限（秒） |
| `--zip` | （無効） | 成果物をまとめた `bundle.zip` も出力 |
| `--strict` | （無効） | 要調整があれば終了コード 3 |
| `--relax` | 0 | 制約を何段階まで緩めるか（0 = 緩和なし） |
| `--diagnose` / `--ladder` | （無効） | 診断出力 / 緩和ラダーの詳細 |
| `--sample` | （無効） | サンプルデータを使う |
| `--seed` | 42 | サンプルの乱数シード |

`--closed` と `--holiday` はどちらも繰り返し指定（`action="append"`）です。
カンマ区切りの 1 つの値ではありません。

---

## 入出力

### 入力（3 ファイル）

| ファイル | 必須 | 内容 |
| --- | --- | --- |
| `children.csv` | ✔ | 園児ID / 年齢 / 登降園日 / 登園・降園時刻 / 短時間保育 / 欠席 / 早朝・延長保育 |
| `staff.csv` | ✔ | 職員ID / 資格 / 雇用形態 / 週・日契約時間 / 週最大出勤日数 / 最大連続勤務日数 / 最早始業・最遅終業 |
| `preferences.csv` | — | 職員ID / 種別（出勤不可・休み希望）/ 日付 / 理由 |

列の完全な仕様と例は [データ仕様書](docs/04_データ仕様.md) にあります。

### 出力（`--zip` で最大 10 ファイル）

必ず出力される 7 つ:

`shift.csv` / `payroll.csv` / `shift_matrix.csv` / `shift.ics` / `summary.md` / `shift.xlsx`
に `bundle.zip` を加えたものです。

条件付き 3 つ:

| ファイル | 出力条件 |
| --- | --- |
| `requirements.csv` | 必要人員の表を渡したとき |
| `gap.csv` / `gap_daily.csv` | 過不足の診断を行ったとき（2 つ同時） |
| `violations.csv` | 違反が 1 件以上あるとき |

CSV はすべて **UTF-8 with BOM** なので Excel でそのまま開けます。

`payroll.csv` の `実働時間` は `総勤務時間` と同じ値です。**休憩を二重控除しません**
（`総勤務時間` は最初から `BREAK` を含まない値です）。

---

## 開発

```bash
pip install -e ".[dev]"                  # 実行時 + 開発用依存

python -m pytest                         # 全テスト（CBC を起動する統合テストを含む）
python -m pytest -m "not slow"           # 速いテストだけ（推奨 / 1〜2 分）
python -m pytest -m slow                 # 遅い統合テストだけ
python -m pytest -n auto -m "not slow"   # 並列実行

ruff check .                             # 静的検査
ruff format .                            # 整形
```

`make` が使える環境（macOS / Linux の GNU make）では同じ操作が
`make test-fast` / `make lint` などでできます。`make help` で一覧が出ます。

### 構成

```
src/shiftai/
  domain.py          データクラス（園児・職員・基準・解など）
  standards.py       配置基準エンジン（必要人員と根拠を算出）
  solver.py          MILP モデリングと CBC 実行
  local_rules.py     自治体のプリセット
  gap_analysis.py    過不足と違反の検査（20 コード）
  diagnostics.py     Slack / IIS 診断と緩和ラダー
  fairness.py        早番・遅番・土曜出勤の公平性集計
  shift_patterns.py  勤務パターンの定義と整列
  live_validation.py 編集中の即時検証
  data_loader.py     入力の読み込みとヘッダ正規化
  importers.py       園業務 CSV 連携
  exporter.py        出力（CSV / Excel / ical / ZIP）
  sample_data.py     サンプルデータ生成
  ui/                Streamlit の画面（state / sidebar / 各タブ）
  __main__.py        CLI
```

設定は `pyproject.toml` に集約しています（依存、ruff 設定、pytest 設定）。

---

## 既知の制約と注意点

### 1. 1 日の上限時間で、ソルバと検査の基準が食い違っています（要修正）

ソルバのハード制約は `solver._daily_cap_minutes` で、正職員 8 時間契約なら
`8h × 1.25` で最大 10 時間（10 時間で頭打ち）です。
一方、検査の `STATUTORY_DAILY_HOURS` は 1 日 8 時間 45 分を超えたら警告します
（`gap_analysis._STATUTORY_DAILY_LIMIT_HOURS = 8.75`）。

そのため `shiftai solve --sample` の既定出力には `STATUTORY_DAILY_HOURS` の
警告が含まれ得ます。ソルバ側を 8.75 時間に合わせるか、検査側を 10 時間まで
緩めるかは方針の判断が必要なため、現時点ではどちらにも寄せずに如实報告しています。

### 2. CBC は決定的ではありません

同じ入力でも実行ごとに解がわずかに変わることがあります。

### 3. 目的関数値は金額でも人時でもありません

配置の過不足や環境条件に重みを付けたスコアです。比較のための指標であり、
法的な最適性を保証するものではありません。

### 4. 本番規模では最適解が得られません

変数 14,039 個・制約 18,906 本の規模では、`--time-limit` の範囲に収まるのは
通常 `実行可能解`（Feasible）です。最適性の証明が必要な検証は小さい入力で
行ってください（[ソルバ仕様](docs/02_ソルバ仕様.md) §2.7 / §8）。

### 5. 配置基準には前提があります

3 歳未満の園児では定員比の切り上げにより基準を満たさないことがあり、
必須要件として扱うかどうかは `is_binding` で切り替えます。

---

## 詳細ドキュメント

| 文書 | 内容 |
| --- | --- |
| [アーキテクチャ](docs/01_アーキテクチャ.md) | 全体の構成とデータの流れ |
| [ソルバ仕様](docs/02_ソルバ仕様.md) | MILP の変数・制約・目的関数、実測モデル規模 |
| [配置基準と自治体ルール](docs/03_配置基準と自治体ルール.md) | 配置基準の内容と自治体の差 |
| [データ仕様](docs/04_データ仕様.md) | CSV / Excel / 出力の列定義 |
| [開発ガイド](docs/05_開発ガイド.md) | セットアップ、Makefile、テスト戦略 |
| [運用手引き](docs/06_運用手引き.md) | 現場での使い方、トラブルシュート |
| [回帰防止の実装計画](docs/07_実装計画_回帰防止.md) | テストとガードの方針 |
| [商業品質向上の実装計画](docs/08_実装計画_公平性・即時検証・CSV連携.md) | 機能追加の計画 |
| [商業品質向上（Round 2）](docs/09_実装計画_商業品質向上.md) | 未着手の改善項目 |

---

## 免責

本ソフトウェアは配置基準と労働法令の**補助**として提供されます。
生成された勤務表は必ず現場で確認し、最終判断は現場の責任で行ってください。
基準の内容と解釈は施設や自治体ごとに異なります。
本ソフトウェアの使用によって生じたいかなる損害についても、責任を負いません。
