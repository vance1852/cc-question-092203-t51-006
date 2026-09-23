"""预约全状态流测试：创建、幂等重试、防重叠、续期、取消、过期、
重启识别、履约、并发争抢最后名额。使用固定时钟保证可复现。
"""
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.database import SessionLocal
from app.main import app
from app.models import RESERVATION_PENDING, Station, Vehicle
from app.seed import init_db
from app.services import reservation_service as svc

init_db()
client = TestClient(app)

T0 = clock.now()


@pytest.fixture(autouse=True)
def fixed_clock():
    # 每个测试从统一固定时刻开始，结束后还原
    clock.set_fixed(T0)
    yield
    clock.reset()


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _make_station(headers, ready: int) -> int:
    resp = client.post(
        "/api/stations",
        json={"name": f"预约测试站{uuid.uuid4().hex[:6]}", "slot_total": max(ready, 1), "battery_ready": ready},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_vehicle(headers) -> int:
    resp = client.post(
        "/api/vehicles",
        json={"plate": f"测{uuid.uuid4().hex[:8]}", "model": "预约测试车"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_create_locks_capacity():
    headers = _headers()
    sid = _make_station(headers, 3)
    vid = _make_vehicle(headers)

    cap = client.get(f"/api/stations/{sid}/capacity", headers=headers).json()
    assert cap["available"] == 3

    r = client.post("/api/reservations", json={"vehicle_id": vid, "station_id": sid}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    # 默认保留窗口 15 分钟
    assert (
        datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(body["reserved_for"])
    ).total_seconds() == 15 * 60

    cap = client.get(f"/api/stations/{sid}/capacity", headers=headers).json()
    assert cap["available"] == 2
    assert cap["reserved_count"] == 1
    # 运营列表看到的可预约量与容量接口一致
    listed = next(s for s in client.get("/api/stations", headers=headers).json() if s["id"] == sid)
    assert listed["available"] == 2


def test_idempotent_retry_does_not_double_lock():
    headers = _headers()
    sid = _make_station(headers, 2)
    vid = _make_vehicle(headers)
    key = f"idem-{uuid.uuid4().hex}"
    payload = {"vehicle_id": vid, "station_id": sid, "idempotency_key": key}

    first = client.post("/api/reservations", json=payload, headers=headers)
    assert first.status_code == 201
    second = client.post("/api/reservations", json=payload, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first.json()["id"]

    cap = client.get(f"/api/stations/{sid}/capacity", headers=headers).json()
    assert cap["reserved_count"] == 1  # 重试没有重复占位


def test_same_vehicle_overlap_rejected():
    headers = _headers()
    sid = _make_station(headers, 5)
    vid = _make_vehicle(headers)
    now = clock.now()
    r1 = client.post(
        "/api/reservations",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reserved_for": now.isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat(),
        },
        headers=headers,
    )
    assert r1.status_code == 201
    # 时间窗重叠的第二份预约应被拒绝
    r2 = client.post(
        "/api/reservations",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reserved_for": (now + timedelta(minutes=10)).isoformat(),
            "expires_at": (now + timedelta(minutes=30)).isoformat(),
        },
        headers=headers,
    )
    assert r2.status_code == 409
    # 不相邻、不重叠的窗口允许
    r3 = client.post(
        "/api/reservations",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reserved_for": (now + timedelta(minutes=21)).isoformat(),
            "expires_at": (now + timedelta(minutes=40)).isoformat(),
        },
        headers=headers,
    )
    assert r3.status_code == 201, r3.text


def test_capacity_exhaustion():
    headers = _headers()
    sid = _make_station(headers, 1)
    v1, v2 = _make_vehicle(headers), _make_vehicle(headers)
    assert client.post("/api/reservations", json={"vehicle_id": v1, "station_id": sid}, headers=headers).status_code == 201
    # 最后一份已被锁定，再来应失败且不产生占位
    second = client.post("/api/reservations", json={"vehicle_id": v2, "station_id": sid}, headers=headers)
    assert second.status_code == 409
    cap = client.get(f"/api/stations/{sid}/capacity", headers=headers).json()
    assert cap["available"] == 0
    assert cap["reserved_count"] == 1


def test_renew_extends_and_then_fulfill():
    headers = _headers()
    sid = _make_station(headers, 2)
    vid = _make_vehicle(headers)
    r = client.post("/api/reservations", json={"vehicle_id": vid, "station_id": sid}, headers=headers).json()
    original_expires = r["expires_at"]

    # 推进 14 分钟，续期 10 分钟，避免在第 15 分钟过期
    clock.advance(minutes=14)
    renewed = client.post(f"/api/reservations/{r['id']}/renew", json={"extend_minutes": 10}, headers=headers)
    assert renewed.status_code == 200, renewed.text
    assert datetime.fromisoformat(renewed.json()["expires_at"]) > datetime.fromisoformat(original_expires)

    # 到原到期时刻之后，预约因续期仍有效，可正常履约
    clock.advance(minutes=5)
    swap = client.post(
        "/api/swaps",
        json={"reservation_id": r["id"], "soc_before": 12.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 201, swap.text
    got = client.get(f"/api/reservations/{r['id']}", headers=headers).json()
    assert got["status"] == "fulfilled"
    assert got["closed_at"] is not None


def test_cancel_releases_capacity():
    headers = _headers()
    sid = _make_station(headers, 2)
    vid = _make_vehicle(headers)
    r = client.post("/api/reservations", json={"vehicle_id": vid, "station_id": sid}, headers=headers).json()
    assert client.get(f"/api/stations/{sid}/capacity", headers=headers).json()["available"] == 1

    cancelled = client.post(f"/api/reservations/{r['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.get(f"/api/stations/{sid}/capacity", headers=headers).json()["available"] == 2

    # 取消后不能再履约
    swap = client.post(
        "/api/swaps",
        json={"reservation_id": r["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 409
    # 重复取消保持幂等
    assert client.post(f"/api/reservations/{r['id']}/cancel", headers=headers).status_code == 200


def test_expiry_releases_capacity_and_restart_recognizes():
    headers = _headers()
    sid = _make_station(headers, 2)
    vid = _make_vehicle(headers)
    r = client.post("/api/reservations", json={"vehicle_id": vid, "station_id": sid}, headers=headers).json()
    assert client.get(f"/api/stations/{sid}/capacity", headers=headers).json()["available"] == 1

    # 超过到期时间
    clock.advance(minutes=16)

    # 模拟进程重启：新会话执行启动过期扫描
    db = SessionLocal()
    try:
        released = svc.sweep_expired(db, commit=True)
    finally:
        db.close()
    assert released == 1

    got = client.get(f"/api/reservations/{r['id']}", headers=headers).json()
    assert got["status"] == "expired"
    assert got["closed_at"] is not None
    # 名额被安全释放，可重新预约
    assert client.get(f"/api/stations/{sid}/capacity", headers=headers).json()["available"] == 2
    vid2 = _make_vehicle(headers)
    assert client.post("/api/reservations", json={"vehicle_id": vid2, "station_id": sid}, headers=headers).status_code == 201
    # 过期预约不能履约
    swap = client.post(
        "/api/swaps",
        json={"reservation_id": r["id"], "soc_before": 10.0, "soc_after": 100.0},
        headers=headers,
    )
    assert swap.status_code == 409


def test_list_by_status_and_time():
    headers = _headers()
    sid = _make_station(headers, 4)
    vid = _make_vehicle(headers)
    now = clock.now()
    r = client.post(
        "/api/reservations",
        json={
            "vehicle_id": vid,
            "station_id": sid,
            "reserved_for": (now + timedelta(hours=2)).isoformat(),
            "expires_at": (now + timedelta(hours=2, minutes=15)).isoformat(),
        },
        headers=headers,
    ).json()
    client.post(f"/api/reservations/{r['id']}/cancel", headers=headers)

    pending = client.get("/api/reservations?status=pending", headers=headers).json()
    cancelled = client.get(
        f"/api/reservations?status=cancelled&vehicle_id={vid}", headers=headers
    ).json()
    assert all(x["status"] == "cancelled" for x in cancelled)
    assert any(x["id"] == r["id"] for x in cancelled)
    assert all(x["id"] != r["id"] for x in pending)

    # 时间窗口查询
    in_window = client.get(
        f"/api/reservations?time_from={(now + timedelta(hours=1)).isoformat()}"
        f"&time_to={(now + timedelta(hours=3)).isoformat()}",
        headers=headers,
    ).json()
    assert any(x["id"] == r["id"] for x in in_window)
    out_window = client.get(
        f"/api/reservations?time_to={(now + timedelta(hours=1)).isoformat()}",
        headers=headers,
    ).json()
    assert all(x["id"] != r["id"] for x in out_window)


def test_concurrent_contention_for_last_slot():
    """10 个线程并发争抢最后 1 份容量：恰好一个成功，其余确定失败。"""
    db = SessionLocal()
    try:
        station = Station(name=f"并发站{uuid.uuid4().hex[:6]}", slot_total=1, battery_ready=1, reserved_count=0)
        db.add(station)
        db.commit()
        db.refresh(station)
        sid = station.id
        vehicle_ids = []
        for _ in range(10):
            v = Vehicle(plate=f"并{uuid.uuid4().hex[:10]}", model="并发测试车")
            db.add(v)
            db.flush()
            vehicle_ids.append(v.id)
        db.commit()
    finally:
        db.close()

    results = []
    errors = []
    barrier = threading.Barrier(10)

    def worker(vid: int):
        session = SessionLocal()
        try:
            barrier.wait()
            _, created = svc.create_reservation(
                session, vehicle_id=vid, station_id=sid,
                reserved_for=clock.now(), expires_at=clock.now() + timedelta(minutes=15),
            )
            results.append(created)
        except svc.ServiceError as exc:
            errors.append(exc.status_code)
        except Exception as exc:  # 不应出现数据库层面的异常
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(vid,)) for vid in vehicle_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [True]            # 恰好一个成功占位
    assert sorted(errors) == [409] * 9  # 其余全部容量不足

    check = SessionLocal()
    try:
        s = check.get(Station, sid)
        assert s.reserved_count == 1
        assert s.battery_ready - s.reserved_count == 0
    finally:
        check.close()


def test_concurrent_fulfill_same_reservation_consumes_once():
    """同一预约被并发履约：只有一次成功，电池只扣一块。"""
    headers = _headers()
    sid = _make_station(headers, 1)
    vid = _make_vehicle(headers)
    rid = client.post("/api/reservations", json={"vehicle_id": vid, "station_id": sid}, headers=headers).json()["id"]

    outcomes = []
    barrier = threading.Barrier(5)

    def worker():
        session = SessionLocal()
        try:
            barrier.wait()
            svc.fulfill_swap(session, reservation_id=rid, soc_before=10.0, soc_after=100.0)
            outcomes.append("ok")
        except svc.ServiceError as exc:
            outcomes.append(f"err{exc.status_code}")
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("err409") == 4
    check = SessionLocal()
    try:
        s = check.get(Station, sid)
        assert s.battery_ready == 0
        assert s.reserved_count == 0
    finally:
        check.close()
