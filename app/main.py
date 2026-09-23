"""应用入口。

新能源物流车换电站运营管理平台 —— 纯后端 API 服务。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routers import auth, dashboard, reservations, stations, swaps, vehicles
from .seed import init_db
from .sweeper import ReservationSweeper


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库（建表 + 种子数据 + 重启后识别过期预约）
    init_db()
    # 后台周期性扫描到期预约，释放爽约名额
    sweeper = ReservationSweeper()
    sweeper.start()
    try:
        yield
    finally:
        sweeper.stop()


app = FastAPI(
    title="换电站运营管理平台 API",
    description="新能源物流车换电站后台管理（纯后端），支持持久化预约与凭约换电。",
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
