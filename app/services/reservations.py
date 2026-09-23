"""预约与换电核心业务逻辑。

所有写操作都在单个事务内完成（引擎层统一以 BEGIN IMMEDIATE 开事务，
写请求在 SQLite 层串行执行），配合条件 UPDATE 做容量判定：

- 占位：UPDATE stations SET battery_held = battery_held + 1
        WHERE id = ? AND battery_ready - battery_held > 0
  影响行数为 0 即容量不足，确定地拒绝，绝不超卖。
- 履约：预约状态、站点库存、换电记录、车辆电量在同一事务内提交，
  reservation_id 唯一约束保证一条预约只能被消费一次。
"""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import clock
from ..models import (
    RESERVATION_CANCELLED,
    RESERVATION_EXPIRED,
    RESERVATION_FULFILLED,
    RESERVATION_HELD,
    Reservation,
    Station,
    SwapRecord,
    Vehicle,
)


class ReservationError(Exception):
    """业务校验失败。status 为建议的 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _expire_and_commit(db: Session) -> datetime:
    """惰性过期并把释放结果先落库，再返回当前时刻。

    单独提交可保证：即使后续业务校验失败（如 409/422），
    已到期名额的释放也不会随回滚丢失。
    """
    now = clock.now()
    if expire_due_reservations(db, now):
        db.commit()
    return now


def generate_code() -> str:
    return f"RSV{uuid.uuid4().hex[:12].upper()}"


def expire_due_reservations(db: Session, now: datetime | None = None) -> int:
    """把所有已到期但仍 held 的预约置为 expired 并归还其锁定容量。

    可在启动、后台扫描及各业务操作前安全重复调用（惰性过期）。
    返回本次释放的预约数量。
    """
    now = now or clock.now()
    # 先统计各站即将释放的数量（此时行仍是 held），再改状态、减锁定。
    due = (
        db.query(Reservation.station_id, func.count(Reservation.id))
        .filter(Reservation.status == RESERVATION_HELD, Reservation.expires_at <= now)
        .group_by(Reservation.station_id)
        .all()
    )
    if not due:
        return 0
    expired = (
        db.query(Reservation)
        .filter(Reservation.status == RESERVATION_HELD, Reservation.expires_at <= now)
        .update(
            {
                Reservation.status: RESERVATION_EXPIRED,
                Reservation.finalized_at: now,
            },
            synchronize_session=False,
        )
    )
    for station_id, count in due:
        db.query(Station).filter(Station.id == station_id).update(
            {Station.battery_held: Station.battery_held - count},
            synchronize_session=False,
        )
    return expired


def available_capacity(station: Station) -> int:
    """可预约量唯一口径：满电电池数 - 有效预约锁定数。"""
    return station.battery_ready - station.battery_held


def create_reservation(
    db: Session,
    *,
    vehicle_id: int,
    station_id: int,
    ttl_minutes: int,
    expected_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> tuple[Reservation, bool]:
    """创建预约并锁定一份容量。返回 (预约, 是否新建)。

    幂等：相同 idempotency_key 的重试直接返回已有预约，不重复占位。
    """
    now = _expire_and_commit(db)

    if idempotency_key:
        existing = (
            db.query(Reservation)
            .filter(Reservation.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing, False

    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise ReservationError(404, "车辆不存在")
    station = db.get(Station, station_id)
    if not station:
        raise ReservationError(404, "换电站不存在")
    if station.status != "running":
        raise ReservationError(422, "该换电站当前不处于运营状态，无法预约")

    overlap = (
        db.query(Reservation.id)
        .filter(
            Reservation.vehicle_id == vehicle_id,
            Reservation.status == RESERVATION_HELD,
        )
        .first()
    )
    if overlap:
        raise ReservationError(409, "该车辆已有一份有效预约，不能重复占位")

    # 条件更新做原子占位：仍有可预约容量时才锁定最后一份。
    # 写事务已被 BEGIN IMMEDIATE 串行化，并发争夺时一个成功、其余确定失败。
    locked = (
        db.query(Station)
        .filter(
            Station.id == station_id,
            Station.battery_ready - Station.battery_held > 0,
        )
        .update({Station.battery_held: Station.battery_held + 1}, synchronize_session=False)
    )
    if locked == 0:
        raise ReservationError(409, "该换电站可预约容量已满")

    reservation = Reservation(
        code=generate_code(),
        vehicle_id=vehicle_id,
        station_id=station_id,
        status=RESERVATION_HELD,
        idempotency_key=idempotency_key,
        expected_at=expected_at or now,
        expires_at=now + timedelta(minutes=ttl_minutes),
        created_at=now,
        renewed_at=now,
    )
    db.add(reservation)
    try:
        db.commit()
    except IntegrityError:
        # 并发下由唯一约束兜底（幂等键竞争等）：回滚占位并改走确定性结果
        db.rollback()
        if idempotency_key:
            existing = (
                db.query(Reservation)
                .filter(Reservation.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing, False
        raise ReservationError(409, "预约创建冲突，请重试")
    db.refresh(reservation)
    return reservation, True


def renew_reservation(db: Session, reservation_id: int, ttl_minutes: int) -> Reservation:
    """续期：从当前时刻重新计算到期时间。仅 held 预约可续期。"""
    now = _expire_and_commit(db)

    reservation = db.get(Reservation, reservation_id)
    if not reservation:
        raise ReservationError(404, "预约不存在")
    if reservation.status == RESERVATION_EXPIRED:
        raise ReservationError(410, "预约已过期，无法续期")
    if reservation.status == RESERVATION_CANCELLED:
        raise ReservationError(409, "预约已取消，无法续期")
    if reservation.status == RESERVATION_FULFILLED:
        raise ReservationError(409, "预约已履约，无法续期")

    reservation.expires_at = now + timedelta(minutes=ttl_minutes)
    reservation.renewed_at = now
    db.commit()
    db.refresh(reservation)
    return reservation


def cancel_reservation(db: Session, reservation_id: int) -> Reservation:
    """取消预约并归还容量。重复取消是幂等的。"""
    now = _expire_and_commit(db)

    reservation = db.get(Reservation, reservation_id)
    if not reservation:
        raise ReservationError(404, "预约不存在")
    if reservation.status == RESERVATION_CANCELLED:
        return reservation
    if reservation.status == RESERVATION_EXPIRED:
        raise ReservationError(410, "预约已过期，无需取消")
    if reservation.status == RESERVATION_FULFILLED:
        raise ReservationError(409, "预约已履约，无法取消")

    released = (
        db.query(Station)
        .filter(Station.id == reservation.station_id, Station.battery_held > 0)
        .update({Station.battery_held: Station.battery_held - 1}, synchronize_session=False)
    )
    if released == 0:  # 理论上不可能，CHECK 约束兜底
        raise ReservationError(409, "站点锁定容量状态异常，无法取消")

    reservation.status = RESERVATION_CANCELLED
    reservation.cancelled_at = now
    reservation.finalized_at = now
    db.commit()
    db.refresh(reservation)
    return reservation


def fulfill_reservation(
    db: Session,
    *,
    vehicle_id: int,
    station_id: int,
    reservation_code: str,
    soc_before: float,
    soc_after: float,
) -> SwapRecord:
    """凭有效预约完成换电。

    预约匹配校验、状态流转、库存扣减、换电记录写入、车辆电量更新
    全部在同一事务内原子提交。
    """
    now = _expire_and_commit(db)

    reservation = (
        db.query(Reservation).filter(Reservation.code == reservation_code).first()
    )
    if not reservation:
        raise ReservationError(404, "预约不存在")
    if reservation.status == RESERVATION_EXPIRED:
        raise ReservationError(410, "预约已过期，容量已释放")
    if reservation.status == RESERVATION_CANCELLED:
        raise ReservationError(409, "预约已取消，不能换电")
    if reservation.status == RESERVATION_FULFILLED:
        raise ReservationError(409, "该预约已使用，不能重复换电")
    if reservation.vehicle_id != vehicle_id:
        raise ReservationError(422, "预约与车辆不匹配")
    if reservation.station_id != station_id:
        raise ReservationError(422, "预约与换电站不匹配")
    if soc_after <= soc_before:
        raise ReservationError(422, "换电后电量应高于换电前电量")

    vehicle = db.get(Vehicle, vehicle_id)
    if not vehicle:
        raise ReservationError(404, "车辆不存在")
    station = db.get(Station, station_id)
    if not station:
        raise ReservationError(404, "换电站不存在")

    # 锁定容量转为实际扣减：ready 与 held 各减一。条件保证库存确实存在。
    consumed = (
        db.query(Station)
        .filter(
            Station.id == station_id,
            Station.battery_ready > 0,
            Station.battery_held > 0,
        )
        .update(
            {
                Station.battery_ready: Station.battery_ready - 1,
                Station.battery_held: Station.battery_held - 1,
            },
            synchronize_session=False,
        )
    )
    if consumed == 0:
        raise ReservationError(409, "该换电站暂无满电电池可换")

    reservation.status = RESERVATION_FULFILLED
    reservation.fulfilled_at = now
    reservation.finalized_at = now

    record = SwapRecord(
        vehicle_id=vehicle_id,
        station_id=station_id,
        reservation_id=reservation.id,
        soc_before=soc_before,
        soc_after=soc_after,
        swapped_at=now,
    )
    vehicle.current_soc = soc_after
    db.add(record)
    db.commit()
    db.refresh(record)
    return record
