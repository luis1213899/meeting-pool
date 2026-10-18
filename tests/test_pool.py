"""号池核心逻辑测试。"""
from __future__ import annotations

import base64
from datetime import timedelta

import pytest

from pool import service
from pool.db import connect, get_settings
from pool.security import decrypt_secret, encrypt_secret, mask_secret, verify_password, hash_password


def tomorrow(hour: int, minute: int = 0):
    base = (service.now() + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base


def make_account(**kw):
    payload = {
        "label": kw.get("label", "测试号A"),
        "owner_name": "老王",
        "tier": kw.get("tier", "pro"),
        "driver": kw.get("driver", "mock"),
        "max_minutes": kw.get("max_minutes", 240),
        "daily_limit": kw.get("daily_limit", 6),
        "status": kw.get("status", "active"),
        "fixed_code": kw.get("fixed_code", ""),
        "token": kw.get("token", ""),
    }
    conn = connect()
    try:
        acc = service.save_account(conn, payload)
        acc.pop("token_cipher", None)
        return acc
    finally:
        conn.close()


# --------------------------------------------------------------------- 安全


def test_token_roundtrip_and_mask():
    cipher = encrypt_secret("token-abcdef123456")
    assert cipher.startswith("v1:")
    assert "token-abcdef" not in cipher
    assert decrypt_secret(cipher) == "token-abcdef123456"
    assert mask_secret("token-abcdef123456") == "••••3456"


def test_password_hash():
    stored = hash_password("hello-pool")
    assert verify_password("hello-pool", stored)
    assert not verify_password("wrong", stored)


# --------------------------------------------------------------------- 账号


def test_mcp_account_requires_token():
    conn = connect()
    try:
        with pytest.raises(ValueError):
            service.save_account(conn, {"label": "无Token", "driver": "mcp"})
    finally:
        conn.close()


def test_manual_account_requires_code():
    conn = connect()
    try:
        with pytest.raises(ValueError):
            service.save_account(conn, {"label": "固定号", "driver": "manual"})
    finally:
        conn.close()


def test_account_token_is_encrypted_and_never_returned(conn):
    acc = service.save_account(conn, {"label": "市场号", "driver": "mcp", "token": "secret-token-xyz"})
    row = conn.execute("SELECT token_cipher, token_hint FROM accounts WHERE id=?", (acc["id"],)).fetchone()
    assert row["token_cipher"].startswith("v1:")
    assert row["token_hint"] == "••••-xyz"
    assert "token_cipher" not in service.list_accounts(conn)[0]


# --------------------------------------------------------------------- 借号


def test_booking_allocates_and_returns_code():
    acc = make_account()
    start = tomorrow(10)
    booking = service.create_booking({
        "requester": "张三", "subject": "客户沟通",
        "start_at": start.isoformat(), "duration_minutes": 60,
    })
    assert booking["account_id"] == acc["id"]
    assert len(booking["meeting_code"]) == 9
    assert booking["join_url"].endswith(booking["meeting_code"])
    assert booking["password"] == ""          # 默认不设密码
    assert booking["record"] is True          # 默认开录制
    assert "入会密码" not in booking["share_text"]
    assert "客户沟通" in booking["share_text"]
    assert booking["manage_token"]


def test_same_slot_second_request_rejected_when_single_account():
    make_account()
    start = tomorrow(10)
    payload = {"requester": "张三", "subject": "会A", "start_at": start.isoformat(), "duration_minutes": 60}
    service.create_booking(payload)
    with pytest.raises(ValueError) as exc:
        service.create_booking({**payload, "requester": "李四", "subject": "会B"})
    assert "满了" in str(exc.value)


def test_buffer_prevents_back_to_back_overlap():
    make_account()
    start = tomorrow(10)
    service.create_booking({"requester": "张三", "subject": "会A", "start_at": start.isoformat(),
                            "duration_minutes": 60})
    # 10:00-11:00 已占，11:02 开始仍落在 5 分钟缓冲内
    tight = start.replace(hour=11, minute=2)
    with pytest.raises(ValueError):
        service.create_booking({"requester": "李四", "subject": "会B", "start_at": tight.isoformat(),
                                "duration_minutes": 30})
    # 11:06 之后可以
    later = start.replace(hour=11, minute=6)
    booking = service.create_booking({"requester": "李四", "subject": "会C",
                                      "start_at": later.isoformat(), "duration_minutes": 30})
    assert booking["meeting_code"]


def test_load_balancing_prefers_idle_account():
    a = make_account(label="号A")
    b = make_account(label="号B")
    start = tomorrow(9)
    first = service.create_booking({"requester": "张三", "subject": "会A",
                                    "start_at": start.isoformat(), "duration_minutes": 60})
    second = service.create_booking({"requester": "李四", "subject": "会B",
                                     "start_at": start.isoformat(), "duration_minutes": 60})
    assert first["account_id"] != second["account_id"]
    assert {first["account_id"], second["account_id"]} == {a["id"], b["id"]}


def test_daily_limit_blocks_further_booking():
    make_account(daily_limit=2)
    for i in range(2):
        start = tomorrow(9 + i * 2)
        service.create_booking({"requester": f"员工{i}", "subject": f"会{i}",
                                "start_at": start.isoformat(), "duration_minutes": 30})
    with pytest.raises(ValueError):
        service.create_booking({"requester": "员工X", "subject": "会X",
                                "start_at": tomorrow(15).isoformat(), "duration_minutes": 30})


def test_week_quota_per_person():
    make_account(label="号A")
    make_account(label="号B")
    conn = connect()
    try:
        cfg = get_settings(conn)
    finally:
        conn.close()
    for i in range(cfg["max_per_week"]):
        service.create_booking({"requester": "高频用户", "subject": f"会{i}",
                                "start_at": tomorrow(8 + i).isoformat(), "duration_minutes": 30})
    with pytest.raises(ValueError) as exc:
        service.create_booking({"requester": "高频用户", "subject": "超额",
                                "start_at": tomorrow(20).isoformat(), "duration_minutes": 30})
    assert "上限" in str(exc.value)


def test_duration_limits():
    make_account(max_minutes=120)
    with pytest.raises(ValueError):
        service.create_booking({"requester": "张三", "subject": "太长",
                                "start_at": tomorrow(10).isoformat(), "duration_minutes": 180})
    with pytest.raises(ValueError):
        service.create_booking({"requester": "张三", "subject": "太短",
                                "start_at": tomorrow(10).isoformat(), "duration_minutes": 5})


def test_past_time_rejected():
    make_account()
    past = service.now() - timedelta(hours=2)
    with pytest.raises(ValueError):
        service.create_booking({"requester": "张三", "subject": "过去",
                                "start_at": past.isoformat(), "duration_minutes": 60})


def test_out_of_working_hours_rejected():
    make_account()
    start = tomorrow(3)
    with pytest.raises(ValueError) as exc:
        service.create_booking({"requester": "张三", "subject": "凌晨",
                                "start_at": start.isoformat(), "duration_minutes": 30})
    assert "可借时段" in str(exc.value)


def test_cancel_releases_slot():
    make_account()
    start = tomorrow(14)
    booking = service.create_booking({"requester": "张三", "subject": "会A",
                                      "start_at": start.isoformat(), "duration_minutes": 60})
    service.cancel_booking(booking["id"], token=booking["manage_token"])
    again = service.create_booking({"requester": "李四", "subject": "会B",
                                    "start_at": start.isoformat(), "duration_minutes": 60})
    assert again["meeting_code"]


def test_cancel_requires_manage_token():
    make_account()
    booking = service.create_booking({"requester": "张三", "subject": "会A",
                                      "start_at": tomorrow(14).isoformat(), "duration_minutes": 60})
    with pytest.raises(PermissionError):
        service.cancel_booking(booking["id"], token="bad-token")
    assert service.get_booking(booking["id"], booking["manage_token"])["status"] == "pending"


def test_manual_driver_uses_fixed_code():
    make_account(driver="manual", fixed_code="826341057")
    booking = service.create_booking({"requester": "张三", "subject": "固定号会",
                                      "start_at": tomorrow(16).isoformat(), "duration_minutes": 45})
    assert booking["meeting_code"] == "826341057"
    assert booking["driver"] == "manual"


def test_paused_account_not_allocated():
    make_account(label="停用号", status="paused")
    with pytest.raises(ValueError):
        service.create_booking({"requester": "张三", "subject": "没人",
                                "start_at": tomorrow(11).isoformat(), "duration_minutes": 30})


def test_driver_failure_falls_back_to_next_account(monkeypatch):
    make_account(label="坏号")
    make_account(label="好号")
    from pool import drivers

    original = drivers.MockDriver.create
    calls = {"n": 0}

    def flaky(self, account, token, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise drivers.DriverError("模拟上游超时")
        return original(self, account, token, payload)

    monkeypatch.setattr(drivers.MockDriver, "create", flaky)
    booking = service.create_booking({"requester": "张三", "subject": "容灾",
                                      "start_at": tomorrow(12).isoformat(), "duration_minutes": 30})
    assert booking["meeting_code"]
    assert calls["n"] == 2


# --------------------------------------------------------------------- 看板


def test_reap_promotes_and_closes():
    make_account()
    # 让用例与本地时间无关：临时把可借时段放宽到全天
    conn = connect()
    try:
        from pool.db import update_settings
        update_settings(conn, {"work_hour_start": 0, "work_hour_end": 24})
    finally:
        conn.close()
    start = service.now() + timedelta(minutes=1)
    booking = service.create_booking({"requester": "张三", "subject": "马上开始",
                                      "start_at": start.isoformat(), "duration_minutes": 15})
    assert booking["status"] == "pending"
    assert service.reap() >= 0


def test_timeline_and_overview_shapes():
    make_account()
    service.create_booking({"requester": "张三", "subject": "会A",
                            "start_at": tomorrow(10).isoformat(), "duration_minutes": 60})
    timeline = service.day_timeline(tomorrow(10).date().isoformat())
    assert timeline["accounts"][0]["blocks"]
    overview = service.admin_overview()
    assert overview["accounts_total"] == 1
    assert overview["week_bookings"] == 1
    assert overview["hour_heat"]
    assert service.public_state()["accounts_active"] == 1


def test_csv_export_contains_booking():
    make_account()
    service.create_booking({"requester": "张三", "subject": "导出测试",
                            "start_at": tomorrow(10).isoformat(), "duration_minutes": 60})
    csv_text = service.export_bookings_csv()
    assert "导出测试" in csv_text and "预约ID" in csv_text


# --------------------------------------------------------------------- 主持人能力


def test_password_only_when_requested():
    make_account()
    start = tomorrow(11)
    plain = service.create_booking({"requester": "张三", "subject": "无密码",
                                    "start_at": start.isoformat(), "duration_minutes": 30})
    assert plain["password"] == ""

    with_pass = service.create_booking({
        "requester": "李四", "subject": "有密码",
        "start_at": (start + timedelta(hours=2)).isoformat(), "duration_minutes": 30,
        "password_enabled": True, "password": "246810",
    })
    assert with_pass["password"] == "246810"
    assert "入会密码：246810" in with_pass["share_text"]


def test_bad_password_rejected():
    make_account()
    with pytest.raises(ValueError) as exc:
        service.create_booking({"requester": "张三", "subject": "密码不对",
                                "start_at": tomorrow(12).isoformat(), "duration_minutes": 30,
                                "password_enabled": True, "password": "ab"})
    assert "4–6 位数字" in str(exc.value)


def test_record_and_waiting_room_persisted():
    make_account()
    booking = service.create_booking({
        "requester": "张三", "subject": "录屏会",
        "start_at": tomorrow(13).isoformat(), "duration_minutes": 30,
        "record": False, "waiting_room": True,
    })
    assert booking["record"] is False
    assert booking["waiting_room"] is True


def test_delete_account_with_history_is_refused():
    acc = make_account()
    booking = service.create_booking({"requester": "张三", "subject": "留痕",
                                      "start_at": tomorrow(14).isoformat(), "duration_minutes": 30})
    service.cancel_booking(booking["id"], token=booking["manage_token"])  # 变成已结束的历史记录
    conn = connect()
    try:
        with pytest.raises(ValueError) as exc:
            service.delete_account(conn, acc["id"])
        assert "暂停" in str(exc.value)
    finally:
        conn.close()


def test_delete_clean_account_works():
    acc = make_account()
    conn = connect()
    try:
        service.delete_account(conn, acc["id"])
        assert conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"] == 0
    finally:
        conn.close()
