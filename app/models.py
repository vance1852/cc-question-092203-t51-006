"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from .database import Base

# 预约状态
RESERVATION_PENDING = "pending"        # 已占位、待履约
RESERVATION_FULFILLED = "fulfilled"    # 已被换电消费
RESERVATION_CANCELLED = "cancelled"    # 主动取消
RESERVATION_EXPIRED = "expired"        # 到期未履约，名额已释放

RESERVATION_ACTIVE_STATUSES = (RESERVATION_PENDING,)


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
    # 电池仓位总数与当前满电可换电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    # 已被有效预约锁定的电池数；可预约量 = battery_ready - reserved_count
    reserved_count = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线
    status = Column(String(16), nullable=False, default="running")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    reservations = relationship("Reservation", back_populates="station")


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


class Reservation(Base):
    """换电预约。

    创建预约即从站点容量中原子锁定一份（``reserved_count += 1``）；
    取消、过期或履约时释放/核销该锁定。状态流转：

    pending ──履约──▶ fulfilled
       │──取消──▶ cancelled（释放容量）
       └──到期──▶ expired（释放容量）
    """

    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # pending / fulfilled / cancelled / expired
    status = Column(String(16), nullable=False, default=RESERVATION_PENDING, index=True)
    # 预约的到站时间窗口 [reserved_for, expires_at]，到期未履约自动释放
    reserved_for = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    # 调用方幂等键：重试创建不得重复占位
    idempotency_key = Column(String(64), unique=True, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # 进入终态（履约/取消/过期）的时间
    closed_at = Column(DateTime, nullable=True)

    vehicle = relationship("Vehicle", back_populates="reservations")
    station = relationship("Station", back_populates="reservations")
    swap = relationship("SwapRecord", back_populates="reservation", uselist=False)


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # 履约的预约（一次换电至多消费一份预约），用于预约/换电/库存原子关联
    reservation_id = Column(
        Integer, ForeignKey("reservations.id"), nullable=True, unique=True, index=True
    )
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")
    reservation = relationship("Reservation", back_populates="swap")
