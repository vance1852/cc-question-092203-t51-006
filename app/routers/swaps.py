"""换电记录路由（需登录）。

实际换电必须消费一份与车辆、站点匹配且仍有效的预约；
预约状态、库存变化与换电记录在同一事务内原子落库。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import SwapRecord
from ..schemas import SwapCreate, SwapOut
from ..services import reservations as service

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        reservation_id=record.reservation_id,
        reservation_code=record.reservation.code if record.reservation else None,
        soc_before=record.soc_before,
        soc_after=record.soc_after,
        swapped_at=record.swapped_at,
        vehicle_plate=record.vehicle.plate if record.vehicle else None,
        station_name=record.station.name if record.station else None,
    )


@router.get("", response_model=list[SwapOut])
def list_swaps(db: Session = Depends(get_db)):
    records = db.query(SwapRecord).order_by(SwapRecord.swapped_at.desc()).all()
    return [_to_out(r) for r in records]


@router.post("", response_model=SwapOut, status_code=status.HTTP_201_CREATED)
def create_swap(payload: SwapCreate, db: Session = Depends(get_db)):
    try:
        record = service.fulfill_reservation(
            db,
            vehicle_id=payload.vehicle_id,
            station_id=payload.station_id,
            reservation_code=payload.reservation_code,
            soc_before=payload.soc_before,
            soc_after=payload.soc_after,
        )
    except service.ReservationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _to_out(record)
