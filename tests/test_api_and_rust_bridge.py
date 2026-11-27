from __future__ import annotations

from fastapi.testclient import TestClient

from shiftai import rust_bridge
from shiftai.api import create_api_app


def test_rust_bridge_fallback_matches_statutory_rules() -> None:
    assert rust_bridge.required_break_minutes(360) == 0
    assert rust_bridge.required_break_minutes(361) == 45
    assert rust_bridge.required_break_minutes(481) == 60

    ok_res = rust_bridge.validate_staff_day(
        {
            "staff_id": "S001",
            "work_minutes": 480,
            "break_minutes": 60,
            "daily_hours": 8.0,
            "overtime_allowed": False,
            "has_36_agreement": False,
            "consecutive_work_days": 3,
            "max_consecutive_days": 5,
        }
    )
    assert ok_res["ok"] is True
    assert ok_res["issues"] == []

    ng_res = rust_bridge.validate_staff_day(
        {
            "staff_id": "S002",
            "work_minutes": 540,
            "break_minutes": 30,
            "daily_hours": 8.0,
            "overtime_allowed": True,
            "has_36_agreement": False,
            "consecutive_work_days": 6,
            "max_consecutive_days": 5,
        }
    )
    assert ng_res["ok"] is False
    codes = {issue["code"] for issue in ng_res["issues"]}
    assert "STATUTORY_BREAK_SHORTAGE" in codes
    assert "DAILY_CAP_EXCEEDED" in codes
    assert "MAX_CONSECUTIVE_DAYS" in codes


def test_shiftai_api_serves_spa_and_endpoints() -> None:
    app = create_api_app()
    client = TestClient(app)

    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "NuShift" in res_root.text

    res_health = client.get("/api/v1/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"

    res_grid = client.get("/api/v1/sample-grid?days_count=7")
    assert res_grid.status_code == 200
    grid = res_grid.json()
    assert len(grid["days"]) == 7
    assert len(grid["staff"]) >= 1

    res_val = client.post(
        "/api/v1/validate-day",
        json={
            "staff_id": "S001",
            "work_minutes": 500,
            "break_minutes": 30,
            "daily_hours": 8.0,
        },
    )
    assert res_val.status_code == 200
    assert res_val.json()["ok"] is False
