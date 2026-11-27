"""NuShift REST API ルーター。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from shiftai import rust_bridge, sample_data
from shiftai.config import APP_VERSION

router = APIRouter(tags=["nushift"])


class StaffDayValidationRequest(BaseModel):
    staff_id: str
    work_minutes: int = Field(ge=0)
    break_minutes: int = Field(ge=0, default=0)
    daily_hours: float = Field(gt=0, default=8.0)
    overtime_allowed: bool = False
    has_36_agreement: bool = False
    consecutive_work_days: int = Field(ge=0, default=1)
    max_consecutive_days: int = Field(ge=0, default=5)


class GridSolveRequest(BaseModel):
    days_count: int = Field(ge=1, le=31, default=14)
    start_date: str | None = None


@router.get("/health")
def api_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "rust_core_available": rust_bridge.RUST_CORE_AVAILABLE,
    }


@router.post("/validate-day")
def validate_staff_day_endpoint(req: StaffDayValidationRequest) -> dict[str, Any]:
    return rust_bridge.validate_staff_day(req.model_dump())


@router.get("/sample-grid")
def get_sample_grid(days_count: int = 14) -> dict[str, Any]:
    """SPA の月間シフトエディタ向けの初期グリッドデータ（職員・日付・パターン）を返す。"""
    staff_list = sample_data.make_staff()
    start = date.today().replace(day=1)
    days = [(start + timedelta(days=i)).isoformat() for i in range(max(1, min(days_count, 31)))]

    patterns = [
        {"symbol": "早", "label": "早番 (07:30-16:30)", "work_minutes": 480, "break_minutes": 60},
        {"symbol": "日", "label": "日勤 (08:30-17:30)", "work_minutes": 480, "break_minutes": 60},
        {"symbol": "遅", "label": "遅番 (10:00-19:00)", "work_minutes": 480, "break_minutes": 60},
        {"symbol": "休", "label": "公休", "work_minutes": 0, "break_minutes": 0},
    ]

    staff_rows = []
    cycle = ["早", "日", "遅", "休"]
    for idx, st_member in enumerate(staff_list):
        assignments: dict[str, str] = {}
        for d_idx, d_str in enumerate(days):
            assignments[d_str] = "休" if (d_idx + idx) % 7 in (5, 6) else cycle[(d_idx + idx) % 3]
        staff_rows.append(
            {
                "staff_id": st_member.staff_id,
                "name": st_member.name,
                "role": st_member.primary_role.value,
                "daily_hours": float(st_member.contract.daily_hours),
                "overtime_allowed": bool(st_member.contract.overtime_allowed),
                "has_36_agreement": bool(st_member.contract.has_36_agreement),
                "max_consecutive_days": int(st_member.contract.max_consecutive_days),
                "assignments": assignments,
                "pinned": {},
            }
        )

    return {
        "days": days,
        "patterns": patterns,
        "staff": staff_rows,
    }
