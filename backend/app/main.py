"""实时指标计算与监控大盘平台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import ROUTERS
from app.services import profilechain
from app.store import store

app = FastAPI(title="实时指标计算与监控大盘平台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)

# 示例画像启动时补齐快照：列表、差异与事件分析消费方看到的是同一口径。
BOOTSTRAP_RESULT = profilechain.bootstrap()


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据与画像快照已经就绪。"""
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(store.module_names()),
        "profile_snapshots": BOOTSTRAP_RESULT,
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()
