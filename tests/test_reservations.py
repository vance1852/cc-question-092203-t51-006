"""预约完整状态流测试：创建、续期、取消、过期、履约、幂等、重启识别与并发争用。

使用固定时钟（app.clock）确定性地驱动时间相关流转；
并发争用直接在多个线程/会话上调用服务层，验证最后一份名额结果确定。
"""
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.clock import clock
from app.config import DATABASE_URL
from app.database import get_db
from app.main import app
from app.models import RESERVATION_HELD, Station, Vehicle
from app.seed import init_db
from app.services import reservations as service

client = TestClient(app)


@pytest.fixture
def auth():
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def station_factory():
    """直接用独立会话构造一个容量确定的运营站与车辆（HTTP 无法写 held）。"""
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)

    def _make(name: str, ready: int, held: int = 0, slots: int | None = None):
        db = Session()
        station = Station(
            name=f"{name}-{uuid.uuid4().hex[:8]}",
            address="测试",
            slot_total=slots if slots is not None else max(ready, held),
            battery_ready=ready,
            battery_held=held,
            status="running",
        )
        db.add(station)
        db.commit()
        db.refresh(station)
        sid = station.id
        db.close()
        return sid

    return _make


@pytest.fixture
def vehicle_factory():
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
    Session = sessionmaker(bind=engine)
    def _make():
        db = Session()
        vehicle = Vehicle(plate=f"测约{uuid.uuid4().hex[:10]}", model="测试车")
        db.add(vehicle)
        db.commit()
        db.refresh(vehicle)
        vid = vehicle.id
        db.close()
        return vid

    return _make


def _station_view(auth, station_id: int) -> dict:
    return client.get(f"/api/stations/{station_id}", headers=auth).json()


def _capacity(auth, station_id: int) -> int:
    return client.get(f"/api/stations/{station_id}/capacity", headers=auth).json()["available_capacity"]


# ---------- 创建与容量锁定 ----------
def test_create_locks_capacity(auth, station_factory, vehicle_factory):
    sid = station_factory("预约锁定站", ready=3)
    vid = vehicle_factory()
    assert _capacity(auth, sid) == 3

    resp = client.post(
        "/api/reservations",
        json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "held"
    assert data["code"].startswith("RSV")

    view = _station_view(auth, sid)
    # 锁定后 ready 不变、held +1、可预约量 -1
    assert view["battery_ready"] == 3
    assert view["battery_held"] == 1
    assert view["available_capacity"] == 2
    assert _capacity(auth, sid) == 2


def test_create_rejects_when_full(auth, station_factory, vehicle_factory):
    sid = station_factory("满载预约站", ready=2, held=2)
    vid = vehicle_factory()
    resp = client.post(
        "/api/reservations",
        json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15},
        headers=auth,
    )
    assert resp.status_code == 409
    assert "容量" in resp.json()["detail"]
    # 失败不得占位
    assert _station_view(auth, sid)["battery_held"] == 2


def test_create_rejects_non_running_station(auth, station_factory, vehicle_factory):
    # 直接建维护站
    from app.database import SessionLocal

    db = SessionLocal()
    st = Station(name="维护中站", slot_total=5, battery_ready=5, status="maintenance")
    db.add(st)
    db.commit()
    sid = st.id
    db.close()
    vid = vehicle_factory()
    resp = client.post(
        "/api/reservations",
        json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15},
        headers=auth,
    )
    assert resp.status_code == 422


def test_overlapping_vehicle_reservation_rejected(auth, station_factory, vehicle_factory):
    sid = station_factory("重叠预约站", ready=5)
    vid = vehicle_factory()
    first = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    )
    assert first.status_code == 201
    second = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    )
    assert second.status_code == 409
    # 第二次失败不占位：held 仍为 1
    assert _station_view(auth, sid)["battery_held"] == 1


# ---------- 幂等 ----------
def test_idempotent_retry_returns_same_reservation(auth, station_factory, vehicle_factory):
    sid = station_factory("幂等站", ready=3)
    vid = vehicle_factory()
    payload = {"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15, "idempotency_key": "key-001"}
    first = client.post("/api/reservations", json=payload, headers=auth)
    second = client.post("/api/reservations", json=payload, headers=auth)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    # 重试不重复占位
    assert _station_view(auth, sid)["battery_held"] == 1


def test_idempotency_key_unique_across_requests(auth, station_factory, vehicle_factory):
    sid = station_factory("幂等冲突站", ready=3)
    v1, v2 = vehicle_factory(), vehicle_factory()
    first = client.post(
        "/api/reservations",
        json={"vehicle_id": v1, "station_id": sid, "ttl_minutes": 15, "idempotency_key": "shared-key"},
        headers=auth,
    )
    assert first.status_code == 201
    # 不同车辆复用同一幂等键：返回首份预约而不是再占位（调用方重试语义）
    second = client.post(
        "/api/reservations",
        json={"vehicle_id": v2, "station_id": sid, "ttl_minutes": 15, "idempotency_key": "shared-key"},
        headers=auth,
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert _station_view(auth, sid)["battery_held"] == 1


# ---------- 取消 ----------
def test_cancel_releases_capacity(auth, station_factory, vehicle_factory):
    sid = station_factory("取消释放站", ready=3)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    ).json()
    assert _capacity(auth, sid) == 2

    cancelled = client.post(f"/api/reservations/{rsv['id']}/cancel", headers=auth)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancelled_at"] is not None
    assert _capacity(auth, sid) == 3

    # 取消后同车可以再次预约（名额已释放）
    again = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    )
    assert again.status_code == 201
    assert again.json()["id"] != rsv["id"]


def test_cancel_idempotent_and_terminal_guards(auth, station_factory, vehicle_factory):
    sid = station_factory("取消幂等站", ready=3)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    ).json()
    first = client.post(f"/api/reservations/{rsv['id']}/cancel", headers=auth)
    second = client.post(f"/api/reservations/{rsv['id']}/cancel", headers=auth)
    assert first.status_code == 200 and second.status_code == 200
    # 重复取消不会二次归还容量
    assert _station_view(auth, sid)["battery_held"] == 0
    # 已取消的预约不能履约
    swap = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reservation_code": rsv["code"],
            "soc_before": 10.0,
            "soc_after": 100.0,
        },
        headers=auth,
    )
    assert swap.status_code == 409


# ---------- 续期与过期（固定时钟） ----------
def test_renew_extends_expiry(auth, station_factory, vehicle_factory):
    t0 = datetime(2026, 9, 23, 8, 0, 0)
    clock.freeze(t0)
    sid = station_factory("续期站", ready=2)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 10}, headers=auth
    ).json()
    assert rsv["expires_at"].endswith("08:10:00")

    clock.tick(minutes=9)
    renewed = client.post(f"/api/reservations/{rsv['id']}/renew", json={"ttl_minutes": 10}, headers=auth)
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"].endswith("08:19:00")

    # 原到期时刻 08:10 已过，但续期后仍 held
    clock.tick(minutes=2)  # 08:11
    detail = client.get(f"/api/reservations/{rsv['id']}", headers=auth).json()
    assert detail["status"] == "held"


def test_expiry_releases_capacity(auth, station_factory, vehicle_factory):
    t0 = datetime(2026, 9, 23, 9, 0, 0)
    clock.freeze(t0)
    sid = station_factory("过期释放站", ready=2)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 10}, headers=auth
    ).json()
    assert _capacity(auth, sid) == 1

    # 到期前不释放
    clock.tick(minutes=10)  # 恰好到期（expires_at <= now）
    detail = client.get(f"/api/reservations/{rsv['id']}", headers=auth).json()
    assert detail["status"] == "expired"
    assert _capacity(auth, sid) == 2
    # 过期后不能履约
    swap = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reservation_code": rsv["code"],
            "soc_before": 5.0,
            "soc_after": 100.0,
        },
        headers=auth,
    )
    assert swap.status_code == 410
    # 过期后不能续期，但同车可重新预约
    renew = client.post(f"/api/reservations/{rsv['id']}/renew", json={"ttl_minutes": 10}, headers=auth)
    assert renew.status_code == 410
    again = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 10}, headers=auth
    )
    assert again.status_code == 201


def test_lazy_expiry_in_list_query(auth, station_factory, vehicle_factory):
    t0 = datetime(2026, 9, 23, 10, 0, 0)
    clock.freeze(t0)
    sid = station_factory("惰性过期站", ready=2)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 5}, headers=auth
    ).json()
    clock.tick(minutes=6)
    held_list = client.get("/api/reservations", params={"status": "held"}, headers=auth).json()
    assert all(r["id"] != rsv["id"] for r in held_list)
    expired_list = client.get("/api/reservations", params={"status": "expired"}, headers=auth).json()
    assert any(r["id"] == rsv["id"] for r in expired_list)


def test_sweep_endpoint(auth, station_factory, vehicle_factory):
    t0 = datetime(2026, 9, 23, 11, 0, 0)
    clock.freeze(t0)
    sid = station_factory("扫描站", ready=2)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 1}, headers=auth
    ).json()
    clock.tick(minutes=2)
    resp = client.post("/api/reservations/expire-sweep", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["released"] >= 1
    detail = client.get(f"/api/reservations/{rsv['id']}", headers=auth).json()
    assert detail["status"] == "expired"


# ---------- 履约 ----------
def test_fulfill_consumes_reservation_atomically(auth, station_factory, vehicle_factory):
    sid = station_factory("履约站", ready=2)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    ).json()

    swap = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reservation_code": rsv["code"],
            "soc_before": 12.0,
            "soc_after": 100.0,
        },
        headers=auth,
    )
    assert swap.status_code == 201, swap.text
    swap_data = swap.json()
    assert swap_data["reservation_id"] == rsv["id"]

    detail = client.get(f"/api/reservations/{rsv['id']}", headers=auth).json()
    assert detail["status"] == "fulfilled"
    assert detail["fulfilled_at"] is not None

    view = _station_view(auth, sid)
    assert view["battery_ready"] == 1  # 实际扣减
    assert view["battery_held"] == 0  # 锁定转出
    assert view["available_capacity"] == 1

    # 换电记录可按预约关联查到
    swaps = client.get("/api/swaps", headers=auth).json()
    linked = next(s for s in swaps if s["reservation_id"] == rsv["id"])
    assert linked["vehicle_id"] == vid and linked["station_id"] == sid

    # 同一预约不能重复消费
    replay = client.post(
        "/api/swaps",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reservation_code": rsv["code"],
            "soc_before": 12.0,
            "soc_after": 100.0,
        },
        headers=auth,
    )
    assert replay.status_code == 409


def test_fulfill_requires_matching_vehicle_and_station(auth, station_factory, vehicle_factory):
    s1 = station_factory("履约匹配站A", ready=2)
    s2 = station_factory("履约匹配站B", ready=2)
    v1, v2 = vehicle_factory(), vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": v1, "station_id": s1, "ttl_minutes": 15}, headers=auth
    ).json()

    wrong_vehicle = client.post(
        "/api/swaps",
        json={"vehicle_id": v2, "station_id": s1, "reservation_code": rsv["code"], "soc_before": 10, "soc_after": 100},
        headers=auth,
    )
    assert wrong_vehicle.status_code == 422
    wrong_station = client.post(
        "/api/swaps",
        json={"vehicle_id": v1, "station_id": s2, "reservation_code": rsv["code"], "soc_before": 10, "soc_after": 100},
        headers=auth,
    )
    assert wrong_station.status_code == 422
    # 匹配失败不扣库存、不转状态
    assert client.get(f"/api/reservations/{rsv['id']}", headers=auth).json()["status"] == "held"
    assert _station_view(auth, s1)["battery_held"] == 1


def test_swap_requires_reservation(auth, station_factory, vehicle_factory):
    sid = station_factory("无约换电站", ready=2)
    vid = vehicle_factory()
    resp = client.post(
        "/api/swaps",
        json={"vehicle_id": vid, "station_id": sid, "reservation_code": "RSVNOTEXIST", "soc_before": 10, "soc_after": 100},
        headers=auth,
    )
    assert resp.status_code == 404


# ---------- 列表查询 ----------
def test_list_filters_by_status_and_time(auth, station_factory, vehicle_factory):
    t0 = datetime(2026, 9, 23, 12, 0, 0)
    clock.freeze(t0)
    sid = station_factory("查询站", ready=5)
    vid = vehicle_factory()
    rsv = client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 10}, headers=auth
    ).json()

    by_vehicle = client.get("/api/reservations", params={"vehicle_id": vid}, headers=auth).json()
    assert len(by_vehicle) == 1
    by_station = client.get("/api/reservations", params={"station_id": sid}, headers=auth).json()
    assert len(by_station) == 1

    # 时间范围：创建时刻在窗口外则查不到
    out_of_window = client.get(
        "/api/reservations",
        params={"created_from": (t0 + timedelta(hours=1)).isoformat()},
        headers=auth,
    ).json()
    assert all(r["id"] != rsv["id"] for r in out_of_window)
    in_window = client.get(
        "/api/reservations",
        params={"created_from": (t0 - timedelta(minutes=1)).isoformat()},
        headers=auth,
    ).json()
    assert any(r["id"] == rsv["id"] for r in in_window)

    # active 是 held 的别名
    active = client.get("/api/reservations", params={"status": "active", "vehicle_id": vid}, headers=auth).json()
    assert [r["id"] for r in active] == [rsv["id"]]

    # 非法状态过滤
    bad = client.get("/api/reservations", params={"status": "nope"}, headers=auth)
    assert bad.status_code == 422


# ---------- 重启后识别 ----------
def test_restart_recognizes_held_and_expired(auth, station_factory, vehicle_factory, monkeypatch):
    t0 = datetime(2026, 9, 23, 13, 0, 0)
    clock.freeze(t0)
    sid = station_factory("重启识别站", ready=3)
    v1, v2 = vehicle_factory(), vehicle_factory()
    live = client.post(
        "/api/reservations", json={"vehicle_id": v1, "station_id": sid, "ttl_minutes": 30}, headers=auth
    ).json()
    stale = client.post(
        "/api/reservations", json={"vehicle_id": v2, "station_id": sid, "ttl_minutes": 10}, headers=auth
    ).json()

    # 模拟停机后又过了 20 分钟再启动：init_db 的启动扫描应释放过期预约
    clock.freeze(t0 + timedelta(minutes=20))
    init_db()

    live_view = client.get(f"/api/reservations/{live['id']}", headers=auth).json()
    stale_view = client.get(f"/api/reservations/{stale['id']}", headers=auth).json()
    assert live_view["status"] == "held"
    assert stale_view["status"] == "expired"

    view = _station_view(auth, sid)
    # 只剩 live 一份锁定
    assert view["battery_held"] == 1
    assert view["available_capacity"] == 2


# ---------- 并发争用 ----------
def test_concurrent_contention_last_slot_deterministic(station_factory, vehicle_factory):
    sid = station_factory("并发争夺站", ready=1)
    n = 8
    vids = [vehicle_factory() for _ in range(n)]

    from app.database import SessionLocal

    results: list[bool] = []
    barrier = threading.Barrier(n)

    def worker(vid: int):
        barrier.wait()
        db = SessionLocal()
        try:
            _rsv, created = service.create_reservation(
                db, vehicle_id=vid, station_id=sid, ttl_minutes=15
            )
            results.append(created)
        except service.ReservationError:
            results.append(False)
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(vid,)) for vid in vids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n
    assert results.count(True) == 1, "恰好一个请求抢到最后名额"
    assert results.count(False) == n - 1

    db = SessionLocal()
    station = db.get(Station, sid)
    assert station.battery_held == 1
    assert station.battery_ready - station.battery_held == 0
    db.close()


def test_concurrent_fulfill_same_reservation_consumed_once(station_factory, vehicle_factory):
    from app.database import SessionLocal

    sid = station_factory("并发履约站", ready=1)
    vid = vehicle_factory()
    db0 = SessionLocal()
    rsv, _ = service.create_reservation(db0, vehicle_id=vid, station_id=sid, ttl_minutes=15)
    code = rsv.code
    db0.close()

    n = 6
    outcomes: list[str] = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        db = SessionLocal()
        try:
            service.fulfill_reservation(
                db,
                vehicle_id=vid,
                station_id=sid,
                reservation_code=code,
                soc_before=10.0,
                soc_after=100.0,
            )
            outcomes.append("ok")
        except service.ReservationError:
            outcomes.append("rejected")
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("rejected") == n - 1
    db = SessionLocal()
    station = db.get(Station, sid)
    assert station.battery_ready == 0
    assert station.battery_held == 0
    db.close()


def test_concurrent_idempotency_retry_holds_once(station_factory, vehicle_factory):
    """多线程同时用相同幂等键创建：要么同一份预约，要么冲突，绝不重复占位。"""
    from app.database import SessionLocal

    sid = station_factory("并发幂等站", ready=3)
    vid = vehicle_factory()
    n = 5
    ids: list[int] = []
    errors: list[int] = []
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        db = SessionLocal()
        try:
            rsv, _created = service.create_reservation(
                db, vehicle_id=vid, station_id=sid, ttl_minutes=15, idempotency_key="dup-key-1"
            )
            ids.append(rsv.id)
        except service.ReservationError as exc:
            errors.append(exc.status_code)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) + len(errors) == n
    # 所有成功的请求拿到的是同一个预约
    assert set(ids) <= {ids[0]} if ids else True
    db = SessionLocal()
    assert db.get(Station, sid).battery_held == 1
    db.close()


def test_background_sweeper_releases_expired(station_factory, vehicle_factory):
    """后台线程在到期后自动释放容量，无需人工触发。"""
    from app.database import SessionLocal
    from app.sweeper import ReservationSweeper

    t0 = datetime(2026, 9, 23, 14, 0, 0)
    clock.freeze(t0)
    sid = station_factory("后台扫描站", ready=2)
    vid = vehicle_factory()
    db = SessionLocal()
    rsv, _ = service.create_reservation(db, vehicle_id=vid, station_id=sid, ttl_minutes=1)
    rid = rsv.id
    db.close()

    sweeper = ReservationSweeper(interval_seconds=1)
    clock.tick(minutes=2)
    sweeper.start()
    try:
        # 时钟已固定，这里以真实时间作为等待上限
        deadline_real = time.monotonic() + 8
        while time.monotonic() < deadline_real:
            db = SessionLocal()
            status_val = db.get(service.Reservation, rid).status
            held = db.get(Station, sid).battery_held
            db.close()
            if status_val == "expired" and held == 0:
                break
            time.sleep(0.1)
        assert status_val == "expired"
        assert held == 0
    finally:
        sweeper.stop()


def test_dashboard_includes_held_count(auth, station_factory, vehicle_factory):
    sid = station_factory("仪表盘锁定站", ready=3)
    vid = vehicle_factory()
    client.post(
        "/api/reservations", json={"vehicle_id": vid, "station_id": sid, "ttl_minutes": 15}, headers=auth
    )
    stats = client.get("/api/dashboard/stats", headers=auth).json()
    assert stats["battery_held_total"] >= 1
    assert stats["reservation_active"] >= 1
