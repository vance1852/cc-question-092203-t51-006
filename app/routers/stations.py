"""换电站管理路由（需登录）。"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from ..models import Station
from ..schemas import StationCapacityOut, StationCreate, StationOut, StationUpdate
from ..services import reservation_service as svc

router = APIRouter(prefix="/api/stations", tags=["换电站"], dependencies=[Depends(get_current_user)])


def _to_out(station: Station, available: int) -> StationOut:
    return StationOut(
        id=station.id,
        name=station.name,
        address=station.address,
        slot_total=station.slot_total,
        battery_ready=station.battery_ready,
        reserved_count=station.reserved_count,
        status=station.status,
        available=available,
        created_at=station.created_at,
    )


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    # 列表先统一完成过期释放，保证运营看到的可预约量与预约/换电接口口径一致
    svc.sweep_expired(db, commit=True)
    stations = db.query(Station).order_by(Station.id).all()
    return [
        _to_out(s, max(s.battery_ready - s.reserved_count, 0))
        for s in stations
    ]


@router.post("", response_model=StationOut, status_code=status.HTTP_201_CREATED)
def create_station(payload: StationCreate, db: Session = Depends(get_db)):
    if payload.battery_ready > payload.slot_total:
        raise HTTPException(status_code=422, detail="满电电池数不能超过仓位总数")
    station = Station(**payload.model_dump())
    db.add(station)
    db.commit()
    db.refresh(station)
    return _to_out(station, station.battery_ready)


@router.get("/{station_id}/capacity", response_model=StationCapacityOut)
def get_station_capacity(station_id: int, db: Session = Depends(get_db)):
    """查询站点实时可预约量（先释放过期占位，与下单口径一致）。"""
    try:
        available = svc.available_capacity(db, station_id)
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    station = db.get(Station, station_id)
    return StationCapacityOut(
        station_id=station.id,
        battery_ready=station.battery_ready,
        reserved_count=station.reserved_count,
        available=available,
    )


@router.get("/{station_id}", response_model=StationOut)
def get_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    try:
        available = svc.available_capacity(db, station_id)
    except svc.ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _to_out(station, available)


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
    if station.battery_ready < station.reserved_count:
        raise HTTPException(status_code=422, detail="满电电池数不能少于已被预约锁定的数量")
    db.commit()
    db.refresh(station)
    return _to_out(station, max(station.battery_ready - station.reserved_count, 0))


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_station(station_id: int, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if not station:
        raise HTTPException(status_code=404, detail="换电站不存在")
    db.delete(station)
    db.commit()
    return None
