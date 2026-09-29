"""サンプルデータ（``shiftai.sample_data``）のテスト。

園児 34 名 / 職員 28 名という規模が維持されていること、**同じ seed で必ず同じ出力**に
なること、書き出した CSV がそのまま読み戻せることを保証する。
"""

from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path

import pytest

from shiftai import sample_data
from shiftai.data_loader import (
    CHILDREN_COLUMNS,
    PREFERENCE_COLUMNS,
    STAFF_COLUMNS,
    load_bundle,
    read_table,
)
from shiftai.domain import AgeClass, EmploymentType, Role, StaffMember

pytestmark = pytest.mark.timeout(300)

DAY = date(2026, 9, 28)
WEEK = [date(2026, 9, 28) + timedelta(days=i) for i in range(7)]


# ---------------------------------------------------------------------------
# 規模
# ---------------------------------------------------------------------------


def test_園児34名_職員28名():
    """サンプルの規模が UI の初期表示で想定している 34 名 / 28 名であること。"""
    assert sample_data.TOTAL_CHILDREN == 34
    assert sample_data.TOTAL_STAFF == 28
    assert sum(count for _, count in sample_data.AGE_PLAN) == 34
    assert sum(spec[3] for spec in sample_data.STAFF_BLUEPRINT) == 28


def test_make_datasetの人数():
    """1 日ぶんの ``make_dataset`` が 34 名・28 名を返すこと。"""
    children, staff, prefs = sample_data.make_dataset([DAY], seed=42)
    assert len(children) == sample_data.TOTAL_CHILDREN
    assert len(staff) == sample_data.TOTAL_STAFF
    assert len(prefs) == sample_data.TOTAL_STAFF
    assert set(prefs) == {m.staff_id for m in staff}


def test_1週間分は曜日ごとの園児():
    """7 日ぶんの園児データが日ごとに生成されること（日曜は在園児 0 名）。"""
    children, _, _ = sample_data.make_dataset(WEEK, seed=42)
    days = sorted({c.day for c in children})
    assert days == [d for d in WEEK if d.weekday() < 6]
    assert len(children) > sample_data.TOTAL_CHILDREN


def test_年齢構成():
    """年齢クラスの内訳が AGE_PLAN と一致すること。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    counts: dict[AgeClass, int] = {}
    for child in children:
        counts[child.age_class] = counts.get(child.age_class, 0) + 1
    for age_class, expected in sample_data.AGE_PLAN:
        assert counts.get(age_class) == expected


def test_職員の内訳():
    """職員が blueprints どおりの資格内訳で構成されていること。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    by_role: dict[Role, int] = {}
    for member in staff:
        by_role[member.primary_role] = by_role.get(member.primary_role, 0) + 1
    expected: dict[Role, int] = {}
    for _, role, _, count in sample_data.STAFF_BLUEPRINT:
        expected[role] = expected.get(role, 0) + count
    assert by_role == expected
    assert sum(1 for m in staff if m.has_role(Role.HOIKUSHI)) == 18


def test_職員IDが一意():
    """職員IDが重複しないこと（シフト表のキーになるため）。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    ids = [m.staff_id for m in staff]
    assert len(set(ids)) == len(ids)


def test_園児IDが一意():
    """園児IDが重複しないこと。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    ids = [c.child_id for c in children]
    assert len(set(ids)) == len(ids)


def test_職員の契約が正():
    """全職員が正の契約時間を持ち、終業時刻が朝より後であること。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    for member in staff:
        assert member.contract.weekly_hours > 0
        assert member.contract.daily_hours > 0
        assert member.contract.latest_end > member.contract.earliest_start
        assert member.contract.max_monthly_hours >= member.contract.min_monthly_hours
        assert member.roles


def test_正職員とパートの混在():
    """雇用形態が混在していること（人件費係数が効く前提）。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    kinds = {m.contract.employment_type for m in staff}
    assert EmploymentType.SEI in kinds
    assert EmploymentType.PART in kinds


# ---------------------------------------------------------------------------
# 決定論性
# ---------------------------------------------------------------------------


def test_同じseedで同じ園児():
    """同じ ``seed`` なら園児データが完全に一致すること。"""
    first, _, _ = sample_data.make_dataset(WEEK, seed=7)
    second, _, _ = sample_data.make_dataset(WEEK, seed=7)
    assert first == second


def test_同じseedで同じ職員():
    """同じ ``seed`` なら職員データが完全に一致すること。"""
    _, first, _ = sample_data.make_dataset(WEEK, seed=7)
    _, second, _ = sample_data.make_dataset(WEEK, seed=7)
    assert first == second


def test_同じseedで同じ希望休():
    """同じ ``seed`` なら希望休データも一致すること。"""
    _, _, first = sample_data.make_dataset(WEEK, seed=7)
    _, _, second = sample_data.make_dataset(WEEK, seed=7)
    assert first.keys() == second.keys()
    for key in first:
        assert first[key].unavailable == second[key].unavailable
        assert first[key].preferred_days == second[key].preferred_days
        assert first[key].avoid_early == second[key].avoid_early


def test_異なるseedで異なる職員():
    """``seed`` を変えると職員（通勤区間等）が変わること。"""
    _, first, _ = sample_data.make_dataset(WEEK, seed=1)
    _, second, _ = sample_data.make_dataset(WEEK, seed=2)
    assert first != second


def test_seed無しの既定():
    """``days=None`` でも既定の表示期間ぶんが生成されること。"""
    children, staff, prefs = sample_data.make_dataset()
    assert children and staff and prefs
    assert len({c.day for c in children}) == 6
    assert len(staff) == 28


# ---------------------------------------------------------------------------
# 登降園の妥当性
# ---------------------------------------------------------------------------


def test_在園時間が正():
    """欠席者を除き、降園は登園より後であること。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    for child in children:
        if child.absent:
            assert child.stay_minutes == 0
        else:
            assert child.depart > child.arrive


def test_在園時間が開所時間内():
    """全園児の在園時間が園の開所時間（7:15〜19:30）に収まること。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    for child in children:
        if child.absent:
            continue
        assert child.arrive >= time(7, 15)
        assert child.depart <= time(19, 30)


def test_短時間保育児がいる():
    """短時間保育児がサンプルに含まれ、保育標準時間内に収まること。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    shorts = [c for c in children if c.is_short_time]
    assert shorts
    for child in shorts:
        assert child.depart <= time(17, 15)


def test_希望休は契約日数を超えない():
    """希望休の件数が契約の最大出勤日数を超えないこと（破綻防止）。"""
    _, staff, prefs = sample_data.make_dataset(WEEK, seed=42)
    for member in staff:
        off = sum(1 for u in prefs[member.staff_id].unavailable if u.start == time(0, 0))
        assert off <= member.contract.max_weekly_days


# ---------------------------------------------------------------------------
# DataFrame 化・ラウンドトリップ
# ---------------------------------------------------------------------------


def test_sample_dataframesの列():
    """3 枚の DataFrame が UI と同じ日本語スキーマであること。"""
    children, staff, prefs = sample_data.make_dataset([DAY], seed=42)
    frames = sample_data.sample_dataframes(children=children, staff=staff, preferences=prefs)
    assert set(frames) == {"children", "staff", "preferences"}
    assert list(frames["children"].columns) == CHILDREN_COLUMNS
    assert list(frames["staff"].columns) == STAFF_COLUMNS
    assert list(frames["preferences"].columns) == PREFERENCE_COLUMNS
    assert len(frames["children"]) == 34
    assert len(frames["staff"]) == 28


def test_load_bundleへのラウンドトリップ():
    """サンプルを DataFrame にして読み戻しても ``ok=True`` で同数が得られること。"""
    children, staff, prefs = sample_data.make_dataset([DAY], seed=42)
    frames = sample_data.sample_dataframes(children=children, staff=staff, preferences=prefs)
    result = load_bundle(frames["children"], frames["staff"], frames["preferences"])
    assert result.ok is True
    assert result.errors == []
    assert len(result.children) == 34
    assert len(result.staff) == 28
    assert len(result.preferences) == 28
    assert {c.child_id for c in result.children} == {c.child_id for c in children}
    assert {m.staff_id for m in result.staff} == {m.staff_id for m in staff}


def test_往復で在園時間が一致():
    """往復後も園児の登降園時刻が保存されること。"""
    children, _, _ = sample_data.make_dataset([DAY], seed=42)
    frames = sample_data.sample_dataframes(children=children)
    reloaded = load_bundle(frames["children"]).children
    by_id = {c.child_id: c for c in children if not c.absent}
    for child in reloaded:
        if child.absent:
            continue
        original = by_id[child.child_id]
        assert child.arrive == original.arrive
        assert child.depart == original.depart
        assert child.age_class is original.age_class


def test_往復で職員契約が一致():
    """往復後も職員の契約時間が保存されること。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    frames = sample_data.sample_dataframes(staff=staff)
    reloaded = load_bundle(staff_df=frames["staff"]).staff
    by_id = {m.staff_id: m for m in staff}
    for member in reloaded:
        original = by_id[member.staff_id]
        assert member.contract.weekly_hours == pytest.approx(original.contract.weekly_hours)
        assert member.contract.daily_hours == pytest.approx(original.contract.daily_hours)
        assert member.contract.earliest_start == original.contract.earliest_start
        assert member.contract.latest_end == original.contract.latest_end
        assert member.primary_role is original.primary_role


# ---------------------------------------------------------------------------
# ファイル書き出し
# ---------------------------------------------------------------------------


def test_write_sample_files(tmp_path: Path):
    """3 枚の CSV が書き出され、そのまま読み戻せること。"""
    out = sample_data.write_sample_files(tmp_path / "sample", [DAY], seed=42)
    assert set(out) == {"children", "staff", "preferences"}
    for path in out.values():
        assert path.exists()
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    children = read_table(out["children"], "children")
    assert len(children) == 34
    assert list(children.columns) == CHILDREN_COLUMNS
    staff = read_table(out["staff"], "staff")
    assert len(staff) == 28


def test_write_sample_files_既存ディレクトリ(tmp_path: Path):
    """既存のディレクトリにも書けること。"""
    target = tmp_path / "nested" / "sample"
    out = sample_data.write_sample_files(target, [DAY], seed=42)
    assert all(p.exists() for p in out.values())


def test_write_sample_files_再現性(tmp_path: Path):
    """同じ seed で書き出したファイルの内容が一致すること。"""
    first = sample_data.write_sample_files(tmp_path / "a", [DAY], seed=3)
    second = sample_data.write_sample_files(tmp_path / "b", [DAY], seed=3)
    for key in first:
        assert first[key].read_bytes() == second[key].read_bytes()


def test_職員名は日本語():
    """サンプル名が日本語であること（UI 表示の smoke test）。"""
    _, staff, _ = sample_data.make_dataset([DAY], seed=42)
    assert all(any("一" <= ch <= "鿿" for ch in m.name) for m in staff)
    assert all(m.name in sample_data.STAFF_NAMES for m in staff)


def test_make_staffは引数で決まる():
    """``make_staff(seed=...)`` が職員 28 名を返し seed で変わること。"""
    first = sample_data.make_staff(seed=1)
    second = sample_data.make_staff(seed=2)
    assert len(first) == 28
    assert first != second
    assert all(isinstance(m, StaffMember) for m in first)
