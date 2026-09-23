"""预约管理路由（需登录）。"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import (
    RESERVATION_HELD,
    RESERVATION_QUERY_STATUSES,
    Reservation,
)
from ..schemas import ReservationCreate, ReservationOut, ReservationRenew
from ..services import reservations as service

router = APIRouter(prefix="/api/reservations", tags=["预约"], dependencies=[Depends(get_current_user)])


def _to_out(reservation: Reservation) -> ReservationOut:
    return ReservationOut(
        id=reservation.id,
        code=reservation.code,
        vehicle_id=reservation.vehicle_id,
        station_id=reservation.station_id,
        status=reservation.status,
        expected_at=reservation.expected_at,
        expires_at=reservation.expires_at,
        created_at=reservation.created_at,
        renewed_at=reservation.renewed_at,
        fulfilled_at=reservation.fulfilled_at,
        cancelled_at=reservation.cancelled_at,
        finalized_at=reservation.finalized_at,
        vehicle_plate=reservation.vehicle.plate if reservation.vehicle else None,
        station_name=reservation.station.name if reservation.station else None,
    )


@router.get("", response_model=list[ReservationOut])
def list_reservations(
    status_filter: Optional[str] = Query(None, alias="status"),
    vehicle_id: Optional[int] = None,
    station_id: Optional[int] = None,
    created_from: Optional[datetime] = None,
    created_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
):
    """按状态与时间查询预约。

    status 支持 held / fulfilled / cancelled / expired；active 等价于 held。
    时间范围作用于创建时间（created_from <= created_at <= created_to）。
    """
    # 先惰性过期，保证按状态查询时“待履约/已过期”与当前时刻一致
    service.expire_due_reservations(db)
    db.commit()

    query = db.query(Reservation)
    if status_filter is not None:
        if status_filter not in RESERVATION_QUERY_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"非法状态，可选：{', '.join(RESERVATION_QUERY_STATUSES)}",
            )
        normalized = RESERVATION_HELD if status_filter == "active" else status_filter
        query = query.filter(Reservation.status == normalized)
    if vehicle_id is not None:
        query = query.filter(Reservation.vehicle_id == vehicle_id)
    if station_id is not None:
        query = query.filter(Reservation.station_id == station_id)
    if created_from is not None:
        query = query.filter(Reservation.created_at >= created_from)
    if created_to is not None:
        query = query.filter(Reservation.created_at <= created_to)

    records = query.order_by(Reservation.created_at.desc(), Reservation.id.desc()).all()
    return [_to_out(r) for r in records]


@router.post("/expire-sweep")
def sweep_expired(db: Session = Depends(get_db)):
    """手动触发过期释放（启动与后台任务也会自动执行），返回释放数量。"""
    released = service.expire_due_reservations(db)
    db.commit()
    return {"released": released}


@router.post("", response_model=ReservationOut, status_code=status.HTTP_201_CREATED)
def create_reservation(payload: ReservationCreate, db: Session = Depends(get_db)):
    try:
        reservation, _created = service.create_reservation(
            db,
            vehicle_id=payload.vehicle_id,
            station_id=payload.station_id,
            ttl_minutes=payload.ttl_minutes,
            expected_at=payload.expected_at,
            idempotency_key=payload.idempotency_key,
        )
    except service.ReservationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _to_out(reservation)


@router.get("/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    service.expire_due_reservations(db)
    db.commit()
    reservation = db.get(Reservation, reservation_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="预约不存在")
    return _to_out(reservation)


@router.post("/{reservation_id}/renew", response_model=ReservationOut)
def renew_reservation(reservation_id: int, payload: ReservationRenew, db: Session = Depends(get_db)):
    try:
        reservation = service.renew_reservation(db, reservation_id, payload.ttl_minutes)
    except service.ReservationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _to_out(reservation)


@router.post("/{reservation_id}/cancel", response_model=ReservationOut)
def cancel_reservation(reservation_id: int, db: Session = Depends(get_db)):
    try:
        reservation = service.cancel_reservation(db, reservation_id)
    except service.ReservationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _to_out(reservation)
