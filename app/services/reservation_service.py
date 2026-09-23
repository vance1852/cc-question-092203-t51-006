"""预约与换电的领域服务。

所有涉及容量（``stations.battery_ready / reserved_count``）与预约状态的变更，
引擎层（见 database.py）都以 ``BEGIN IMMEDIATE`` 开启事务：SQLite 下写事务在
全库级别串行执行，配合条件 UPDATE（CAS 风格），保证：

- 创建预约原子锁定一份容量，并发争抢最后名额时结果确定（一个成功，其余失败）；
- 取消、过期、履约原子释放/核销锁定；
- 换电只能消费匹配且仍有效的预约，并与换电记录、库存变化同事务提交；
- 调用方携带幂等键重试不会重复占位。
"""
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import clock
from ..config import DEFAULT_RESERVATION_WINDOW_MINUTES, MAX_RESERVATION_WINDOW_MINUTES
from ..models import (
    RESERVATION_CANCELLED,
    RESERVATION_EXPIRED,
    RESERVATION_FULFILLED,
    RESERVATION_PENDING,
    Reservation,
    Station,
    SwapRecord,
    Vehicle,
)


class ServiceError(Exception):
    """业务错误：携带 HTTP 状态码与中文提示。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _commit(db: Session) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------- 过期扫描（重启后识别、每次写操作前兜底） ----------

def sweep_expired(db: Session, at: Optional[datetime] = None, commit: bool = False) -> int:
    """把所有已到期但仍 pending 的预约标记为 expired，并释放其锁定的容量。

    默认在调用方已有事务内执行（不单独提交）；``commit=True`` 时自行提交。
    返回被释放的预约数量。
    """
    at = at or clock.now()
    rows = (
        db.query(Reservation.id, Reservation.station_id)
        .filter(Reservation.status == RESERVATION_PENDING, Reservation.expires_at <= at)
        .all()
    )
    if not rows:
        return 0

    # 按站点汇总需要释放的锁定数，再做带下限保护的原子扣减
    released: dict[int, int] = {}
    for _rid, station_id in rows:
        released[station_id] = released.get(station_id, 0) + 1
    for station_id, count in released.items():
        result = db.execute(
            text(
                "UPDATE stations SET reserved_count = reserved_count - :n "
                "WHERE id = :sid AND reserved_count >= :n"
            ),
            {"n": count, "sid": station_id},
        )
        if result.rowcount != 1:  # 容量账不应出现负值，出现即数据损坏
            raise ServiceError(500, "站点预留容量数据异常，释放失败")

    db.query(Reservation).filter(
        Reservation.status == RESERVATION_PENDING, Reservation.expires_at <= at
    ).update(
        {Reservation.status: RESERVATION_EXPIRED, Reservation.closed_at: at},
        synchronize_session=False,
    )
    if commit:
        _commit(db)
    return len(rows)


# ---------- 查询 ----------

def available_capacity(db: Session, station_id: int, at: Optional[datetime] = None) -> int:
    """站点当前可预约量（与换电/预约接口使用同一口径）。

    可预约量 = battery_ready - reserved_count（先完成过期释放）。
    """
    at = at or clock.now()
    station = db.get(Station, station_id)
    if station is None:
        raise ServiceError(404, "换电站不存在")
    # 读路径也先清理过期占位，保证运营看到的余量与实际可订一致
    sweep_expired(db, at, commit=True)
    db.refresh(station)
    return max(station.battery_ready - station.reserved_count, 0)


def list_reservations(
    db: Session,
    *,
    status: Optional[str] = None,
    vehicle_id: Optional[int] = None,
    station_id: Optional[int] = None,
    time_from: Optional[datetime] = None,
    time_to: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Reservation]:
    """按状态与时间窗口查询预约（时间作用于 reserved_for）。"""
    query = db.query(Reservation)
    if status:
        query = query.filter(Reservation.status == status)
    if vehicle_id is not None:
        query = query.filter(Reservation.vehicle_id == vehicle_id)
    if station_id is not None:
        query = query.filter(Reservation.station_id == station_id)
    if time_from is not None:
        query = query.filter(Reservation.reserved_for >= time_from)
    if time_to is not None:
        query = query.filter(Reservation.reserved_for <= time_to)
    return (
        query.order_by(Reservation.reserved_for.desc(), Reservation.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def get_reservation(db: Session, reservation_id: int) -> Reservation:
    reservation = db.get(Reservation, reservation_id)
    if reservation is None:
        raise ServiceError(404, "预约不存在")
    return reservation


# ---------- 创建 ----------

def _windows_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start <= b_end and b_start <= a_end


def create_reservation(
    db: Session,
    *,
    vehicle_id: int,
    station_id: int,
    reserved_for: datetime,
    expires_at: Optional[datetime] = None,
    idempotency_key: Optional[str] = None,
    at: Optional[datetime] = None,
) -> tuple[Reservation, bool]:
    """创建预约并锁定一份站点容量。

    返回 (预约, 是否新建)；幂等键命中已有预约时返回 (已有预约, False)。
    """
    at = at or clock.now()
    if expires_at is None:
        expires_at = reserved_for + timedelta(minutes=DEFAULT_RESERVATION_WINDOW_MINUTES)
    if expires_at <= at:
        raise ServiceError(422, "到期时间必须晚于当前时间")
    if expires_at <= reserved_for:
        raise ServiceError(422, "到期时间必须晚于预约到站时间")
    if expires_at - reserved_for > timedelta(minutes=MAX_RESERVATION_WINDOW_MINUTES):
        raise ServiceError(422, f"预约保留时长不能超过 {MAX_RESERVATION_WINDOW_MINUTES} 分钟")

    try:
        sweep_expired(db, at)

        # 幂等重试：同一幂等键直接回放已有预约，绝不二次占位
        if idempotency_key:
            existing = (
                db.query(Reservation)
                .filter(Reservation.idempotency_key == idempotency_key)
                .first()
            )
            if existing is not None:
                if existing.vehicle_id != vehicle_id or existing.station_id != station_id:
                    raise ServiceError(409, "幂等键已被其他车辆或站点的预约占用")
                _commit(db)  # 连同本次过期扫描一并落库，回放已有预约
                return existing, False

        vehicle = db.get(Vehicle, vehicle_id)
        if vehicle is None:
            raise ServiceError(404, "车辆不存在")
        station = db.get(Station, station_id)
        if station is None:
            raise ServiceError(404, "换电站不存在")
        if station.status != "running":
            raise ServiceError(422, "该换电站当前不运营，无法预约")

        # 同一车辆的有效预约时间窗不得重叠
        clash = (
            db.query(Reservation)
            .filter(
                Reservation.vehicle_id == vehicle_id,
                Reservation.status == RESERVATION_PENDING,
            )
            .all()
        )
        for other in clash:
            if _windows_overlap(reserved_for, expires_at, other.reserved_for, other.expires_at):
                raise ServiceError(409, "同一车辆在该时段已有有效预约，时间窗重叠")

        # 原子锁定一份容量：仅当仍有可预约量时成功（并发争抢的决胜点）
        result = db.execute(
            text(
                "UPDATE stations SET reserved_count = reserved_count + 1 "
                "WHERE id = :sid AND battery_ready - reserved_count > 0"
            ),
            {"sid": station_id},
        )
        if result.rowcount != 1:
            raise ServiceError(409, "该换电站可预约容量不足")

        reservation = Reservation(
            vehicle_id=vehicle_id,
            station_id=station_id,
            status=RESERVATION_PENDING,
            reserved_for=reserved_for,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            created_at=at,
            updated_at=at,
        )
        db.add(reservation)
        db.flush()
        _commit(db)
        return reservation, True
    except Exception:
        db.rollback()
        raise


# ---------- 续期 ----------

def renew_reservation(
    db: Session,
    reservation_id: int,
    *,
    expires_at: Optional[datetime] = None,
    extend_minutes: Optional[int] = None,
    reserved_for: Optional[datetime] = None,
    at: Optional[datetime] = None,
) -> Reservation:
    """续期待履约预约（延长有效期，锁定的容量保持不变）。"""
    at = at or clock.now()
    try:
        sweep_expired(db, at)
        reservation = db.get(Reservation, reservation_id)
        if reservation is None:
            raise ServiceError(404, "预约不存在")
        if reservation.status != RESERVATION_PENDING:
            raise ServiceError(409, f"预约当前状态为 {reservation.status}，无法续期")

        new_expires = expires_at
        if extend_minutes is not None:
            if extend_minutes <= 0:
                raise ServiceError(422, "续期分钟数必须为正数")
            new_expires = reservation.expires_at + timedelta(minutes=extend_minutes)
        if new_expires is None:
            raise ServiceError(422, "需提供新的到期时间或续期分钟数")
        if new_expires <= at:
            raise ServiceError(422, "新的到期时间必须晚于当前时间")
        new_reserved_for = reserved_for or reservation.reserved_for
        if new_expires <= new_reserved_for:
            raise ServiceError(422, "到期时间必须晚于预约到站时间")
        if new_expires - new_reserved_for > timedelta(minutes=MAX_RESERVATION_WINDOW_MINUTES):
            raise ServiceError(422, f"预约保留时长不能超过 {MAX_RESERVATION_WINDOW_MINUTES} 分钟")

        # 续期后同样不能与本车其他有效预约重叠
        others = (
            db.query(Reservation)
            .filter(
                Reservation.vehicle_id == reservation.vehicle_id,
                Reservation.status == RESERVATION_PENDING,
                Reservation.id != reservation_id,
            )
            .all()
        )
        for other in others:
            if _windows_overlap(new_reserved_for, new_expires, other.reserved_for, other.expires_at):
                raise ServiceError(409, "续期后的时间窗与本车其他有效预约重叠")

        reservation.reserved_for = new_reserved_for
        reservation.expires_at = new_expires
        reservation.updated_at = at
        db.flush()
        _commit(db)
        return reservation
    except Exception:
        db.rollback()
        raise


# ---------- 取消 ----------

def cancel_reservation(db: Session, reservation_id: int, at: Optional[datetime] = None) -> Reservation:
    """取消预约并释放锁定的容量。"""
    at = at or clock.now()
    try:
        sweep_expired(db, at)
        reservation = db.get(Reservation, reservation_id)
        if reservation is None:
            raise ServiceError(404, "预约不存在")
        if reservation.status == RESERVATION_CANCELLED:
            _commit(db)
            return reservation  # 取消天然幂等
        if reservation.status != RESERVATION_PENDING:
            raise ServiceError(409, f"预约当前状态为 {reservation.status}，无法取消")

        result = db.execute(
            text(
                "UPDATE stations SET reserved_count = reserved_count - 1 "
                "WHERE id = :sid AND reserved_count >= 1"
            ),
            {"sid": reservation.station_id},
        )
        if result.rowcount != 1:
            raise ServiceError(500, "站点预留容量数据异常，释放失败")
        reservation.status = RESERVATION_CANCELLED
        reservation.closed_at = at
        reservation.updated_at = at
        db.flush()
        _commit(db)
        return reservation
    except Exception:
        db.rollback()
        raise


# ---------- 履约（实际换电，消费预约） ----------

def fulfill_swap(
    db: Session,
    *,
    reservation_id: int,
    soc_before: float,
    soc_after: float,
    at: Optional[datetime] = None,
) -> SwapRecord:
    """凭一份匹配且仍有效的预约完成换电。

    预约核销、电池库存扣减、车辆电量更新、换电记录写入在同一事务内原子完成。
    """
    at = at or clock.now()
    if soc_after <= soc_before:
        raise ServiceError(422, "换电后电量应高于换电前电量")

    try:
        sweep_expired(db, at)
        reservation = db.get(Reservation, reservation_id)
        if reservation is None:
            raise ServiceError(404, "预约不存在")
        if reservation.status != RESERVATION_PENDING:
            raise ServiceError(409, f"预约当前状态为 {reservation.status}，无法履约")
        vehicle = db.get(Vehicle, reservation.vehicle_id)
        station = db.get(Station, reservation.station_id)
        if vehicle is None or station is None:
            raise ServiceError(404, "预约关联的车辆或站点不存在")
        if station.status != "running":
            raise ServiceError(422, "该换电站当前不运营，无法换电")

        # 1) 原子核销预约（并发重复消费时只有一个事务能成功）
        consumed = db.execute(
            text(
                "UPDATE reservations SET status = :fulfilled, closed_at = :at, updated_at = :at "
                "WHERE id = :rid AND status = :pending"
            ),
            {
                "fulfilled": RESERVATION_FULFILLED,
                "pending": RESERVATION_PENDING,
                "at": at,
                "rid": reservation_id,
            },
        )
        if consumed.rowcount != 1:
            raise ServiceError(409, "预约已被消费或已失效")

        # 2) 原子扣减实物电池并同时销账预留（battery_ready 与 reserved_count 各减一）
        stock = db.execute(
            text(
                "UPDATE stations "
                "SET battery_ready = battery_ready - 1, reserved_count = reserved_count - 1 "
                "WHERE id = :sid AND battery_ready >= 1 AND reserved_count >= 1"
            ),
            {"sid": station.id},
        )
        if stock.rowcount != 1:
            raise ServiceError(500, "站点库存或预留数据异常")

        # 3) 更新车辆电量
        vehicle.current_soc = soc_after
        if vehicle.status != "fault":
            vehicle.status = "idle"

        # 4) 写入换电记录，与预约原子关联
        record = SwapRecord(
            vehicle_id=vehicle.id,
            station_id=station.id,
            reservation_id=reservation.id,
            soc_before=soc_before,
            soc_after=soc_after,
            swapped_at=at,
        )
        db.add(record)
        db.flush()
        _commit(db)
        db.refresh(record)
        return record
    except Exception:
        db.rollback()
        raise
