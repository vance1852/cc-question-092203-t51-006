"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, computed_field

from .config import RESERVATION_DEFAULT_TTL_MINUTES


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
    # 已被有效预约锁定的容量，由系统维护，不由调用方写入
    battery_held: int = 0
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def available_capacity(self) -> int:
        """当前可预约量：满电电池扣除已锁定数量，与换电接口口径一致。"""
        return self.battery_ready - self.battery_held

    model_config = {"from_attributes": True}


class StationCapacityOut(BaseModel):
    """站点可预约量（运营查询与换电扣减共用同一口径）。"""

    station_id: int
    battery_ready: int
    battery_held: int
    available_capacity: int


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
    # 预约到站时间，仅用于运营展示；不传默认当前时间
    expected_at: Optional[datetime] = None
    # 有效时长（分钟），从创建时刻起算，到期未履约自动释放
    ttl_minutes: int = Field(RESERVATION_DEFAULT_TTL_MINUTES, ge=1, le=240)
    # 调用方幂等键：相同键重试返回同一预约，不会重复占位
    idempotency_key: Optional[str] = Field(None, min_length=1, max_length=64)


class ReservationRenew(BaseModel):
    ttl_minutes: int = Field(RESERVATION_DEFAULT_TTL_MINUTES, ge=1, le=240)


class ReservationOut(BaseModel):
    id: int
    code: str
    vehicle_id: int
    station_id: int
    status: str
    expected_at: datetime
    expires_at: datetime
    created_at: datetime
    renewed_at: datetime
    fulfilled_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    finalized_at: Optional[datetime] = None
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    vehicle_id: int
    station_id: int
    # 必须携带与车辆、站点匹配且仍有效的预约码
    reservation_code: str = Field(..., min_length=1, max_length=32)
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    reservation_id: Optional[int] = None
    reservation_code: Optional[str] = None
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
    battery_held_total: int
    reservation_active: int
