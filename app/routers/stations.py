"""换电站管理路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Reservation, Station, SwapRecord
from ..schemas import (
    StationCapacityOut,
    StationCreate,
    StationOut,
    StationUpdate,
)
from ..services.reservations import available_capacity

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    return db.query(Station).order_by(Station.id).all()


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    station = Station(**payload.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return station


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return station


@router.get("/{station_id}/capacity", response_model=StationCapacityOut)
def get_station_capacity(station_id: int, db: Session = Depends(get_db)):
    """运营视角的可预约量，口径与预约占位/换电扣减完全一致。"""
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    return StationCapacityOut(
        station_id=station.id,
        battery_ready=station.battery_ready,
        battery_held=station.battery_held,
        available_capacity=available_capacity(station),
    )


@router.put("/{station_id}", response_model=StationOut)
def update_station(station_id: int, payload: StationUpdate, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(station, key, value)
    if station.battery_ready > station.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    if station.battery_ready < station.battery_held:
        raise HTTPException(
            status_code=422,
            detail="满电电池数不能小于已被有效预约锁定的数量，请先释放相关预约",
        )
    db.commit()
    db.refresh(station)
    return station


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    referenced = (
        db.query(SwapRecord.id).filter(SwapRecord.station_id == station_id).first()
        or db.query(Reservation.id).filter(Reservation.station_id == station_id).first()
    )
    if referenced:
        raise HTTPException(status_code=409, detail="该站点已有预约或换电记录，不能删除")
    db.delete(station)
    db.commit()
    return None
