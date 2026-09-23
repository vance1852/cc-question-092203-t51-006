"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数、当前满电可换电池数、已被预约锁定的电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    battery_held = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    reservations = relationship("Reservation", back_populates="station")

    __table_args__ = (
        CheckConstraint("battery_held >= 0", name="ck_station_battery_held_nonneg"),
        CheckConstraint("battery_ready >= battery_held", name="ck_station_battery_held_le_ready"),
        CheckConstraint("battery_ready <= slot_total", name="ck_station_battery_ready_le_slots"),
    )


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")
    reservations = relationship("Reservation", back_populates="vehicle")


# 预约状态：held 有效（已锁定容量）/ fulfilled 已履约 / cancelled 已取消 / expired 已过期释放
RESERVATION_HELD = "held"
RESERVATION_FULFILLED = "fulfilled"
RESERVATION_CANCELLED = "cancelled"
RESERVATION_EXPIRED = "expired"
RESERVATION_STATUSES = (
    RESERVATION_HELD,
    RESERVATION_FULFILLED,
    RESERVATION_CANCELLED,
    RESERVATION_EXPIRED,
)
# 调用方查询时可使用的状态过滤；active = 仍占用容量的有效预约
RESERVATION_QUERY_STATUSES = RESERVATION_STATUSES + ("active",)


class Reservation(Base):
    """换电预约。

    held 状态的预约持有站点一份满电电池容量（体现为 stations.battery_held +1）。
    取消/过期时归还容量并迁移到终态；履约时容量转为实际扣减，不归还。
    """

    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), unique=True, nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # held / fulfilled / cancelled / expired
    status = Column(String(16), nullable=False, default=RESERVATION_HELD, index=True)
    # 调用方幂等键：同键重试直接返回已有预约，不重复占位
    idempotency_key = Column(String(64), unique=True, nullable=True, index=True)
    expected_at = Column(DateTime, nullable=False)  # 预约到站时间
    expires_at = Column(DateTime, nullable=False, index=True)  # 到期未履约则释放
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    renewed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    fulfilled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    finalized_at = Column(DateTime, nullable=True, index=True)  # 进入任一终态的时间

    vehicle = relationship("Vehicle", back_populates="reservations")
    station = relationship("Station", back_populates="reservations")
    swap = relationship("SwapRecord", back_populates="reservation", uselist=False)


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # 凭预约换电时关联预约；一对一，保证一条预约只能履约一次
    reservation_id = Column(
        Integer, ForeignKey("reservations.id"), nullable=True, unique=True, index=True
    )
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")
    reservation = relationship("Reservation", back_populates="swap")
