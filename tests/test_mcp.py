"""腾讯会议 MCP 适配层测试：双层包装解析、参数拼装、错误翻译。"""
from __future__ import annotations

import json

import pytest

from pool.config import SETTINGS
from pool.drivers import (
    CANDIDATE_URLS,
    CLIENT_INFO,
    McpDriver,
    DriverError,
    TOKEN_HINT,
)


def wrap(body, status_code=200):
    """按官方真实返回结构造两层包装。"""
    inner = json.dumps({"status_code": status_code, "headers": {"rpcUuid": "x"},
                        "body": json.dumps(body, ensure_ascii=False)})
    payload = json.dumps({"_meta": {}, "data": json.loads(inner)}, ensure_ascii=False)
    return {"content": [{"type": "text", "text": payload}]}


def test_official_endpoint_is_default():
    assert SETTINGS.mcp_url.endswith("/mcp/wemeet-open/v1")
    assert CANDIDATE_URLS[0] == SETTINGS.mcp_url


def test_unwrap_success_returns_body():
    result = wrap({"meeting_code": "123456789", "meeting_id": "7788",
                   "join_url": "https://meeting.tencent.com/dm/123456789", "host_key": "hk001"})
    body = McpDriver()._unwrap(result)
    assert body["meeting_code"] == "123456789"
    assert body["host_key"] == "hk001"


def test_unwrap_token_error_is_translated():
    result = wrap({"error": "type:business, code:-1, msg:token not found"}, status_code=500)
    with pytest.raises(DriverError) as exc:
        McpDriver()._unwrap(result)
    assert str(exc.value) == TOKEN_HINT


def test_unwrap_business_status_without_error_field():
    result = wrap({"msg": "invalid params"}, status_code=400)
    with pytest.raises(DriverError):
        McpDriver()._unwrap(result)


def test_unwrap_is_error_flag():
    with pytest.raises(DriverError):
        McpDriver()._unwrap({"isError": True, "content": [{"type": "text", "text": "boom"}]})


def test_args_template_uses_time_zone_and_drops_empty():
    args = McpDriver()._build_args({
        "subject": "周会", "start": "2026-10-19T10:00:00+08:00",
        "end": "2026-10-19T11:00:00+08:00", "password": "", "timezone": "Asia/Shanghai",
    })
    assert args["time_zone"] == "Asia/Shanghai"
    assert "timezone" not in args
    assert "password" not in args  # 空密码不传，避免上游校验失败
    assert args["subject"] == "周会"


def test_call_injects_client_info(monkeypatch):
    driver = McpDriver()
    seen = {}

    monkeypatch.setattr(driver, "_initialize", lambda token, url=None: "sid")

    def fake_rpc(token, payload, session_id="", url=None):
        seen.update(payload["params"]["arguments"])
        text = json.dumps({"data": {"status_code": 200, "body": "{}"}}, ensure_ascii=False)
        return {"result": {"content": [{"type": "text", "text": text}]}}, ""

    monkeypatch.setattr(driver, "_rpc", fake_rpc)
    driver._call("tok", "get_user_meetings", {"page_size": 1})
    assert seen["_client_info"] == CLIENT_INFO
    assert seen["page_size"] == 1


def test_create_extracts_fields(monkeypatch):
    driver = McpDriver()
    body = {"meeting_code": 987654321, "meeting_id": 42,
            "join_url": "https://meeting.tencent.com/dm/987654321", "password": "1234",
            "host_key": "hk9"}
    monkeypatch.setattr(driver, "_call", lambda *a, **kw: body)
    result = driver.create({}, "tok", {"subject": "会", "start": "x", "end": "y",
                                       "password": "1234", "timezone": "Asia/Shanghai"})
    assert result.meeting_code == "987654321"
    assert result.meeting_id == "42"
    assert result.host_key == "hk9"


def test_create_raises_when_no_code(monkeypatch):
    driver = McpDriver()
    monkeypatch.setattr(driver, "_call", lambda *a, **kw: {"foo": "bar"})
    with pytest.raises(DriverError):
        driver.create({}, "tok", {"subject": "会", "start": "x", "end": "y",
                                   "password": "", "timezone": "Asia/Shanghai"})


def test_cancel_requires_meeting_id(monkeypatch):
    driver = McpDriver()

    def boom(*a, **kw):
        raise DriverError("lookup failed")

    monkeypatch.setattr(driver, "_resolve_meeting_id", boom)
    with pytest.raises(DriverError) as exc:
        driver.cancel({}, "tok", {"meeting_id": "", "meeting_code": "123456789"})
    assert "meeting_id" in str(exc.value)
