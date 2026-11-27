"""NuShift FastAPI アプリケーションおよび SPA 配信エントリポイント。"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from shiftai.api.routes import router as api_router
from shiftai.config import APP_TITLE, APP_VERSION

_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]


def _resolve_cors_origins() -> list[str]:
    raw = os.getenv("NUSHIFT_CORS_ORIGINS", "")
    origins = [o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*"]
    return origins or list(_DEFAULT_CORS_ORIGINS)


def create_api_app(*, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(
        title=f"{APP_TITLE} API",
        version=APP_VERSION,
        description="NuShift 配置基準連動型シフト最適化 JSON/SSE API",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_origins=_resolve_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(api_router, prefix="/api/v1")

    dist_dir = frontend_dist or (Path(__file__).resolve().parents[3] / "frontend" / "dist")
    if dist_dir.exists() and (dist_dir / "index.html").exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        def serve_spa_index() -> FileResponse:
            return FileResponse(dist_dir / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def serve_api_root() -> JSONResponse:
            return JSONResponse(
                {
                    "service": "nushift-api",
                    "version": APP_VERSION,
                    "hint": "frontend/dist が未ビルドのため API モードで稼働中 (/api/v1/health)",
                }
            )

    return app
