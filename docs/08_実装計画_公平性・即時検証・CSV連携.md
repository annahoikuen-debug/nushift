# 08 実装計画：公平性ペナルティ／即時バリデーション／園業務CSV連携

本書は、旧「提案一覧」其中的以下 3 件を実装するための計画書である。

| 優先 | 提案 | 対応モジュール |
| --- | --- | --- |
| 4 | 実務・公平性: 早番・遅番・土曜出勤の公平配分制約 | `fairness.py` / `solver.py` |
| 5 | UI/UX: 配置基準リアルタイム即時バリデーション | `live_validation.py` / `ui/tab_shift.py` |
| 6 | データ連携: CoDMON / キッズリー等の CSV インポート | `importers.py` / `ui/tab_data.py` |

---

## 1. 目的と背景

### 1.1 公平性

シフト作成者の最大の悩みは「誰かに早番・土曜が偏ることによる人間関係の軋轢」である。
現状のソルバは **勤務時間（時間数）** の偏りしか罰していない（`hours_imbalance_penalty`）。
時間数が同じでも「Aさんは早番5回・Bさんは早番0回」なら現場では明显に不公平である。

したがって **「時間帯区分の回数」についての均等度** を目的関数に足す。

### 1.2 即時バリデーション

現状のタブ4は `st.data_editor` で手動編集するが、判定は「確定して再最適化」を
押すまで行われない。急な休み対応では、シフトを確定して1セルだけ動かしても
「基準を割っていないか」が分からず、確定を取り消して調整し直す往復が発生する。
そこで **確定前** に、編集中のグリッドをそのまま評価して色表示する。

### 1.3 CSV 連携

園児の登降園予定は実運用では CoDMON / キッズリー 等の園業務支援 reluctanceから CSV 出力する。
本アプリ独自の列名で手入力するのは導入ハードルが高いため、**マッピングアダプター**を用意する。

---

## 2. 設計方針（3件に共通）

* **既存層に混ぜない。** 新しい責務は新しいモジュールに置き、`solver.py` / `tab_*.py` から
  呼び出すだけに留める。これにより回帰の切り分けが容易になる。
* **副作用を持たない（コア側）。** `fairness.py` / `live_validation.py` / `importers.py` は
  Streamlit も PuLP も触らない純粋関数のみ。UI 層の描画は `ui/` 側で行う。
* **省略時は完全互換。** 既定値を「無効」または「現在の挙動と同じ」にする。
  既存テストが一つも壊れないことを必須条件とする。
* **簡体字を混入させない。** 静的ガード `test_簡体字が混入していない` があるため、
  日本語文言は既存表記（`**配置**` / `基準` / `偏り`）に揃える。

---

## 3. 仕様1: 早番・遅番・土曜出勤の公平配分

### 3.1 対象とする回数

| キー | 定義 | 判定方法 |
| --- | --- | --- |
| `early` | 早番の回数 | 勤務ブロックの**開始時刻**が `standard.early_care_window[0]` 以下、または勤務パターンの `early` 枠と一致 |
| `late` | 遅番の回数 | 勤務ブロックの**終了時刻**が `standard.late_care_window[1]` 以降、または勤務パターンの `late` 枠と一致 |
| `saturday` | 土曜出勤日数 | `weekday() == 5` に出勤（`can_work_holiday` が真の職員のみ対象） |

日曜は休園日のため原則 0 であり、平日を超えたら休園日設定の不整合なので対象にしない。

### 3.2 集約级别

等間隔に **1 週間の窓**（`domain.weekly_periods`）で区切り、**週ごとに**均等度を取る。
全期間を一括で均すと「前半だけ偏る」情報が隠れるため。

### 3.3 目的関数への組み込み

職員 `s`・週 `w`・区分 `k` について、勤務ブロックの開始/終了時刻から
[bool 式] `x_{s,w,k} ∈ {0,1}` を作り、

```
count_{s,w,k} = Σ_{d∈w} x_{s,d,k}
```

各区分について **最大値・最小値** を補助変数 `max_k`, `min_k` で表し、

```
max_k ≥ count_{s,w,k}    (∀s,w)
min_k ≤ count_{s,w,k}    (∀s,w)
objective += fairness_penalty_k × Σ_w (max_k^w − min_k^w)
```

とする。これにより **「最大と最小の差（レンジ）」を最小化**する。
单纯的「総和を均す」榜一ではないのは、職員数が減るとレンジが縮む性質があるため。

重みは `ObjectiveWeights` に次の 3 項目を追加する。

```python
fairness_early_penalty: float = 4.0
fairness_late_penalty: float = 4.0
fairness_saturday_penalty: float = 4.0
```

> **当初計画は「既定 0.0 = 無効」だったが、実装時に 4.0（有効）に変更した。**
> 0.0 だと公平性グループが目的関数に何も寄与せず、排出が偏った解が
> 「最適」として返っていたため。現在の既定は §8 のとおり **4.0 で有効**。
> 無効化するには `drop_groups={"fairness"}` を使う。

### 3.4 制約選択の理由

* **ハード制約にしない。** 早番を使える職員が 2 名しかいない fortnight 園では均衡が物理的に
  ありえない。ハードにすると解が消える（現状の最主要リスク）。
* **max−min をinda_each 週で取る。** 全期間一括だと解の自由度が高すぎて CBC が間に合わない。

### 3.5 影響範囲

* `domain.ObjectiveWeights`: 3 フィールド追加（後方互換）。
* `relaxation.py`: `relaxed_weight_names` に fairness を追加し、緩和時に無効化できるようにする。
* `solver.py`: `_add_fairness(ctx, staff, requirements, obj, weights, patterns)` を新設し、
  `_build_problem` から呼び出す。`drop_groups` に `"fairness"` を追加。
* `ui/state.py`: `WEIGHT_WIDGETS` に 3 スライダーを追加（既定 0）。
* `ui/sidebar.py`: 上記スライダーの説明文を補記。

### 3.6 レポート

* `fairness.counts(result, staff, slots, standard, patterns)` → 職員×区分の回数表。
* `fairness.report_frame(...)` → 職員ごとの「早番/遅番/土曜」とレンジの DataFrame。
* `fairness.spread_stats(...)` → レンジの最大値など要約。
* 公平性の偏りは airness.spread_stats で算出・表示する（違反コードは追加していない。コード種別は労働基準・契約の検証に限定している）。
* `ui/components.py` に `fairness_frame` / `style_fairness_table` を追加（色分け）。
* `ui/tab_solve.py` / `ui/tab_shift.py` に表示。

---

## 4. 仕様2: 手動編集の即時バリデーション

### 4.1 判定対象

編集中の（未確定の）グリッドを、確定済みとして hypothetically 評価する。

| 深刻度 | 判定 | 例 |
| --- | --- | --- |
| `error` | 配置基準の不足 | 「この時間帯、必要 3 名に対し配置 2 名」 |
| `error` | 保育士数の不足 | 「必要保育士 2 名に対し 1 名」 |
| `error` | 契約時間帯外への勤務 | 「最早始業 8:30 より前の勤務」 |
| `error` | 希望休・不在時間帯への勤務 | 「出勤不可日の勤務」 |
| `warning` | 1日の契約時間超過 | 「7.5h（契約 5.0h）」 |
| `warning` | 最低休憩時間の不足 | 「6時間超勤務だが休憩なし」 |
| `warning` | 勤務ブロックの分断 | 「勤務－オフ－勤務」 |
| `warning` | 曜日の過不足 | 「土曜出勤が 5 名」 |

### 4.2 出力

```python
@dataclass(frozen=True)
class LiveIssue:
    level: str  # "error" | "warning"
    code: str
    message: str
    staff_id: str  # 該当職員（時間帯全体の指摘なら ""）
    slot_label: str  # 該当時間帯（職員全体の指摘なら ""）
```

* `live_validation.validate_day(...) -> list[LiveIssue]`
* `live_validation.to_dataframe(issues) -> DataFrame`
* `live_validation.cell_styles(frame, issues) -> Styler`
  - `error` のセルは赤枠（`box-shadow: inset 0 0 0 2px #C62828`）
  - `warning` のセルは橙枠
  - 時間帯列全体の指摘（配置不足）は**列見出し側**を赤にする

### 4.3 UI 統合

* `ui/components.py` に `style_live_grid(frame, issues)` を追加（`style_shift_grid` の拡張）。
* `ui/tab_shift.py::_render_editor` 内で `st.data_editor` の直後に
  `live_validation.validate_day` を呼び、
  * `error` があれば赤い `st.error` + 内訳 DataFrame
  * `warning` があれば黄色い `st.warning`
  * セルの警告は下の「編集中のシフト（ライブ検証）」テーブルに**色付きで**表示

既存 `st.data_editor` 自体は `Styler` を渡せないため、**隣接に読み取り専用の色付きビュー**を出す
（2026 年現在 Streamlit の制約）。編集体験は損なわない。

### 4.4 既存 `gap_analysis` との重用

判定ロジック（`_required_break_minutes` / `_is_contiguous` など）は `gap_analysis` と共有する。
`live_validation.py` 側は `gap_analysis` から参照し、逆依存を作らない。

---

## 5. 仕様3: 園業務支援システム CSV インポート

### 5.1 対応形式

| プロファイル | 対象 | 主キー |備考 |
| --- | --- | --- | --- |
| `codemon` | CoDMON（ Kendix ）利用園 | 園児ID = `園児コード` | 「登園日/退園日/保育標準時間区分」形式 |
| `kids_ryu` | キッズリー利用園 | 園児ID = `園児ID` | 「登園予定時刻/降園予定時刻」形式 |
| `generic` | 上記以外の全园業務システム | 手動マッピング |  Wage サイトのテンプレート |

### 5.2 設計

```python
@dataclass(frozen=True)
class ImportProfile:
    key: str
    label: str
    column_map: Mapping[str, str]     # 正規列名 → プロファイル側の列名候補（先頭一致）
    defaults: Mapping[str, str] = ...  # 例: 延長保育の既定値
    notes: str = ""

def list_profiles() -> list[ImportProfile]
def get_profile(key: str) -> ImportProfile
def detect_profile(frame: DataFrame) -> str        # 列名から推測
def convert(frame: DataFrame, profile_key: str) -> ConvertResult
```

`ConvertResult` は

```python
@dataclass(frozen=True)
class ConvertResult:
    frame: DataFrame  # data_loader.CHILDREN_COLUMNS に揃えた DataFrame
    mapping: dict[str, str]  # 正規列名 → 実際に採用した入力列名
    missing: tuple[str, ...]  # 入力になかった正規列（既定値で埋めた）
    notes: tuple[str, ...]  # 運用上の注意（例: 「短時間保育の判定は保育標準時間区分のみ」）
```

を含む。

### 5.3 値の正規化

* 日付: `data_loader.parse_date` に委譲（`20260901` / `2026/9/1` / `R8.9.1` に対応）。
  - 和暦表記 `R6.9.1` は西暦変換する（2026 年固定ではなく `+2018`）。
* 時刻: `data_loader.parse_time` に委譲。
* 人数/年齢: `_parse_age_class` 相当。
* 短時間保育: CoDMON の「短時間保育」区分、`9:00-14:00` のような保育標準時間帯から推定。

### 5.4 UI

* `ui/tab_data.py::_preview_table("children", ...)` の中に
  「🔗 園業務支援システムから取り込む」 expander を追加。
* `st.file_uploader` → `st.selectbox`（プロファイル選択、`detect_profile` を既定に）
  → 変換結果のプレビュー（先頭 20 行） → 「この内容で取り込む」ボタン。
* 取り込み時は既存の「ファイルアップロード」と同じ `st.session_state[f"frame_children"]` に書き、
  `_record_edit` で履歴に残す。

---

## 6. テスト計画

| ファイル | 内容 |
| --- | --- |
| `tests/test_fairness.py` | `counts` / `report_frame` / `spread_stats` の正しさ、`Solver` 統合（重み>0 でレンジが縮むこと、slow） |
| `tests/test_live_validation.py` | 配置不足 / 契約違反 / 希望休 / 休憩不足 / 分断の検出、正常系 |
| `tests/test_importers.py` | 各プロファイル変換、`detect_profile`、欠落列の既定値、和暦 |
| 既存 | 全体回帰（`make test`） |

`test_static_guards.py` の **T-11-R1（死んだ公開関数がない）** に注意。
新モジュール})}{ new public 関数は `src` か `tests` のどちらかで必ず参照すること。

---

## 7. 実装順序

1. `fairness.py`（純粋関数） → テスト
2. `domain.ObjectiveWeights` / `relaxation.py` / `state.WEIGHT_WIDGETS` → 既存テスト回帰確認
3. `solver._add_fairness` → 統合テスト（slow）
4. `ui/components.py` / `ui/tab_solve.py` / `ui/tab_shift.py` へ表示を追加
5. `live_validation.py` → テスト → `ui/tab_shift.py` 統合
6. `importers.py` → テスト → `ui/tab_data.py` 統合
7. `docs/02_ソルバ仕様.md` / `docs/04_データ仕様.md` の追記、`make lint` / `make test`

---

## 8. リスクと緩和

| リスク | 緩和 |
| --- | --- |
| 公平性変数の追加で CBC が遅くなる | 既定重み 4.0（fairness グループは既定で有効）。`drop_groups={"fairness"}` で無効化できる |
| 均衡制約により「解なし」 | ハード制約にしない。全てソフト |
| `st.data_editor` に Styler を渡せない | 隣接に読み取り専用の色付きビューを出す |
| CoDMON の CSV は施設ごとの差異がある | プロファイル追加が容易な構造（`column_map` は候補リスト）にする。未対応は `generic` で通知を回避 |
| 既存動作の変化 | ペナルティ重みは既定で有効（4.0）。既存テストは公平性条件を満たした解を検証済み |