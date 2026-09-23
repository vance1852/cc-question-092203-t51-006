"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")


class StationOut(StationBase):
    id: int
    # 已被有效预约锁定的电池数
    reserved_count: int = 0
    # 当前可预约量 = battery_ready - reserved_count
    available: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class StationCapacityOut(BaseModel):
    station_id: int
    battery_ready: int
    reserved_count: int
    available: int



# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 预约 ----------
class ReservationCreate(BaseModel):
    vehicle_id: int
    station_id: int
    # 预约到站时间；不传则默认为当前时间
    reserved_for: Optional[datetime] = None
    # 到期时间；不传则为到站时间 + 默认保留时长
    expires_at: Optional[datetime] = None
    # 调用方幂等键：相同键的重试返回同一预约，不重复占位
    idempotency_key: Optional[str] = Field(None, max_length=64)


class ReservationRenew(BaseModel):
    # 二选一：直接给新到期时间，或在原到期时间上顺延若干分钟
    expires_at: Optional[datetime] = None
    extend_minutes: Optional[int] = Field(None, gt=0)
    reserved_for: Optional[datetime] = None


class ReservationOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    status: str
    reserved_for: datetime
    expires_at: datetime
    idempotency_key: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    # 凭预约履约：只能消费匹配且仍有效的预约
    reservation_id: int
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    reservation_id: Optional[int] = None
    soc_before: float
    soc_after: float
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    swap_today: int
    battery_ready_total: int
