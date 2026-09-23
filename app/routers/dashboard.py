"""仪表盘统计路由（需登录）。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..clock import clock
from ..database import get_db
from ..models import (
    RESERVATION_HELD,
    Reservation,
    Station,
    SwapRecord,
    Vehicle,
)
from ..schemas import DashboardStats
from ..services.reservations import expire_due_reservations

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    # 统计前先惰性过期，保证待履约/已过期口径与当前时刻一致
    expire_due_reservations(db)
    db.commit()
    day_start = clock.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return DashboardStats(
        station_total=db.query(Station).count(),
        station_running=db.query(Station).filter(Station.status == "running").count(),
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= day_start).count(),
        battery_ready_total=db.query(func.coalesce(func.sum(Station.battery_ready), 0)).scalar() or 0,
        battery_held_total=db.query(func.coalesce(func.sum(Station.battery_held), 0)).scalar() or 0,
        reservation_active=db.query(Reservation)
        .filter(Reservation.status == RESERVATION_HELD)
        .count(),
    )
