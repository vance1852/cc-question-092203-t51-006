"""首次启动时初始化数据库：建表 + 内置管理员 + 种子业务数据。

启动时还会执行一次预约过期扫描，使服务重启后仍能正确识别
待履约（held）与已过期（expired）的预约并归还容量。
"""
from datetime import datetime, timedelta

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from .auth import hash_password
from .config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from .database import Base, SessionLocal, engine
from .models import Station, SwapRecord, User, Vehicle
from .services.reservations import expire_due_reservations


def init_db() -> None:
    """创建所有表、补齐旧库结构并灌入种子数据（幂等：已存在则跳过）。"""
    Base.metadata.create_all(bind=engine)
    _ensure_schema()
    db: Session = SessionLocal()
    try:
        _seed_admin(db)
        _seed_business(db)
        # 重启后立即识别在停机期间到期的预约，释放其锁定容量
        released = expire_due_reservations(db)
        db.commit()
        if released:
            import logging

            logging.getLogger("app.seed").info("启动扫描释放了 %s 份过期预约", released)
    finally:
        db.close()


def _ensure_schema() -> None:
    """为早期版本的 SQLite 库补齐新增列（create_all 不会 ALTER 既有表）。"""
    # 反射必须在开启写事务之前完成：inspect 会另借连接，写锁占用下会自死锁。
    inspector = inspect(engine)
    statements: list[str] = []
    tables = inspector.get_table_names()
    if "stations" in tables:
        columns = {col["name"] for col in inspector.get_columns("stations")}
        if "battery_held" not in columns:
            statements.append(
                "ALTER TABLE stations ADD COLUMN battery_held INTEGER NOT NULL DEFAULT 0"
            )
    if "swap_records" in tables:
        columns = {col["name"] for col in inspector.get_columns("swap_records")}
        if "reservation_id" not in columns:
            statements.append("ALTER TABLE swap_records ADD COLUMN reservation_id INTEGER")
    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))


def _seed_admin(db: Session) -> None:
    if db.query(User).filter(User.username == DEFAULT_ADMIN_USERNAME).first():
        return
    db.add(
        User(
            username=DEFAULT_ADMIN_USERNAME,
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            display_name="平台管理员",
        )
    )


def _seed_business(db: Session) -> None:
    if db.query(Station).count() > 0:
        return

    stations = [
        Station(name="城东物流园换电站", address="城东大道 128 号", slot_total=20, battery_ready=14, status="running"),
        Station(name="临港枢纽换电站", address="临港四路 9 号", slot_total=16, battery_ready=11, status="running"),
        Station(name="北郊配送中心换电站", address="北环高速出口 3 公里", slot_total=12, battery_ready=4, status="maintenance"),
        Station(name="高新园区换电站", address="科创路 66 号", slot_total=24, battery_ready=20, status="running"),
    ]
    db.add_all(stations)
    db.flush()

    vehicles = [
        Vehicle(plate="沪EV1234", model="远程星瀚 H", battery_capacity=141.0, current_soc=82.0, status="running"),
        Vehicle(plate="沪EV5678", model="比亚迪 T5", battery_capacity=100.0, current_soc=23.0, status="charging"),
        Vehicle(plate="苏EV9012", model="江淮恺达 EX8", battery_capacity=120.0, current_soc=56.0, status="idle"),
        Vehicle(plate="浙EV3456", model="开瑞优优 EV", battery_capacity=42.0, current_soc=9.0, status="fault"),
        Vehicle(plate="沪EV7788", model="远程星智 G", battery_capacity=160.0, current_soc=95.0, status="running"),
    ]
    db.add_all(vehicles)
    db.flush()

    now = datetime.utcnow()
    swaps = [
        SwapRecord(vehicle_id=vehicles[0].id, station_id=stations[0].id, soc_before=12.0, soc_after=100.0, swapped_at=now - timedelta(hours=2)),
        SwapRecord(vehicle_id=vehicles[1].id, station_id=stations[1].id, soc_before=8.0, soc_after=98.0, swapped_at=now - timedelta(hours=5)),
        SwapRecord(vehicle_id=vehicles[2].id, station_id=stations[0].id, soc_before=15.0, soc_after=100.0, swapped_at=now - timedelta(days=1, hours=1)),
        SwapRecord(vehicle_id=vehicles[4].id, station_id=stations[3].id, soc_before=20.0, soc_after=100.0, swapped_at=now - timedelta(minutes=40)),
    ]
    db.add_all(swaps)
