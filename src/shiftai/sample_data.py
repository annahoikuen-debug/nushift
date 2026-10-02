"""現実的な日本式保育/sample データ生成。

すべて ``random.Random(seed)`` を明示的に生成するため、同じ ``seed`` なら
常に同じ出力が得られる（グローバル ``random`` は一切使わない）。
UI の「サンプルデータで試す」ボタンから直接利用する。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from shiftai.config import DEFAULT_RANGE_DAYS, DEFAULT_RANGE_START
from shiftai.data_loader import (
    CHILDREN_COLUMNS,
    PREFERENCE_COLUMNS,
    STAFF_COLUMNS,
)
from shiftai.domain import (
    AgeClass,
    ChildPlan,
    Contract,
    EmploymentType,
    FacilitySettings,
    Role,
    StaffMember,
    StaffPreferences,
    Unavailability,
    daterange,
)

SURNAMES: tuple[str, ...] = (
    "佐藤",
    "鈴木",
    "高橋",
    "田中",
    "伊藤",
    "渡辺",
    "山本",
    "中村",
    "小林",
    "加藤",
    "吉田",
    "山田",
    "松本",
    "井上",
    "木村",
    "林",
    "斎藤",
    "清水",
    "山崎",
    "森",
)

GIVEN_NAMES: tuple[str, ...] = (
    "さくら",
    "あおい",
    "ひなた",
    "そら",
    "ゆず",
    "れん",
    "みなと",
    "あかね",
    "すず",
    "めい",
    "はな",
    "のあ",
    "いつき",
    "しょう",
    "りお",
    "ゆう",
    "あい",
    "はるか",
    "ゆい",
    "きお",
    "まこ",
    "りか",
    "しお",
    "あき",
    "なな",
    "はなこ",
    "ゆな",
    "いずき",
    "りゅう",
    "そう",
    "まひろ",
    "かける",
    "とあ",
    "はると",
    "みお",
    "しおり",
    "たまき",
    "ゆうき",
    "のり",
    "のあな",
)

STAFF_NAMES: tuple[str, ...] = (
    "山田花子",
    "佐藤健太郎",
    "鈴木由美",
    "高橋美咲",
    "伊藤大輔",
    "渡辺直子",
    "山本雅子",
    "中村拓也",
    "小林麻衣",
    "加藤裕太",
    "吉田千尋",
    "山田涼太",
    "松本陽菜",
    "井上智也",
    "木村明日香",
    "林大樹",
    "藤田美月",
    "岡田翔太",
    "三浦七海",
    "浜田大輝",
    "西村あかり",
    "河野健太",
    "清水颯太",
    "斎藤結衣",
    "森本大輝",
    "池田彩乃",
    "橋本拓真",
    "石川陽菜",
    "山下美咲",
    "中村健太",
    "小林結衣",
    "加藤優子",
    "吉田蓮",
    "山本颯",
)

AGE_PLAN: tuple[tuple[AgeClass, int], ...] = (
    (AgeClass.INFANT, 8),
    (AgeClass.AGE_1, 7),
    (AgeClass.AGE_2, 6),
    (AgeClass.AGE_3, 5),
    (AgeClass.AGE_4, 4),
    (AgeClass.AGE_5, 4),
)

TOTAL_CHILDREN = sum(count for _, count in AGE_PLAN)
"""園児 34 名（0歳8/1歳7/2歳6/3歳5/4歳4/5歳4）。"""

STAFF_BLUEPRINT: tuple[tuple[str, Role, EmploymentType, int], ...] = (
    ("正職員 保育士", Role.HOIKUSHI, EmploymentType.SEI, 6),
    ("パート 保育士", Role.HOIKUSHI, EmploymentType.PART, 12),
    ("パート 子育て支援員", Role.SHIENSHIIN, EmploymentType.PART, 6),
    ("正職員 幼稚園教諭", Role.YOUCHUIN, EmploymentType.SEI, 1),
    ("正職員 看護師", Role.KANGSHI, EmploymentType.SEI, 1),
    ("パート 調理員", Role.CHUUBOU, EmploymentType.PART, 1),
    ("正職員 園長", Role.ENJOGAKUIN, EmploymentType.SEI, 1),
)
"""職員の内訳（区分名, 資格, 雇用形態, 人数）。人数はここだけ直せばよい。

34 名・開所 7:15〜19:30 の設定では、保育標準時間に保育士 9 名を Peak 配置し
つつ延長帯まで Pratt するには保育士 18 名（正職 6 + パート 12）が必要。
その上で 1 週間 551 人時の必要人員を受給できる。
"""

TOTAL_STAFF = sum(spec[3] for spec in STAFF_BLUEPRINT)
"""職員 28 名（``STAFF_BLUEPRINT`` の合計）。"""

PART_LATEST_END = time(20, 0)
"""パート職員の最終終業。19:30 閉所の延長帯をカバーするため 20:00 まで許可。"""

MAX_OFF_DAY_RATIO = 0.25
"""1 日あたりの希望休人数の上限（職員数に対する割合）。"""

ABSENT_REASONS: tuple[str, ...] = (
    "体調不良",
    "発熱",
    "家庭事情",
    "通院",
    "下痢",
    "実家滞在",
    "行事参加",
    "体調不良（軽度）",
)

SKILL_POOL: tuple[str, ...] = (
    "乳幼児研修修了",
    "応急処置資格",
    "ピアノ指導可",
    "手話講習受講",
    "食育指導可",
    "担任_0・1歳児",
    "担任_2・3歳児",
    "担任_4・5歳児",
    "保育リーダー研修了",
    "個別計画作成経験",
    "小規模保育運営研修了",
    "保育相談支援研修修了",
    "虐待対応研修修了",
    "アレルギー対応可",
)

LEAVE_REASONS: tuple[str, ...] = (
    "希望休",
    "PTO",
    "通院",
    "育児",
    "夏季休業",
    "役員会",
)


def _arrive_window(day: date) -> tuple[int, int]:
    """曜日ごとの登園時刻の目安（分）。"""
    weekday = day.weekday()
    if weekday == 4:
        return 8 * 60 + 30, 9 * 60 + 30
    if weekday == 3:
        return 8 * 60, 8 * 60 + 45
    if weekday in (1, 2):
        return 8 * 60, 9 * 60
    if weekday == 0:
        return 8 * 60 + 15, 9 * 60 + 15
    if weekday == 5:
        return 8 * 60 + 45, 9 * 60 + 30
    return 9 * 60, 9 * 60 + 30


def make_children(
    days: Sequence[date],
    *,
    seed: int = 42,
    facility: FacilitySettings | None = None,
) -> list[ChildPlan]:
    """園児 34 名 × ``days`` の登降園予定を生成する。"""
    rng = random.Random(seed)
    day_list = sorted(set(days))
    names = list(GIVEN_NAMES)
    rng.shuffle(names)

    roster: list[dict[str, Any]] = []
    index = 0
    serial = 1
    for age_class, count in AGE_PLAN:
        for _ in range(count):
            given = names[index % len(names)]
            index += 1
            surname = SURNAMES[(serial - 1) % len(SURNAMES)]
            roster.append(
                {
                    "child_id": f"C{serial:03d}",
                    "name": f"{surname} {given}",
                    "age_class": age_class,
                    "short": False,
                    "early": False,
                    "late": False,
                }
            )
            serial += 1
    assert len(roster) == TOTAL_CHILDREN, len(roster)

    short_ids = {r["child_id"] for r in rng.sample(roster, 5)}
    late_ids = {
        r["child_id"] for r in rng.sample([r for r in roster if r["child_id"] not in short_ids], 8)
    }
    early_ids = {
        r["child_id"] for r in rng.sample([r for r in roster if r["child_id"] not in late_ids], 5)
    }
    for row in roster:
        row["short"] = row["child_id"] in short_ids
        row["late"] = row["child_id"] in late_ids
        row["early"] = row["child_id"] in early_ids

    plans: list[ChildPlan] = []
    for day in day_list:
        weekday = day.weekday()
        if weekday == 6:
            continue
        attending = [row for row in roster if weekday < 5 or row["short"] or row["late"]]
        if weekday == 5:
            attending = [row for row in attending if row["short"] or row["late"]]
        absent_count = 0 if weekday == 5 else rng.randint(1, 2)
        absent_ids = (
            {r["child_id"] for r in rng.sample(attending, absent_count)} if attending else set()
        )

        for row in attending:
            child_id = row["child_id"]
            absent = child_id in absent_ids
            lo, hi = _arrive_window(day)
            if row["short"]:
                arrive_m = 9 * 60 + rng.randint(0, 15)
                depart_m = 14 * 60 + rng.randint(0, 30)
            else:
                arrive_m = rng.randint(lo, hi)
                span = rng.choice([8 * 60 + 30, 9 * 60, 9 * 60 + 30, 10 * 60])
                depart_m = arrive_m + span
                if row["late"]:
                    depart_m = max(depart_m, 17 * 60 + 30)
                if row["early"]:
                    arrive_m = min(arrive_m, 7 * 60 + 45)
            reason = rng.choice(ABSENT_REASONS) if absent else ""
            plans.append(
                ChildPlan(
                    child_id=child_id,
                    name=row["name"],
                    day=day,
                    age_class=row["age_class"],
                    arrive=time(arrive_m // 60, arrive_m % 60) if not absent else time(0, 0),
                    depart=time(depart_m // 60, depart_m % 60) if not absent else time(0, 0),
                    is_short_time=bool(row["short"]),
                    absent=absent,
                    absent_reason=reason,
                    uses_early_care=bool(row["early"]) and not absent,
                    uses_late_care=bool(row["late"]) and not absent,
                    notes="",
                )
            )
    return plans


def _contract_for(label: str, rng: random.Random) -> Contract:
    """``STAFF_BLUEPRINT`` の区分名から ``Contract`` を組み立てる。"""
    if label == "正職員 保育士":
        return Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            min_monthly_hours=150.0,
            max_monthly_hours=185.0,
            max_weekly_days=5,
            max_consecutive_days=5,
        )
    if label == "パート 保育士":
        weekly = float(rng.choice([25, 26, 28, 30]))
        daily = min(float(rng.choice([5, 5.5, 6])), weekly / 5.0)
        return Contract(
            weekly_hours=weekly,
            daily_hours=daily,
            employment_type=EmploymentType.PART,
            min_monthly_hours=50.0,
            max_monthly_hours=110.0,
            max_weekly_days=5,
            max_consecutive_days=5,
            earliest_start=time(7, 0),
            latest_end=PART_LATEST_END,
        )
    if label == "パート 子育て支援員":
        weekly = float(rng.choice([20, 22, 24, 25, 28]))
        daily = min(float(rng.choice([4, 4.5, 5, 5.5])), weekly / 5.0)
        return Contract(
            weekly_hours=weekly,
            daily_hours=daily,
            employment_type=EmploymentType.PART,
            min_monthly_hours=40.0,
            max_monthly_hours=100.0,
            max_weekly_days=5,
            max_consecutive_days=5,
            earliest_start=time(7, 0),
            latest_end=PART_LATEST_END,
        )
    if label == "正職員 幼稚園教諭":
        return Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            min_monthly_hours=150.0,
            max_monthly_hours=185.0,
            max_weekly_days=5,
            max_consecutive_days=5,
        )
    if label == "正職員 看護師":
        return Contract(
            weekly_hours=35.0,
            daily_hours=7.0,
            employment_type=EmploymentType.SEI,
            min_monthly_hours=130.0,
            max_monthly_hours=165.0,
            max_weekly_days=5,
            max_consecutive_days=5,
        )
    if label == "パート 調理員":
        return Contract(
            weekly_hours=30.0,
            daily_hours=6.0,
            employment_type=EmploymentType.PART,
            min_monthly_hours=60.0,
            max_monthly_hours=110.0,
            max_weekly_days=5,
            max_consecutive_days=4,
            earliest_start=time(8, 0),
            latest_end=time(16, 0),
        )
    if label == "正職員 園長":
        return Contract(
            weekly_hours=40.0,
            daily_hours=8.0,
            employment_type=EmploymentType.SEI,
            min_monthly_hours=150.0,
            max_monthly_hours=185.0,
            max_weekly_days=5,
            max_consecutive_days=5,
        )
    raise ValueError(f"未登録の職員区分です: {label!r}")


def make_staff(*, seed: int = 42) -> list[StaffMember]:
    """職員 24 名を ``STAFF_BLUEPRINT`` の内訳どおりに生成する。

    園児 34 名・7:15〜19:30 開所 の 1 週間（2026-09-28〜2026-10-04）を
    ``standards.build_requirements`` + ``standards.total_required_hours`` で
    実測した結果（seed=42）:

    ==========================  ==========  ============  =================
    基準                          必要人時     総契約時間     必要人時/契約時間
    ==========================  ==========  ============  =================
    福岡市                        551.0 h     737.0 h        0.748
    全国基準（厚労省）            520.5 h     737.0 h        0.706
    ==========================  ==========  ============  =================

    総契約時間 737.0 h の内訳は 正職保育士 240.0 / パート保育士 210.0 /
    パート支援員 142.0 / 幼稚園教諭 40.0 / 看護師 35.0 / 調理員 30.0 / 園長 40.0。
    最も厳しい福岡市基準でも比は 0.748 で、要求上限 1.15 を十分下回るため
    園児側の延長・早朝保育利用者は削減していない（延長 11 名 / 早朝 6 名）。
    園長（``Role.ENJOGAKUIN``）は配置基準の人数に計上しない前提で、
    看護師・調理員は延長保育の緩和措置（保育士1名＋他資格者）只能用として扱う。
    """
    rng = random.Random(seed + 1)

    def skills_for(role: Role) -> frozenset[str]:
        chosen = rng.sample(list(SKILL_POOL), rng.randint(1, 3))
        if role is Role.HOIKUSHI and rng.random() < 0.6:
            chosen.append("乳幼児研修修了")
        return frozenset(dict.fromkeys(chosen))

    fixed_skills: dict[str, frozenset[str]] = {
        "正職員 幼稚園教諭": frozenset(
            {"幼児2歳児クラスの保育経験あり", "ピアノ指導可", "食育指導可"}
        ),
        "正職員 看護師": frozenset({"看護師免許", "応急処置資格", "アレルギー対応可"}),
        "パート 調理員": frozenset({"食育指導可", "アレルギー対応可"}),
        "正職員 園長": frozenset({"管理職", "幼児教育学修士"}),
    }
    fixed_memo: dict[str, str] = {
        "正職員 幼稚園教諭": "幼稚園教諭（配置基準上は保育士として計上）",
        "正職員 園長": "園長（配置基準には計上しない）",
    }

    members: list[StaffMember] = []
    for label, role, _employment, count in STAFF_BLUEPRINT:
        for _ in range(count):
            index = len(members) + 1
            members.append(
                StaffMember(
                    staff_id=f"S{index:02d}",
                    name=STAFF_NAMES[index - 1],
                    roles=(role,),
                    contract=_contract_for(label, rng),
                    skills=fixed_skills.get(label) or skills_for(role),
                    memo=fixed_memo.get(label, ""),
                )
            )

    assert len(members) == TOTAL_STAFF, len(members)
    return members


def _assign_off_days(
    staff: Sequence[StaffMember], day_list: Sequence[date], rng: random.Random
) -> dict[str, list[date]]:
    """希望休の日付を「1 日あたり職員数の 25% 以下」に収めて割り当てる。

    1. 職員をシャッフルし、1〜3 日を貪欲に割り当てつつ ``MAX_OFF_DAY_RATIO`` の
       上限 (``floor(職員数 * 0.25)`` 人) を守る。
    2. 上限のために 1 日も割り当てられなかった職員が生じた場合は、
       3 日取った職員から 1 日を过户して「全員 1 日以上」を保証する。
       过户は空きを作るだけなので上限は破られない。
    """
    cap = max(1, int(len(staff) * MAX_OFF_DAY_RATIO))
    per_day: dict[date, int] = {d: 0 for d in day_list}
    taken: dict[str, list[date]] = {m.staff_id: [] for m in staff}

    order = list(staff)
    rng.shuffle(order)
    for member in order:
        free = [d for d in day_list if per_day[d] < cap]
        free.sort(key=lambda d: d.weekday() >= 5)
        want = min(rng.randint(1, 3), len(free), len(day_list))
        for day in free[:want]:
            taken[member.staff_id].append(day)
            per_day[day] += 1

    for member in order:
        while not taken[member.staff_id]:
            donor = next(
                (m for m in order if len(taken[m.staff_id]) > 1 and m.staff_id != member.staff_id),
                None,
            )
            if donor is None:
                break
            freed = max(taken[donor.staff_id], key=lambda d: (per_day[d], d))
            taken[donor.staff_id].remove(freed)
            per_day[freed] -= 1
            target = min(day_list, key=lambda d: (per_day[d], d))
            taken[member.staff_id].append(target)
            per_day[target] += 1

    return {sid: sorted(days) for sid, days in taken.items()}


def make_preferences(
    staff: Sequence[StaffMember], days: Sequence[date], *, seed: int = 42
) -> dict[str, StaffPreferences]:
    """職員ごとの希望休（1〜3 日 / 1 日 ≤ 職員数の 25%）と早朝・延長の回避希望を作る。

    多数職員が同日に重なって休み、配置基準を満たせなくなるのを防ぐため、
    割り当ては :func:`_assign_off_days` の日別上限で制限する。
    """
    rng = random.Random(seed + 2)
    day_list = sorted(set(days))
    if not day_list:
        return {}
    reasons = list(LEAVE_REASONS)
    off_days = _assign_off_days(staff, day_list, rng)

    hoikushi = [m for m in staff if m.has_role(Role.HOIKUSHI)]
    avoid_early = {m.staff_id for m in hoikushi[:2]}
    avoid_late = {m.staff_id for m in hoikushi[2:4]}

    prefs: dict[str, StaffPreferences] = {}
    for member in staff:
        entry = StaffPreferences(notes=member.memo)
        for day in off_days.get(member.staff_id, []):
            entry.unavailable.append(
                Unavailability(
                    day=day, start=time(0, 0), end=time(23, 59), reason=rng.choice(reasons)
                )
            )
        workdays = [d for d in day_list if d.weekday() < 5]
        if workdays:
            entry.preferred_days = frozenset(rng.sample(workdays, min(2, len(workdays))))
            entry.preferred_off_days = frozenset(rng.sample(workdays, min(1, len(workdays))))
        if member.staff_id in avoid_early:
            entry.avoid_early = True
            entry.notes = (entry.notes + " 早朝を避けたい").strip()
        if member.staff_id in avoid_late:
            entry.avoid_late = True
            entry.notes = (entry.notes + " 延長を避けたい").strip()
        entry.unavailable.sort(key=lambda u: (u.day, u.start))
        prefs[member.staff_id] = entry
    return prefs


def default_days() -> list[date]:
    """サンプルの既定の対象期間（:data:`DEFAULT_RANGE_DAYS` 日）を返す。"""
    return list(
        daterange(DEFAULT_RANGE_START, DEFAULT_RANGE_START + timedelta(days=DEFAULT_RANGE_DAYS - 1))
    )


#: 旧 private 名の別名（内部実装との互換）。
_default_days = default_days


def make_dataset(
    days: Sequence[date] | None = None, *, seed: int = 42
) -> tuple[list[ChildPlan], list[StaffMember], dict[str, StaffPreferences]]:
    """園児 / 職員 / 希望休の 3 つセットを返す。"""
    day_list = list(days) if days is not None else default_days()
    children = make_children(day_list, seed=seed)
    staff = make_staff(seed=seed)
    prefs = make_preferences(staff, day_list, seed=seed)
    return children, staff, prefs


def sample_dataframes(
    days: Sequence[date] | None = None,
    *,
    seed: int = 42,
    children: Sequence[ChildPlan] | None = None,
    staff: Sequence[StaffMember] | None = None,
    preferences: dict[str, StaffPreferences] | None = None,
) -> dict[str, pd.DataFrame]:
    """日本語スキーマの DataFrame 3 枚を返す。"""
    if children is None or staff is None or preferences is None:
        gen_children, gen_staff, gen_prefs = make_dataset(days, seed=seed)
        children = gen_children if children is None else children
        staff = gen_staff if staff is None else staff
        preferences = gen_prefs if preferences is None else preferences

    child_records = [
        {
            "園児ID": c.child_id,
            "氏名": c.name,
            "年齢": str(c.age_class.years),
            "登園日": c.day.isoformat(),
            "登園時刻": "" if c.absent else c.arrive.strftime("%H:%M"),
            "降園時刻": "" if c.absent else c.depart.strftime("%H:%M"),
            "短時間保育": "true" if c.is_short_time else "false",
            "欠席": "true" if c.absent else "false",
            "欠席理由": c.absent_reason,
            "早朝保育": "true" if c.uses_early_care else "false",
            "延長保育": "true" if c.uses_late_care else "false",
            "備考": c.notes,
        }
        for c in children
    ]

    staff_records = [
        {
            "職員ID": m.staff_id,
            "氏名": m.name,
            "資格（主）": m.roles[0].value,
            "資格（副）": "|".join(r.value for r in m.roles[1:]),
            "雇用形態": m.contract.employment_type.value,
            "週契約時間": _num(m.contract.weekly_hours),
            "1日契約時間": _num(m.contract.daily_hours),
            "月間最小時間": _num(m.contract.min_monthly_hours),
            "月間最大時間": _num(m.contract.max_monthly_hours),
            "週最大出勤日数": str(m.contract.max_weekly_days),
            "最大連続勤務日数": str(m.contract.max_consecutive_days),
            "最早始業": m.contract.earliest_start.strftime("%H:%M"),
            "最遅終業": m.contract.latest_end.strftime("%H:%M"),
            "能力タグ": "|".join(sorted(m.skills)),
            "備考": m.memo,
        }
        for m in staff
    ]

    pref_records: list[dict[str, Any]] = []
    for staff_id in sorted(preferences):
        entry = preferences[staff_id]
        for item in entry.unavailable:
            pref_records.append(
                {
                    "職員ID": staff_id,
                    "種別": "出勤不可"
                    if item.start == time(0, 0) and item.end == time(23, 59)
                    else "希望休",
                    "日付": item.day.isoformat(),
                    "開始": "" if item.start == time(0, 0) else item.start.strftime("%H:%M"),
                    "終了": "" if item.end == time(23, 59) else item.end.strftime("%H:%M"),
                    "理由": item.reason,
                }
            )
        for day in sorted(entry.preferred_off_days):
            pref_records.append(
                {
                    "職員ID": staff_id,
                    "種別": "休み希望",
                    "日付": day.isoformat(),
                    "開始": "",
                    "終了": "",
                    "理由": "",
                }
            )
        for day in sorted(entry.preferred_days):
            pref_records.append(
                {
                    "職員ID": staff_id,
                    "種別": "出勤希望",
                    "日付": day.isoformat(),
                    "開始": "",
                    "終了": "",
                    "理由": "",
                }
            )

    frames = {
        "children": pd.DataFrame.from_records(child_records, columns=CHILDREN_COLUMNS),
        "staff": pd.DataFrame.from_records(staff_records, columns=STAFF_COLUMNS),
        "preferences": pd.DataFrame.from_records(pref_records, columns=PREFERENCE_COLUMNS),
    }
    return frames


def _num(value: float) -> str:
    """契約時間は「8」「4.5」のように素直に文字列化する。"""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def write_sample_files(
    target_dir: Path | str, days: Sequence[date] | None = None, seed: int = 42
) -> dict[str, Path]:
    """サンプル 3 枚を CSV として書き出す。"""
    base = Path(target_dir)
    base.mkdir(parents=True, exist_ok=True)
    frames = sample_dataframes(days, seed=seed)
    names = {"children": "children.csv", "staff": "staff.csv", "preferences": "preferences.csv"}
    out: dict[str, Path] = {}
    for key, frame in frames.items():
        path = base / names[key]
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        out[key] = path
    return out
