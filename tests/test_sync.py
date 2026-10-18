"""对账相关：上游会议导入、冲突、取消必须真实生效。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from pool import service
from pool.db import connect
from pool.drivers import DriverError, McpDriver, MeetingResult


def tomorrow(hour: int, minute: int = 0):
    return (service.now() + timedelta(days=1)).replace(hour=hour, minute=minute, second=0,
                                                       microsecond=0)


def mcp_account(**kw):
    conn = connect()
    try:
        acc = service.save_account(conn, {
            "label": kw.get("label", "MCP 号"),
            "driver": "mcp",
            "token": "tok-test",
            "max_minutes": 240,
            "daily_limit": 6,
        })
        acc.pop("token_cipher", None)
        return acc
    finally:
        conn.close()


def fake_create(code: str, meeting_id: str):
    def _create(self, account, token, payload):
        return MeetingResult(meeting_code=code, meeting_id=meeting_id,
                             join_url=f"https://meeting.tencent.com/dm/{code}",
                             password=payload.get("password", ""))
    return _create


def upstream(meetings):
    return lambda self, token, **kw: meetings


# --------------------------------------------------------------- 同步与可见性


def test_sync_imports_external_meetings(monkeypatch):
    acc = mcp_account()
    start = tomorrow(10)
    monkeypatch.setattr(McpDriver, "list_meetings", upstream([{
        "meeting_code": "999888777", "meeting_id": "mid-x", "subject": "号主自己约的会",
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "status": "待开始",
    }]))
    conn = connect()
    try:
        result = service.sync_account(conn, acc["id"])
    finally:
        conn.close()
    assert result["imported"] == 1

    external = service.external_meetings()
    assert len(external) == 1
    assert external[0]["meeting_code"] == "999888777"
    assert external[0]["account_label"] == "MCP 号"

    timeline = service.day_timeline(start.date().isoformat())
    blocks = [b for b in timeline["accounts"][0]["blocks"] if b["source"] == "external"]
    assert blocks and blocks[0]["subject"] == "号主自己约的会"


def test_external_meeting_blocks_allocation(monkeypatch):
    acc = mcp_account()
    start = tomorrow(10)
    monkeypatch.setattr(McpDriver, "list_meetings", upstream([{
        "meeting_code": "999888777", "meeting_id": "mid-x", "subject": "号主约的",
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "status": "待开始",
    }]))
    conn = connect()
    try:
        service.sync_account(conn, acc["id"])
    finally:
        conn.close()

    # 同一时段借号：上游已被占，必须拒绝
    with pytest.raises(ValueError) as exc:
        service.create_booking({"requester": "张三", "subject": "撞车",
                                "start_at": start.isoformat(), "duration_minutes": 60})
    assert "满了" in str(exc.value)

    # 错开的时段可以
    later = start.replace(hour=14)
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    booking = service.create_booking({"requester": "张三", "subject": "错开",
                                      "start_at": later.isoformat(), "duration_minutes": 60})
    assert booking["meeting_code"] == "111222333"


def test_sync_marks_missing_and_cancel_failed(monkeypatch):
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    booking = service.create_booking({"requester": "张三", "subject": "会被上游删掉",
                                      "start_at": tomorrow(11).isoformat(),
                                      "duration_minutes": 60})

    # 上游列表里没有它 → missing
    monkeypatch.setattr(McpDriver, "list_meetings", upstream([]))
    conn = connect()
    try:
        result = service.sync_account(conn, booking["account_id"])
        row = conn.execute("SELECT upstream_state FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    assert result["missing"] == 1
    assert row["upstream_state"] == "missing"


def test_sync_flags_cancelled_but_still_upstream(monkeypatch):
    """本地取消了、上游还在 —— 这正是「取消了没真取消」的场景。"""
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    monkeypatch.setattr(McpDriver, "cancel", lambda self, a, t, b: None)
    monkeypatch.setattr(McpDriver, "_meeting_state", lambda self, t, mid: "")
    booking = service.create_booking({"requester": "张三", "subject": "取消了但还在",
                                      "start_at": tomorrow(15).isoformat(),
                                      "duration_minutes": 60})
    service.cancel_booking(booking["id"], token=booking["manage_token"])

    monkeypatch.setattr(McpDriver, "list_meetings", upstream([{
        "meeting_code": "111222333", "meeting_id": "mid-1", "subject": "取消了但还在",
        "start_at": tomorrow(15).isoformat(),
        "end_at": tomorrow(16).isoformat(), "status": "待开始",
    }]))
    conn = connect()
    try:
        result = service.sync_account(conn, booking["account_id"])
        row = conn.execute("SELECT upstream_state, cancel_error FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    assert result["cancel_failed"] == 1
    assert row["upstream_state"] == "cancel_failed"
    assert "仍然存在" in row["cancel_error"]
    assert service.problem_bookings()[0]["id"] == booking["id"]


# --------------------------------------------------------------- 取消必须真取消


def test_cancel_fails_keeps_slot(monkeypatch):
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))

    def boom(self, account, token, booking):
        raise DriverError("上游超时")

    monkeypatch.setattr(McpDriver, "cancel", boom)
    booking = service.create_booking({"requester": "张三", "subject": "取消会失败",
                                      "start_at": tomorrow(16).isoformat(),
                                      "duration_minutes": 60})

    with pytest.raises(ValueError) as exc:
        service.cancel_booking(booking["id"], token=booking["manage_token"])
    assert "没取消成功" in str(exc.value)

    conn = connect()
    try:
        row = conn.execute("SELECT status, upstream_state, cancel_error FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    # 关键：本地不能假装取消，号必须还占着
    assert row["status"] == "pending"
    assert row["upstream_state"] == "cancel_failed"
    assert "上游超时" in row["cancel_error"]

    with pytest.raises(ValueError):
        service.create_booking({"requester": "李四", "subject": "抢号",
                                "start_at": tomorrow(16).isoformat(),
                                "duration_minutes": 60})


def test_force_cancel_releases_but_keeps_flag(monkeypatch):
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    monkeypatch.setattr(McpDriver, "cancel",
                        lambda self, a, t, b: (_ for _ in ()).throw(DriverError("上游超时")))
    booking = service.create_booking({"requester": "张三", "subject": "强制释放",
                                      "start_at": tomorrow(17).isoformat(),
                                      "duration_minutes": 60})
    out = service.cancel_booking(booking["id"], token=booking["manage_token"], force=True)
    assert out["forced"] is True

    conn = connect()
    try:
        row = conn.execute("SELECT status, upstream_state FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "cancelled"
    assert row["upstream_state"] == "cancel_failed"

    # 释放后别人能借到
    monkeypatch.setattr(McpDriver, "create", fake_create("444555666", "mid-2"))
    again = service.create_booking({"requester": "李四", "subject": "接着用",
                                    "start_at": tomorrow(17).isoformat(),
                                    "duration_minutes": 60})
    assert again["meeting_code"] == "444555666"


def test_retry_cancel_clears_flag(monkeypatch):
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    monkeypatch.setattr(McpDriver, "cancel",
                        lambda self, a, t, b: (_ for _ in ()).throw(DriverError("上游超时")))
    booking = service.create_booking({"requester": "张三", "subject": "稍后重试",
                                      "start_at": tomorrow(18).isoformat(),
                                      "duration_minutes": 60})
    service.cancel_booking(booking["id"], token=booking["manage_token"], force=True)

    monkeypatch.setattr(McpDriver, "cancel", lambda self, a, t, b: None)
    monkeypatch.setattr(McpDriver, "_meeting_state", lambda self, t, mid: "已取消")
    out = service.retry_cancel(booking["id"])
    assert out["note"] == "腾讯会议侧已取消"

    conn = connect()
    try:
        row = conn.execute("SELECT upstream_state, cancel_error FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    assert row["upstream_state"] == "cancelled"
    assert row["cancel_error"] == ""
    assert not service.problem_bookings()


def test_missing_booking_can_be_released(monkeypatch):
    """上游已经没这场会了，取消时不该再去撞一次错误。"""
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    booking = service.create_booking({"requester": "张三", "subject": "上游消失了",
                                      "start_at": tomorrow(19).isoformat(),
                                      "duration_minutes": 60})
    conn = connect()
    try:
        conn.execute("UPDATE bookings SET upstream_state='missing' WHERE id=?", (booking["id"],))
    finally:
        conn.close()
    out = service.cancel_booking(booking["id"], token=booking["manage_token"])
    assert out["status"] == "cancelled"


def test_started_meeting_released_without_upstream_cancel(monkeypatch):
    """已开始的会腾讯不允许取消：释放号码，并明确告知去会里「结束会议」。"""
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    booking = service.create_booking({"requester": "张三", "subject": "正在开的会",
                                      "start_at": tomorrow(9).isoformat(),
                                      "duration_minutes": 60})
    started = service.now() - timedelta(minutes=10)
    conn = connect()
    try:
        conn.execute("UPDATE bookings SET start_at=?, end_at=? WHERE id=?",
                     (started.isoformat(timespec="seconds"),
                      (started + timedelta(hours=1)).isoformat(timespec="seconds"), booking["id"]))
    finally:
        conn.close()

    calls = {"n": 0}

    def spy(self, account, token, row):
        calls["n"] += 1

    monkeypatch.setattr(McpDriver, "cancel", spy)
    out = service.cancel_booking(booking["id"], token=booking["manage_token"])
    assert out["status"] == "cancelled"
    assert "结束会议" in out["note"]
    assert calls["n"] == 0  # 根本不该去撞上游

    conn = connect()
    try:
        row = conn.execute("SELECT upstream_state, cancel_error FROM bookings WHERE id=?",
                           (booking["id"],)).fetchone()
    finally:
        conn.close()
    assert row["upstream_state"] == "started"
    assert "结束会议" in row["cancel_error"]


def test_started_release_not_flagged_as_problem(monkeypatch):
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    booking = service.create_booking({"requester": "张三", "subject": "开完就结束",
                                      "start_at": tomorrow(9).isoformat(),
                                      "duration_minutes": 60})
    started = service.now() - timedelta(minutes=5)
    conn = connect()
    try:
        conn.execute("UPDATE bookings SET start_at=?, end_at=?, status='active' WHERE id=?",
                     (started.isoformat(timespec="seconds"),
                      (started + timedelta(hours=1)).isoformat(timespec="seconds"), booking["id"]))
    finally:
        conn.close()
    service.cancel_booking(booking["id"], token=booking["manage_token"])

    # 上游列表里它还在（会议还在进行），但不应算异常
    monkeypatch.setattr(McpDriver, "list_meetings", upstream([{
        "meeting_code": "111222333", "meeting_id": "mid-1", "subject": "开完就结束",
        "start_at": started.isoformat(), "end_at": (started + timedelta(hours=1)).isoformat(),
        "status": "进行中",
    }]))
    conn = connect()
    try:
        service.sync_account(conn, booking["account_id"])
    finally:
        conn.close()
    assert not service.problem_bookings()


def test_upstream_says_started_is_treated_as_started(monkeypatch):
    """上游明确回「已开始的会议不能取消」时，不要让员工看到吓人的报错。"""
    mcp_account()
    monkeypatch.setattr(McpDriver, "create", fake_create("111222333", "mid-1"))
    monkeypatch.setattr(McpDriver, "cancel",
                        lambda self, a, t, b: (_ for _ in ()).throw(DriverError("已开始的会议不能取消")))
    booking = service.create_booking({"requester": "张三", "subject": "卡时间的会",
                                      "start_at": tomorrow(10).isoformat(),
                                      "duration_minutes": 60})
    out = service.cancel_booking(booking["id"], token=booking["manage_token"])
    assert out["status"] == "cancelled"
    assert "结束会议" in out["note"]


def test_manual_account_skips_upstream_cancel():
    conn = connect()
    try:
        service.save_account(conn, {"label": "固定号", "driver": "manual", "fixed_code": "826341057"})
    finally:
        conn.close()
    booking = service.create_booking({"requester": "张三", "subject": "固定号取消",
                                      "start_at": tomorrow(20).isoformat(),
                                      "duration_minutes": 30})
    out = service.cancel_booking(booking["id"], token=booking["manage_token"])
    assert out["status"] == "cancelled"
