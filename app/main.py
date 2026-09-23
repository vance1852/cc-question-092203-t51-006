"""应用入口。

新能源物流车换电站运营管理平台 —— 纯后端 API 服务。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import SessionLocal
from .routers import auth, dashboard, reservations, stations, swaps, vehicles
from .seed import init_db
from .services.reservation_service import sweep_expired


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库（建表 + 种子数据）
    init_db()
    # 重启后识别并释放已过期但仍占位的预约
    db = SessionLocal()
    try:
        sweep_expired(db, commit=True)
    finally:
        db.close()
    yield


app = FastAPI(
    title="换电站运营管理平台 API",
    description="新能源物流车换电站后台管理（纯后端）。",
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/api/health", tags=["系统"])
def health():
    return {"status": "ok", "service": "swap-station-admin"}


app.include_router(auth.router)
app.include_router(stations.router)
app.include_router(vehicles.router)
app.include_router(reservations.router)
app.include_router(swaps.router)
app.include_router(dashboard.router)
