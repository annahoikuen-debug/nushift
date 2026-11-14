# 違反修正ガイダンス改善 詳細実装計画書
## 「修正ボタンを出せません」を部分指定ボタンと直し方ガイドに置き換える（案1＋案2）

- **作成日**: 2026-11-14
- **対象プロジェクト**:
  - `NuShift` (`E:\nushift\nushift`) — 実装本体
  - `open-hoikuict` (`C:\Users\user\openict\open-hoikuict`) — vendor 同期のみ（ロジック改変なし）
- **関連ドキュメント**:
  - `nushift/docs/phase2-solver-unification-and-design-harmonization-plan.md`（計画書形式の先例）
  - `open-hoikuict/scripts/sync_nushift_vendor.py`（vendor 同期スクリプト）
  - `open-hoikuict/AGENTS.md`（仕様確認→モック試用→実装の手順規定）

---

## 1. 背景・現状の問題

### 1.1 現行実装

違反一覧の下にある「#### 🛠 この違反を修正する」セクション（`src/shiftai/ui/components.py:852` `render_violation_actions`）は、
`Violation` の **日付・時間帯・職員IDの 3 つがすべて揃った違反だけ** に修正ボタンを出す
（`violation_targets` `components.py:820` は 3 つ揃っていないものをすべて除外）。

3 つのうちどれか 1 つでも欠けると、違反は `unfixable_violations`（`components.py:841`）に分類され、
次の汎用メッセージだけが出る:

> この違反は 1 つのセルに特定できないため、修正ボタンを出せません。
> 下の違反一覧の内容を確認し、園の条件や職員データを見直してください。

### 1.2 問題点

1. **大半の違反は「部分特定可能」なのに、導線が一切出ない。**
   実測で、`gap_analysis.check_violations` が返す違反は次の通り座標の揃い方が 4 種類ある（§3 の分類表）。
   現行は「3 つ全部揃っている違反」以外をすべて詰め込んでおり、たとえば
   `SHORTFALL_STAFF`（日付・時間帯は分かっている）も `MONTHLY_HOURS_EXCEEDED`（職員は分かっている）も
   同じ「見直してください」扱いになる。
2. **`aggregate_violations`（`gap_analysis.py:1074`）が同一 (code, staff, day) を集約する際に `slot=None` にする**ため、
   もともとセル特定できた違反（`WORK_ON_UNAVAILABLE` など複数時間帯に跨るもの）まで
   現行 UI では「修正ボタンなし」に落ちる。セル修正ボタンが実際に出る場面はかなり限られる。
3. **メッセージが「できない理由」の説明で終わっており、次の一手がない。**
   「園の条件や職員データを見直してください」は責任の丸投げに近く、
   違反コードごとの具体的な直し方（どのタブの何をどう変えるか）が示されていない。

### 1.3 改善方針（採用した案）

- **案1（部分指定ボタン）**: 揃っている座標の数に応じてボタンを出し分ける。
  「日付＋時間帯」まで分かっている違反なら *その列に人員を追加する* 導線を、
  「職員」まで分かっている違反なら *その職員の行を調整する* 導線を出す。
  押すとシフト表タブ（上級モード: タブ4、シンプルモード: ② シフト作成内）へ自動遷移し、
  対象の時間帯列・職員行をハイライトし、時間帯不足なら **オフの職員候補を提示** する。
- **案2（コード別ガイド）**: 違反コードごとの「直し方」文言を辞書化し、
  修正ボタンが出ない違反には具体的手順を表示する。違反一覧の表にも「直し方」列を追加する。

---

## 2. スコープ

### 対象

| 層 | ファイル | 変更内容 |
|---|---|---|
| UI 共通 | `src/shiftai/ui/components.py` | 分類関数・部分指定ターゲット・ガイド辞書・`render_violation_actions` 再構成・`violation_dataframe` 列追加 |
| UI 状態 | `src/shiftai/ui/state.py` | `set_fix_target` / `take_fix_target` の型を None 許容に拡張（docstring・型ヒントのみ、ロジック変更なし） |
| シフト表 | `src/shiftai/ui/tab_shift.py` | `_apply_fix_target` の部分指定対応・職員候補表示・`_fix_staff_id` ハイライト |
| 出力/作成 | `src/shiftai/ui/tab_export.py`・`ui/tab_solve.py` | 関連キャプション文言の整合（`tab_export.py:338/405/486` 付近の説明文） |
| テスト | `nushift/tests/*` | 新規テスト + 既存テスト拡張（§7） |
| vendor | `open-hoikuict/vendor/nushift/shiftai/**`・`VENDOR.json` | 同期スクリプトで自動生成（手編集しない） |

### 非対象

- ソルバー・ギャップ分析のロジック変更（`gap_analysis.py` の違反生成コードは変更しない）
- `Violation` データクラス自体の変更（`domain.py` は触らない）
- 確認済み（`acknowledged`）機構、出力ゲートの条件変更

---

## 3. 違反コード × 修正対象レベル分類表

`gap_analysis.py` の生成箇所を全数調査した結果。**実装はこの表を `classify_violation` の単一テーブルとしてコード化し、個別の if 分岐を書かない。**

| レベル | code | day | slot | staff_id | 備考 |
|---|---|---|---|---|---|
| **cell**（3/3 揃う） | `WORK_ON_UNAVAILABLE` | ○ | ○ | ○ | 集約後は slot=None＋`detail["slots"]` |
| | `WORK_ON_CLOSED_DAY` | ○ | ○ | ○ | 同上 |
| | `OUTSIDE_CONTRACT_HOURS` | ○ | ○ | ○ | 同上 |
| **slot**（日付＋時間帯） | `SHORTFALL_STAFF` | ○ | ○ | — | 不足人数は `detail["needed"]` |
| | `SHORTFALL_QUALIFIED` | ○ | ○ | — | `detail["needed_qualified"]` |
| | `BREAK_OVERLAP` | ○ | ○ | — | 最悪 1 箇所のみ生成 |
| **staff**（職員。day は有無両方あり得る） | `DAILY_HOURS_EXCEEDED` | ○ | — | ○ | |
| | `OVERTIME_NOT_ALLOWED` | ○ | — | ○ | |
| | `OVERTIME_REQUIRES_36AGREEMENT` | ○ | — | ○ | |
| | `BREAK_INSUFFICIENT` | ○ | — | ○ | |
| | `BREAK_FRAGMENTED` | ○ | — | ○ | `detail["slots"]` に時間帯あり |
| | `SPLIT_SHIFT` | ○ | — | ○ | |
| | `CONSECUTIVE_DAYS` | ○（開始日） | — | ○ | |
| | `WEEKLY_DAYS_EXCEEDED` | ○（週初） | — | ○ | |
| | `REST_HOURS_SHORT` | ○（翌日） | — | ○ | |
| | `WEEKLY_HOURS_EXCEEDED` | — | — | ○ | 週範囲はメッセージ内のみ |
| | `MONTHLY_HOURS_EXCEEDED` | — | — | ○ | |
| | `MONTHLY_HOURS_SHORT` | — | — | ○ | |
| | `VARIABLE_HOURS_EXCEEDED` | — | — | ○ | |
| | `VARIABLE_HOURS_EXCEEDED_NO_36` | — | — | ○ | |
| | `AVOID_EARLY_CONFLICT` | — | — | ○ | |
| | `AVOID_LATE_CONFLICT` | — | — | ○ | |
| **guide**（座標なし） | `HOURS_IMBALANCE` | — | — | — | 全体バランス問題 |
| | `CHECK_UNAVAILABLE` | — | — | — | 検査未完（対象外） |
| | `NOT_PLACED_ROLE` | — | — | — | 配置対象外職種（対象外） |

判定ルール（優先順）: `day かつ slot かつ staff_id → cell` ／ `day かつ slot → slot` ／ `staff_id → staff` ／ `それ以外 → guide`。
集約違反（`slot=None` かつ `detail["slots"]` あり）は staff レベル扱いとし、対象時間帯は案内文に列挙する。

---

## 4. 仕様詳細

### 4.1 修正ターゲットのデータ構造（`state.py`）

現行: `KEY_FIX_TARGET = (iso_day, staff_id, slot_label)` の 3 要素タプル（全要素非 None）。

変更: **欠けている座標を `None` で埋めた 3 要素タプル** を許容する（データクラス化はしない）。

```python
FixTarget = tuple[str | None, str | None, str | None]
# cell:  ("2026-11-14", "S01", "09:00-09:30")   （現行と同じ・後方互換）
# slot:  ("2026-11-14", None,   "09:00-09:30")
# staff: ("2026-11-14", "S01",  None) または (None, "S01", None)
```

- `set_fix_target(target: FixTarget | None)` の型ヒントと docstring を更新。ロジック（set/take で消費）は無変更。
- 消費者は `tab_shift._apply_fix_target`（`tab_shift.py:364`）の 1 箇所のみ（grep 済み）。旧 3 要素フルタプルはそのまま動く。

### 4.2 「🛠 この違反を修正する」セクションの表示仕様（`components.py`）

新規 pure 関数と再構成した描画:

```python
def classify_violation(v: Violation) -> str:            # "cell" | "slot" | "staff" | "guide"
def violation_targets(violations, *, limit=20) -> ...   # 現行どおり cell レベルのみ（互換維持）
def partial_violation_targets(violations, *, limit=10)  # slot / staff レベルの (label, target, level)
def unfixable_violations(violations) -> list[Violation] # guide レベルのみに再定義
```

描画順と出し分け:

1. **cell ボタン**: 現行どおり `修正 2026-11-14 S01 09:00-09:30`。2 列グリッド、最大 8 個表示＋「ほか N 件」。
2. **slot ボタン**（新規）: `人員を追加 2026-11-14 09:00-09:30`。
   押すと `set_fix_target((iso, None, slot_label))` → シフト表タブ遷移。
3. **staff ボタン**（新規）: 職員名は `state.staff_name_map()` で `S01（田中）` 表記。
   - day あり: `勤務を調整 2026-11-14 S01（田中）` → `set_fix_target((iso, sid, None))`
   - day なし: `勤務を調整 S01（田中）` → `set_fix_target((None, sid, None))`
4. **guide 違反のみが残った場合**（= 修正ボタンが 1 つも出ない場合）:
   汎用警告は廃止し、**コード別ガイド**（§4.5）を箇条書きで出す。
   `violation_labels()` に存在しない未知コードだけが残ったときは現行文言をフォールバックとして使用。
5. **「ℹ️ セルでは直せない違反」expander**: guide レベルのみを対象に変更し、
   タイトルを「ℹ️ 個別セルで直せない違反（N 件）」に更新。中身の表は「直し方」列付き（§4.6）。

表示上限: cell=20 件（現行）、partial=10 件、各セクション表示 8 個＋残数キャプション（現行パターン踏襲）。
並び順は入力違反の順（`check_violations` が BLOCKER 優先ソート済み）を維持。

### 4.3 シフト表タブの受け口（`tab_shift.py` `_apply_fix_target`）

現行は `(iso_day, staff_id, slot_label)` を unpack し `date.fromisoformat(iso_day)` で日付切替。
部分指定対応のため次のように分岐する:

| target の形 | 動作 |
|---|---|
| day=None, staff≠None | 日付選択は **変更しない**。`st.session_state["_fix_staff_id"] = staff_id` を設定し案内を出す |
| day≠None, staff=None, slot≠None | `active_day` を切り替え、`_fix_slot_label` を設定。**候補職員を表示**（下記） |
| day≠None, staff≠None（slot=None） | `active_day` 切り替え＋ `_fix_staff_id` 設定。`detail` は持たないので対象時間帯は案内しない |
| 3 つすべて | 現行どおり |
| day≠None だが期間外 | 現行どおり警告＋ `_fix_slot_label` / `_fix_staff_id` を両方 pop |

- **候補職員表示**（slot 指定時）: 新規 helper `components.off_staff_for_slot(result, day, slot_label, staff)` が
  その日・その時間帯で `CellState.OFF` の職員ID一覧を返す。案内文に
  「オフの職員候補: S02（田中）, S05（山田）…（最大 5 名・計 N 名）」を表示する。
- **職員行ハイライト**（staff 指定時）: `_render_editor`（`tab_shift.py:131`）の
  「修正セル: **label**」表示を拡張し、`_fix_staff_id` があれば
  「修正対象の職員: **S01（田中）**」行を表示（pandas スタイリングの行強調はモック確認後に判断、§9）。
- `_fix_slot_label` と `_fix_staff_id` は同一ライフサイクルで管理（設定時は片方を pop、期限切れ時は両方 pop）。

### 4.4 集約違反（`detail["slots"]`）の扱い

`aggregate_violations` により `slot=None` になった staff 違反のうち `detail["slots"]` に時間帯ラベルがあるものは、
staff ボタンの help / 案内文に「対象時間帯: 09:00-09:30, 09:30-10:00」を列挙する。
`_fix_slot_label` は単一時間帯のときのみ設定する（複数時は職員行ハイライト＋案内のみ）。

### 4.5 直し方ガイド辞書（案2）

**配置場所は `components.py`（UI 層）**。`gap_analysis` は streamlit 非依存を保つ必要があるため
（`open-hoikuict/tests/services/test_shiftai_vendor.py` が core の streamlint 非-import を検査）、
core に置かない。タブ名は `theme.tab_ref_for()` で動的に差し込むため、辞書値はプレースホルダー形式:

```python
FALLBACK_VIOLATION_FIX_HINTS: dict[str, str] = {
    "SHORTFALL_STAFF": "不足している時間帯に勤務できる職員が足りません。"
        "{shift} でその時間帯のセルをオフの職員を「勤務」に変えるか、"
        "{data} の必要人員（職員数・保育士数）を見直してください。",
    ...
}

def violation_fix_hints() -> dict[str, str]:
    """gap_analysis.VIOLATION_FIX_HINTS があればそれを優先し、無ければ内置の辞書を使う。"""
    # violation_labels() と同じマージパターン

def fix_hint(code: str) -> str:
    """コードの直し方を返す。プレースホルダー {shift}/{data}/{solve}/{export} を tab_ref_for で展開。"""
```

文言ドラフト（全コード。実装時にこの表を辞書へそのまま移す）:

| code | 直し方ガイド（案） |
|---|---|
| `SHORTFALL_STAFF` | 不足している時間帯に勤務できる職員が足りません。{shift} でオフの職員を「勤務」に変えるか、{data} の必要人員を見直してください。 |
| `SHORTFALL_QUALIFIED` | 不足している時間帯の保育士が足りません。{shift} で保育士資格の職員をその時間帯に配置するか、{data} の配置基準を見直してください。 |
| `BREAK_OVERLAP` | 同時刻の休憩が集中しています。{shift} で休憩の始まる時間帯をずらしてください。 |
| `WORK_ON_UNAVAILABLE` | 希望休・不在時間帯に勤務が入っています。{shift} で該当セルを「オフ」にするか、{data} の勤務不可設定を確認してください。 |
| `WORK_ON_CLOSED_DAY` | 休園日に勤務が入っています。{shift} で該当セルを「オフ」にするか、{data} の開園日設定を確認してください。 |
| `OUTSIDE_CONTRACT_HOURS` | 契約時間帯の外で勤務しています。{shift} で始業・終業を契約内に収めるか、{data} の契約時間帯を確認してください。 |
| `DAILY_HOURS_EXCEEDED` | この職員の 1 日の勤務が契約上限を超えています。{shift} で勤務時間を短くするか、{data} の契約時間を確認してください。 |
| `OVERTIME_NOT_ALLOWED` | 時間外労働の契約がないため 8 時間を超えられません。{shift} で勤務を 8 時間以内に収めるか、{data} の 36 協定設定を確認してください。 |
| `OVERTIME_REQUIRES_36AGREEMENT` | 時間外労働として 36 協定の締結と割増賃金の確認が必要です。{shift} で勤務を短縮するか、{data} の職員設定を確認してください。 |
| `BREAK_INSUFFICIENT` | 休憩が規定時間に足りません。{shift} で休憩セルを追加・延長してください。 |
| `BREAK_FRAGMENTED` | 休憩が分断されています。{shift} で連続した休憩になるようセルを調整してください。 |
| `SPLIT_SHIFT` | 勤務が分割されています。園の運用で分割勤務を許可しない場合は、{shift} で勤務ブロックを 1 つにまとめてください。 |
| `CONSECUTIVE_DAYS` | 連続勤務日数が上限を超えています。{shift} で連続区間の間に休みを入れてください。 |
| `WEEKLY_DAYS_EXCEEDED` | 週の勤務日数が上限を超えています。{shift} で該当週の休みを増やしてください。 |
| `REST_HOURS_SHORT` | 勤務間の休息が 11 時間未満です。{shift} で翌日の始業を遅らせるか前日の終業を早めてください。 |
| `WEEKLY_HOURS_EXCEEDED` | この職員の週勤務が 44 時間を超えています。{shift} で該当週の勤務を減らしてください。 |
| `MONTHLY_HOURS_EXCEEDED` | この職員の勤務時間が契約上限を超えています。{shift} で休みを増やすか、{data} の職員 CSV（契約時間）を確認してください。 |
| `MONTHLY_HOURS_SHORT` | この職員の勤務時間が契約下限に届いていません。{shift} で勤務を増やすか、{data} の契約時間を確認してください。 |
| `VARIABLE_HOURS_EXCEEDED` | 変形労働時間制の総枠を超えています。36 協定に基づく割増賃金が必要です。{shift} で総勤務時間を減らすか、{data} の 36 協定設定を確認してください。 |
| `VARIABLE_HOURS_EXCEEDED_NO_36` | 変形労働時間制の総枠を超えていますが 36 協定が未締結のため違法です。{shift} で総勤務時間を減らすか、{data} で 36 協定を設定してください。 |
| `AVOID_EARLY_CONFLICT` | 早朝保育を希望しない職員が配置されています。{shift} で早朝のセルを「オフ」にするか、{data} の希望設定を確認してください。 |
| `AVOID_LATE_CONFLICT` | 延長保育を希望しない職員が配置されています。{shift} で延長のセルを「オフ」にするか、{data} の希望設定を確認してください。 |
| `HOURS_IMBALANCE` | 職員間の勤務時間の偏りが大きいです。1 つのセルでは直せません。{solve} で公平さの重みを上げて再計算するか、全体の勤務配分を調整してください。 |
| `CHECK_UNAVAILABLE` | 違反検査が完了していません。{solve} で再計算するか、時間制限を延ばして再実行してください。 |
| `NOT_PLACED_ROLE` | 配置対象外の職種です（調理員・栄養教諭など）。シフト・労働時間の判定対象外のため対応は不要です。 |

### 4.6 違反一覧・DataFrame への反映

- `violation_dataframe`（`components.py:798`）に **「直し方」列を追加**（8 列目）。
  値は `fix_hint(code)` の **最初の 1 文**（「。」で分割、未知コードは空文字）。
  呼び出し箇所は components 内 2 箇所のみ（`render_violations` / unfixable expander）で、外部利用なし（grep 済み）。
- 「—」の意味説明キャプション（`components.py:914`）は guide レベル専用の文面に更新:
  「この表の違反は特定のセルでは直せません。『直し方』の手順に従ってください。」

---

## 5. 変更ファイル一覧（NuShift 上流）

| # | ファイル | 種別 | 内容 |
|---|---|---|---|
| 1 | `src/shiftai/ui/components.py` | 改修 | `classify_violation`・`partial_violation_targets`・`FALLBACK_VIOLATION_FIX_HINTS`・`violation_fix_hints`・`fix_hint`・`off_staff_for_slot` 追加、`render_violation_actions` 再構成、`unfixable_violations` 再定義、`violation_dataframe` 列追加 |
| 2 | `src/shiftai/ui/state.py` | 改修 | `set_fix_target` / `take_fix_target` の型ヒント＋docstring（`FixTarget` 型エイリアス） |
| 3 | `src/shiftai/ui/tab_shift.py` | 改修 | `_apply_fix_target` 部分指定分岐・候補職員案内・`_fix_staff_id` 管理・`_render_editor` 表示拡張 |
| 4 | `src/shiftai/ui/tab_export.py` | 改修 | 修正フロー説明文（L338/405/486 付近）を新しい導線に整合 |
| 5 | `src/shiftai/ui/tab_solve.py` | 改修 | `render_violation_actions` 呼び出し周辺のキャプション整合（呼び出し自体は無変更） |
| 6 | `tests/test_ui_components.py` ほか | テスト | §7 参照 |
| 7 | `open-hoikuict/vendor/nushift/shiftai/**`・`VENDOR.json`・`README.md` | 同期 | `scripts/sync_nushift_vendor.py` が自動生成（手編集禁止） |

`domain.py`・`gap_analysis.py`・`solver.py` は **変更しない**。

---

## 6. 実装ステップ

> AGENTS.md の手順に従い、**モック試用による仕様合意を経てから** 実装に入る。
> 本計画書の §9 未確定事項への回答を確認した後、ステップ M → 1…6 の順で進める。

| Step | 内容 | 完了条件 |
|---|---|---|
| **M** | モック作成・試用（架空データで違反 4 種類を出し、ボタン/ガイドの見え方を確認できる状態） | ユーザーがモックで認識合わせし合意 |
| 1 | `components.py`: 分類関数＋ターゲット関数＋ガイド辞書＋`render_violation_actions` 再構成＋DataFrame 列（pure 部分を先行） | 既存 unit テストが変わらず通る。`python -c "from shiftai.ui import components"` 成功 |
| 2 | `state.py`: `FixTarget` 型＋docstring | `tests/test_ui_state.py` green |
| 3 | `tab_shift.py`: `_apply_fix_target` 分岐＋候補職員＋`_fix_staff_id` | 手動起動で 3 種類のターゲット遷移が動く |
| 4 | `tab_export.py` / `tab_solve.py` 文言整合 | 目視確認 |
| 5 | 上流テスト一式＋ruff（§7 の 1〜5） | すべて green |
| 6 | vendor 同期: `python scripts/sync_nushift_vendor.py`（open-hoikuict 側。バックアップ/ロールバック内蔵）→ `--verify` で確認 → open-hoikuict テスト（§7 の 6〜8） | manifest ハッシュ一致・全テスト green |

- **vendor 同期の注意**: sync は upstream ワーキングツリー全体をコピーする。現時点で対象 7 ファイルは
  upstream == vendor（ハッシュ一致を確認済み）だが、upstream に **未コミット変更** がある
  （`solver.py`・`config.py`・`__main__.py`・`ui/tab_solve.py` 等）。
  これらは既に vendor 反映済みのため影響しないが、**コミットはユーザーの指示があるまで行わない**
  （AGENTS.md 規定。source_commit は e3f673e のまま更新される）。
- 配備（本番反映）はユーザーの依頼に従って別途行う。

---

## 7. テスト計画（リグレッション防止）

### 7.1 NuShift 上流（`E:\nushift\nushift\tests`、AppTest ベース）

既存テスト資産: `test_ui_tabs.py` に `_run_tabs` / `solved_payload` / `_warning_violation` ヘルパー、
`test_ui_components.py` に `AppTest` 実行基盤がある。これらを再利用する。

| # | ファイル | テスト | 検証内容 |
|---|---|---|---|
| T1 | `test_ui_components.py` | `test_classify_violationは座標の揃い方で4段階に分ける` | §3 の分類表をパラメータ化して全 code を検証。**表とコードの乖離を防ぐ回帰網** |
| T2 | 同上 | `test_全違反コードに直し方ガイドがある` | `violation_labels()` の全 key に `fix_hint()` が非空文字を返すことを検証。**将来コード追加時にガイド漏れを検知** |
| T3 | 同上 | `test_fix_hintはプレースホルダーをタブ名に展開する` | `{shift}` などが残らず展開されること（シンプル/上級両モード） |
| T4 | 同上 | `test_render_violation_actionsは不足違反に部分指定ボタンを出す` | `SHORTFALL_STAFF` のみ → slot ボタン（ラベルに日付＋時間帯）が表示され、汎用警告が出ない |
| T5 | 同上 | `test_render_violation_actionsは職員単位の違反に調整ボタンを出す` | `MONTHLY_HOURS_EXCEEDED` のみ → staff ボタン表示、クリックで `KEY_FIX_TARGET == (None, "S01", None)` |
| T6 | 同上 | `test_render_violation_actionsは座標なし違反でガイドを出す` | `HOURS_IMBALANCE` のみ → 警告でなくガイド文（「再計算」等の語）が表示、例外なし |
| T7 | 同上 | `test_render_violation_actionsはNone混在でも落ちない` | (day, slot, staff) の None 組み合わせ 8 通り × 代表 code をパラメータ化。**例外ゼロ＝本件の本質的回帰網** |
| T8 | 同上 | `test_violation_dataframeは直し方列を持つ` | 「直し方」列存在・未知コードは空文字・既存 7 列の値が不変 |
| T9 | 同上 | `test_off_staff_for_slotは対象時間帯がオフの職員を返す` | 勤務/休憩/オフ混在の result でオフ職員のみ返る |
| T10 | `test_ui_tabs.py` | `test_部分指定ボタンで対象を指定できる` | export タブで slot ボタンクリック → `KEY_FIX_TARGET == (iso, None, slot)`、例外なし |
| T11 | 同上 | `test_シフト表は部分指定の修正対象を受け取る` | tab_shift 実行時に target=(iso, None, slot) → `active_day` 切替・`_fix_slot_label` 設定・案内に候補職員が含まれる。target=(None, "S01", None) → `active_day` 不変・`_fix_staff_id` 設定 |
| T12 | 同上 | `test_期間外の日付は警告してクリアする`（拡張） | 既存の期間外警告テストに `_fix_staff_id` クリア検証を追加 |
| T13 | `test_ui_state.py` | `test_set_fix_targetは部分タプルを受け付ける` | None を含むタプルの set/take が往復する |
| T14 | `test_regression_ui.py` | `test_修正導線は座標が欠けても必ず何かを出す` | 全 code について `render_violation_actions` が「ボタン or ガイド」のいずれかを必ず出すこと。**「出口がなくなる」状態の再発防止** |
| T15 | 既存 3 本 | `test_確認未達でも修正ボタンを出す` / `test_修正ボタンで対象を指定できる` / `test_修正対象は1回だけ消費される` | **無変更で green**（cell レベル互換の証明） |

実行コマンド（上流ルートで）:

```
python -m pytest tests/test_ui_components.py tests/test_ui_tabs.py tests/test_ui_state.py tests/test_regression_ui.py -q
python -m ruff check src/shiftai/ui tests
```

### 7.2 open-hoikuict（vendor 側の整合）

| # | テスト | 検証内容 |
|---|---|---|
| T16 | `tests/test_vendor_manifest.py` | VENDOR.json のハッシュが vendor 実ファイルと一致（sync 直後に必須） |
| T17 | `tests/test_vendor_sync.py` | upstream（E:\nushift\nushift）と vendor の差分ゼロ |
| T18 | `tests/services/test_shiftai_vendor.py` | core が streamlit なしで import 可能（案2 ガイドを UI 層に置いたことでこの保証を崩さない） |
| T19 | `tests/test_regression_shift.py` | ソルバー系回帰が無影響であること |

実行コマンド（`C:\Users\user\openict\open-hoikuict` で）:

```
python scripts/sync_nushift_vendor.py --verify
python scripts/sync_nushift_vendor.py
python -m pytest tests/test_vendor_manifest.py tests/test_vendor_sync.py tests/services/test_shiftai_vendor.py tests/test_regression_shift.py -q
```

---

## 8. リスクと互換性

| リスク | 対策 |
|---|---|
| `KEY_FIX_TARGET` に None が入る形への変更 | 消費者は `tab_shift._apply_fix_target` 1 箇所のみ（grep 済み）。フルタプルは旧来どおり有効。T13〜T15 で検証 |
| 集約違反のボタン数増加（`detail["slots"]` により staff レベルが多数化） | partial 上限 10 件＋表示 8 個＋残数キャプション。BLOCKER 優先順は `check_violations` の既存ソートに従う |
| シンプルモードでタブ名が変わる（shift→タブ2、requirements/solve が同タブ内） | ガイド文言のタブ名は `theme.tab_ref_for()` 動的展開（T3 で両モード検証）。既存 `SIMPLE_TAB_ALIASES` をそのまま利用 |
| `violation_dataframe` の列追加によるレイアウト崩れ | 呼び出しは components 内 2 箇所のみ・`column_config` 未指定。値は 1 文に短縮。モック（Step M）で幅を確認 |
| core の streamlit 非依存を壊す | ガイド辞書は `components.py`（UI 層）にのみ置く。T18 で継続検証 |
| upstream の未コミット変更との混在 | 対象ファイルは upstream == vendor 一致済み。sync は差分のある他ファイルもそのまま運ぶが現状一致済み。コミットはユーザー指示時のみ |
| 「修正ボタンが押せるのに直しても違反が消えない」誤解 | ガイド文は「シフト表を直す/データを見直す」を明示。slot ボタンは「人員を追加」と行為を明記 |

---

## 9. 未確定事項（モック確認事項）

1. **職員行ハイライトの程度**: 案内テキストだけか、マトリクス表の該当行の背景色強調まで行うか
   （行強調は `style_shift_grid` へのパッチが必要。モックで見た目を確認して決める）。
2. **slot ボタンの候補職員**: 候補が多い場合の表示（5 名まで＋「ほか N 名」でよいか）。
   また、勤務不可・希望休の職員を候補から除外するか。
3. **部分指定ボタンの上限**: partial 10 件／表示 8 個の妥当性。
4. **「直し方」列の有無**: 違反一覧の表に列を足すと幅がきつくないか（列なしでガイド文のみの案も可）。
5. **ガイド文言**: §4.5 ドラフトの表現（特に `SPLIT_SHIFT`＝園の運用次第で許可、`NOT_PLACED_ROLE`＝対応不要、の部分）。

---

## 10. 完了条件

- [ ] Step M のモックでユーザーが仕様に合意している
- [ ] §7.1 T1〜T15 が green（既存テスト T15 含む無変更 green）
- [ ] `ruff check` クリア
- [ ] vendor 同期完了＋§7.2 T16〜T19 が green
- [ ] 架空データでの手動確認: (a) セル違反ボタン（現行どおり） (b) 不足違反→候補職員付き遷移 (c) 月間時間超過→職員行遷移 (d) 偏り→ガイド文、の 4 経路が動く
- [ ] 変更書・完了報告を `nushift/docs/` に残す（配備はユーザー指示時）
