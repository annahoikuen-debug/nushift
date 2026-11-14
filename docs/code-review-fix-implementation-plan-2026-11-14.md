# コードレビュー指摘事項 修正詳細実装計画書

- **作成日**: 2026-11-14
- **対象プロジェクト**:
  - `NuShift` (`E:\nushift\nushift`) — 最適化エンジンおよびシフトUI
  - `open-hoikuict` (`C:\Users\user\openict\open-hoikuict`) — 保育ICT Webアプリ本体およびNuShift vendor
- **ステータス**: 計画策定完了・実装前

---

## 1. 概要・改修目的

先般実施したコードレビューにおいて検出された、以下の不整合・潜在的欠陥・UX上のエッジケースを解消し、両リポジトリの品質と信頼性を向上させる。

1. **NuShift**:
   - `tab_shift.py`: 重複した `st.rerun()` の削除
   - `monthly.py`: Stage 1 への `fixed_assignments` 反映、月初連勤制約（`prior_streak`）の過剰制約解消、パート職員の個別勤務日数目標の考慮
2. **open-hoikuict**:
   - `zengin_service.py`: `inactive_status` の severity を `warning` から `danger` に統一
   - `templates/base.html`: フォーム未保存離脱防止ガードの同一ページハッシュ判定＆二重確認防止
   - `templates/document_exports/dashboard.html`: プレビュー描画の XSS 防御 (`escapeHtml`)
   - `routers/document_exports.py`: 帳票プレビューへのサンプルデータ明示注記追加
3. **ベンダー同期**:
   - NuShift 改修完了後、`sync_nushift_vendor.py` で `vendor/nushift` を同期し、`check_vendor_integrity.py` で整合性を検証。

---

## 2. 実装詳細仕様

### 2.1 【NuShift】`src/shiftai/ui/tab_shift.py`
347 行目の重複呼び出しを削除する。

### 2.2 【NuShift】`src/shiftai/monthly.py`
1. **Stage 1 固定反映**:
   `fixed_assignments` から日ごとの固定状態を抽出し、有給・全日OFFなら `duty_vars[(sid, d)] == 0`、勤務固定なら `duty_vars[(sid, d)] == 1` を課す。
2. **月初連勤制約の適正化**:
   前月末の `prior_streak` 日分の固定値 `1` を先頭に付加した拡張系列に対してスライディングウィンドウ制約（幅 `max_streak + 1`）を適用する。
3. **短時間・パート職員の個別目標考慮**:
   `contract.weekly_work_days` 等の個別契約値がある場合は個別目標日数を算出して `work_diff` を評価する。

### 2.3 【open-hoikuict】`zengin_service.py`
`scan_zengin_preflight` における `inactive_status` の `severity` を `"danger"` に統一。

### 2.4 【open-hoikuict】`templates/base.html`
`origin`, `pathname`, `search` を比較し、「同一ページ内アンカー」のみを除外し、OK選択時は `isFormDirty = false;` をセット。

### 2.5 【open-hoikuict】`templates/document_exports/dashboard.html` & `routers/document_exports.py`
`dashboard.html` に安全な HTML エスケープ関数 `escapeHtml()` を実装し、各値のレンダリングに適用。
`routers/document_exports.py` のレスポンスに `is_sample: True` および注意注記文を含める。

---

## 3. 完了基準 (Definition of Done)
1. NuShift および open-hoikuict の全該当ファイルが改修されていること。
2. `check_vendor_integrity.py` が成功すること（43ファイル整合）。
3. 双方のリポジトリで関連テストが 100% グリーン（PASS）であること。
