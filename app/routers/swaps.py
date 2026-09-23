"""换电记录路由（需登录）。

实际换电只能凭匹配且仍有效的预约履约：POST /api/swaps 消费预约，
预约核销、库存扣减、车辆电量更新与换电记录写入在同一事务内原子完成。
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import SwapRecord
from ..schemas import SwapCreate, SwapOut
from ..services import reservation_service as svc

router = APIRouter(prefix="/api/swaps", tags=["换电记录"], dependencies=[Depends(get_current_user)])


def _to_out(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id,
        vehicle_id=record.vehicle_id,
        station_id=record.station_id,
        reservation_id=record.reservation_id,
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
        record = svc.fulfill_swap(
            db,
            reservation_id=payload.reservation_id,
            soc_before=payload.soc_before,
            soc_after=payload.soc_after,
        )
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _to_out(record)
