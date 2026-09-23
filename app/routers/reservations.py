"""换电预约路由（需登录）。"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from .. import clock
from ..auth import get_current_user
from ..database import get_db
from ..models import Reservation
from ..schemas import ReservationCreate, ReservationOut, ReservationRenew
from ..services import reservation_service as svc

router = APIRouter(prefix="/api/reservations", tags=["预约"], dependencies=[Depends(get_current_user)])

_STATUS_PATTERN = {"pending", "fulfilled", "cancelled", "expired"}


def _to_out(r: Reservation) -> ReservationOut:
    return ReservationOut(
        id=r.id,
        vehicle_id=r.vehicle_id,
        station_id=r.station_id,
        status=r.status,
        reserved_for=r.reserved_for,
        expires_at=r.expires_at,
        idempotency_key=r.idempotency_key,
        created_at=r.created_at,
        updated_at=r.updated_at,
        closed_at=r.closed_at,
        vehicle_plate=r.vehicle.plate if r.vehicle else None,
        station_name=r.station.name if r.station else None,
    )


@router.get("", response_model=list[ReservationOut])
def list_reservations(
    status_filter: Optional[str] = Query(None, alias="status"),
    vehicle_id: Optional[int] = None,
    station_id: Optional[int] = None,
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """按状态与时间窗口查询预约。"""
    if status_filter is not None and status_filter not in _STATUS_PATTERN:
        raise HTTPException(status_code=422, detail="非法的预约状态")
    # 查询前先释放过期占位，保证列表中的状态与可订容量准确
    svc.sweep_expired(db, commit=True)
    rows = svc.list_reservations(
        db,
        status=status_filter,
        vehicle_id=vehicle_id,
        station_id=station_id,
        time_from=time_from,
        time_to=time_to,
        limit=limit,
        offset=offset,
    )
    return [_to_out(r) for r in rows]


@router.post("", response_model=ReservationOut, status_code=status.HTTP_201_CREATED)
def create_reservation(payload: ReservationCreate, db: Session = Depends(get_db)):
    from .. import clock

    reserved_for = payload.reserved_for or clock.now()
    try:
        reservation, _created = svc.create_reservation(
            db,
            vehicle_id=payload.vehicle_id,
            station_id=payload.station_id,
            reserved_for=reserved_for,
            expires_at=payload.expires_at,
            idempotency_key=payload.idempotency_key,
        )
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _to_out(reservation)


@router.get("/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: int, db: Session = Depends(get_db)):
    svc.sweep_expired(db, commit=True)
    try:
        return _to_out(svc.get_reservation(db, reservation_id))
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/{reservation_id}/renew", response_model=ReservationOut)
def renew_reservation(reservation_id: int, payload: ReservationRenew, db: Session = Depends(get_db)):
    try:
        reservation = svc.renew_reservation(
            db,
            reservation_id,
            expires_at=payload.expires_at,
            extend_minutes=payload.extend_minutes,
            reserved_for=payload.reserved_for,
        )
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _to_out(reservation)


@router.post("/{reservation_id}/cancel", response_model=ReservationOut)
def cancel_reservation(reservation_id: int, db: Session = Depends(get_db)):
    try:
        reservation = svc.cancel_reservation(db, reservation_id)
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _to_out(reservation)
