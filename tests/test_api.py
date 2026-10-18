"""接口层测试：登录、借号、后台增删改查、鉴权边界。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pool import service
from pool.api import app, reset_rate_limits


@pytest.fixture
def client():
    reset_rate_limits()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    resp = client.post("/api/admin/login", json={"username": "admin", "password": "test-pass"})
    assert resp.status_code == 200
    return client


def slot(hour: int, days: int = 1):
    return (service.now() + timedelta(days=days)).replace(hour=hour, minute=0, second=0,
                                                          microsecond=0).isoformat()


def add_account(client, **kw):
    payload = {"label": "接口测试号", "driver": "mock", "tier": "pro", "max_minutes": 240,
               "daily_limit": 6, "capacity": 100}
    payload.update(kw)
    resp = client.post("/api/admin/accounts", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_static_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "会议号池" in resp.text


def test_login_rejects_bad_password(client):
    resp = client.post("/api/admin/login", json={"username": "admin", "password": "nope"})
    assert resp.status_code == 401


def test_admin_endpoints_require_login(client):
    for path in ("/api/admin/overview", "/api/admin/accounts", "/api/admin/bookings"):
        assert client.get(path).status_code == 401


def test_public_state_and_timeline(client, admin):
    add_account(admin)
    state = client.get("/api/state").json()
    assert state["accounts_active"] == 1
    assert state["max_minutes"] == 240
    tl = client.get("/api/timeline").json()
    assert tl["accounts"][0]["label"] == "接口测试号"
    assert client.get("/api/timeline?date=bad-date").status_code == 400


def test_full_booking_flow(client, admin):
    add_account(admin)
    resp = client.post("/api/bookings", json={
        "requester": "张三", "dept": "市场部", "subject": "接口借号",
        "start_at": slot(10), "duration_minutes": 60,
    })
    assert resp.status_code == 200, resp.text
    booking = resp.json()
    assert len(booking["meeting_code"]) == 9
    assert booking["share_text"]

    detail = client.get(f"/api/bookings/{booking['id']}?token={booking['manage_token']}")
    assert detail.status_code == 200
    assert client.get(f"/api/bookings/{booking['id']}?token=wrong").status_code == 403

    mine = client.post("/api/my", json={"tokens": [booking["manage_token"]]}).json()
    assert mine["items"][0]["id"] == booking["id"]

    cancelled = client.post(f"/api/bookings/{booking['id']}/cancel",
                            json={"token": booking["manage_token"]})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    again = client.post(f"/api/bookings/{booking['id']}/cancel",
                        json={"token": booking["manage_token"]})
    assert again.status_code == 409


def test_booking_conflict_returns_409(client, admin):
    add_account(admin)
    payload = {"requester": "张三", "subject": "冲突", "start_at": slot(11), "duration_minutes": 60}
    assert client.post("/api/bookings", json=payload).status_code == 200
    payload["requester"] = "李四"
    resp = client.post("/api/bookings", json=payload)
    assert resp.status_code == 409
    assert "满了" in resp.json()["detail"]


def test_validate_errors(client, admin):
    add_account(admin)
    bad = client.post("/api/bookings", json={"requester": "", "subject": "x",
                                             "start_at": slot(12), "duration_minutes": 30})
    assert bad.status_code == 409
    too_long = client.post("/api/bookings", json={"requester": "张三", "subject": "x",
                                                  "start_at": slot(12), "duration_minutes": 999})
    assert too_long.status_code == 409


def test_account_crud_and_probe(client, admin):
    acc = add_account(admin, label="待改号")
    acc_id = acc["id"]
    assert acc["has_token"] is False

    patched = admin.patch(f"/api/admin/accounts/{acc_id}", json={
        "label": "改后名", "driver": "mock", "tier": "biz", "status": "paused",
        "max_minutes": 120, "daily_limit": 3, "capacity": 200,
    })
    assert patched.status_code == 200
    assert patched.json()["label"] == "改后名"
    assert patched.json()["status"] == "paused"

    probe = admin.post(f"/api/admin/accounts/{acc_id}/probe")
    assert probe.status_code == 200
    assert probe.json()["health"] == "ok"

    assert admin.delete(f"/api/admin/accounts/{acc_id}").status_code == 200
    assert admin.get("/api/admin/accounts").json()["items"] == []


def test_mcp_account_without_token_rejected(client, admin):
    resp = admin.post("/api/admin/accounts", json={"label": "无Token", "driver": "mcp"})
    assert resp.status_code == 400


def test_config_update(client, admin):
    resp = admin.patch("/api/admin/config", json={"max_per_week": 2})
    assert resp.status_code == 200
    assert resp.json()["max_per_week"] == 2
    add_account(admin)
    assert admin.post("/api/bookings", json={"requester": "张三", "subject": "1",
                                             "start_at": slot(9), "duration_minutes": 30}).status_code == 200
    assert admin.post("/api/bookings", json={"requester": "张三", "subject": "2",
                                             "start_at": slot(10), "duration_minutes": 30}).status_code == 200
    third = admin.post("/api/bookings", json={"requester": "张三", "subject": "3",
                                              "start_at": slot(11), "duration_minutes": 30})
    assert third.status_code == 409
    assert "上限" in third.json()["detail"]


def test_admin_cancel_and_export(client, admin):
    add_account(admin)
    booking = admin.post("/api/bookings", json={"requester": "张三", "subject": "导出用",
                                                "start_at": slot(13), "duration_minutes": 30}).json()
    assert admin.post(f"/api/admin/bookings/{booking['id']}/cancel").status_code == 200
    csv_resp = admin.get("/api/admin/export.csv")
    assert csv_resp.status_code == 200
    assert "导出用" in csv_resp.text


def test_overview_and_audit(client, admin):
    add_account(admin)
    overview = admin.get("/api/admin/overview").json()
    assert overview["accounts_total"] == 1
    assert "hour_heat" in overview
    audit = admin.get("/api/admin/audit").json()
    assert any(item["action"] == "account.create" for item in audit["items"])


def test_logout(client, admin):
    assert admin.post("/api/admin/logout").status_code == 200
    assert client.get("/api/admin/overview").status_code == 401


def test_access_code_gate(client, monkeypatch):
    """设了口令后，员工端必须先过门；管理接口不受影响。"""
    from pool.config import SETTINGS

    monkeypatch.setattr(SETTINGS, "access_code", "team-2026")
    assert client.get("/").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/access", json={"code": "wrong"}).status_code == 401
    assert client.post("/api/admin/login",
                       json={"username": "admin", "password": "test-pass"}).status_code == 200
    assert client.post("/api/access", json={"code": "team-2026"}).status_code == 200
    assert client.get("/api/state").status_code == 200
