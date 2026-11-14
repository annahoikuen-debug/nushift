# 勤務時間3分割禁止・休憩時間45〜120分調整・正社員4〜8時間勤務 柔軟化 詳細実装計画書

- **作成日**: 2026-11-14
- **対象プロジェクト**:
  - `NuShift` (`E:\nushift\nushift`) — ソルバー・ドメイン・UI
  - `open-hoikuict` (`C:\Users\user\openict\open-hoikuict`) — vendor 同期

---

## 1. 要件と設計方針

### 1.1 要件の整理
1. **勤務時間を3分割する方法を基本的に禁止**:
   - 1日の勤務ブロックが3個以上になる「3分割勤務」を基本的に禁止する。
   - 通常の勤務形態（1連続勤務、または途中に1回休憩を挟む2ブロック勤務）は許容し、3分割以上を構造的に排除する。
2. **休憩時間を45〜120分で調整可能とする**:
   - 労働基準法第34条（6時間超45分、8時間超60分）の法定要件を満たした上で、45分〜120分の範囲で設定できるようにする。
3. **正社員（常勤）の勤務時間を4〜8時間の間で調整可能とする**:
   - 正社員（`EmploymentType.SEI`）について、出勤する日の勤務時間を従来の8時間固定から、需要に応じた「4時間〜8時間（半日〜全日）」の柔軟な範囲で割り当てられるようにする。
4. **施設や職員ごとに調整可能とする**:
   - **施設単位（FacilitySettings）**: 園全体の標準ポリシーとして設定。
   - **職員・契約単位（Contract）**: 職員ごとの個別雇用契約に応じた上書き設定を可能にする。
   - **優先順位**: 職員契約 (`Contract`) > 施設設定 (`FacilitySettings`) > 制度プリセット (`StandardRule`) > システム既定 (`config`)。

---

## 2. データ構造の拡張 (`domain.py`, `config.py`)

### 2.1 定数の追加 (`config.py`)
```python
MIN_ALLOWABLE_BREAK_MINUTES = 45     # 労基法最低休憩（6時間超）
MAX_ALLOWABLE_BREAK_MINUTES = 120    # 許容最大休憩（2時間）
DEFAULT_MAX_WORK_BLOCKS = 2          # 1日最大勤務ブロック数（3分割禁止のため既定2）
DEFAULT_SEI_MIN_DAILY_HOURS = 4.0    # 正社員最低勤務時間（4時間）
DEFAULT_SEI_MAX_DAILY_HOURS = 8.0    # 正社員最高勤務時間（8時間）
```

### 2.2 `FacilitySettings` の拡張 (`domain.py`)
- `max_work_blocks_per_day: int = 2`
- `break_minutes: int = 60`
- `sei_min_daily_hours: float = 4.0`
- `sei_max_daily_hours: float = 8.0`

### 2.3 `Contract` の拡張 (`domain.py`)
- `min_daily_hours: float | None = None`
- `break_minutes: int | None = None`
- `max_work_blocks_per_day: int | None = None`

---

## 3. 数理最適化ソルバーの定式化 (`solver.py`, `monthly.py`)

1. **3分割禁止制約**:
   勤務開始フラグの合計 $\sum s_i \le \text{max\_blocks}$（既定2）を課し、3ブロック以上を排除。
2. **休憩時間（45〜120分）の個別割当**:
   個別設定に応じた $\text{need\_slots}$ の休憩を出勤日に対して要求。
3. **正社員の4〜8時間勤務調整（ビッグM）**:
   出勤フラグ $yvar$ に対し、$4.0\text{h} \cdot yvar \le \text{勤務時間} \le 8.0\text{h} \cdot yvar$ を課す。

---

## 4. バリデーションの拡張 (`gap_analysis.py`)

1. `SPLIT_SHIFT`: `runs > max_blocks` で判定。
2. `BREAK_INSUFFICIENT`: 設定目標休憩時間（45〜120分）および法定基準に基づき判定。
3. `DAILY_HOURS_SHORT`: 正社員で4時間未満の場合に警告。
