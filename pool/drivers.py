"""会议创建驱动层。

三种模式，账号级别可选：

- ``mcp``    真实调用腾讯会议 MCP，用该账号自己的 Token。
- ``manual`` 该账号登记一个固定会议号，系统只负责排期、占用和分发，不连网。
- ``mock``   演示/联调模式，本地伪造会议号，完整跑通借还流程。

腾讯会议 MCP 的关键事实（2026-10 核对官方 tools/list）：

- 端点：``https://mcp.meeting.tencent.com/mcp/wemeet-open/v1``（不是 ``/mcp``）
- 鉴权：请求头 ``X-Tencent-Meeting-Token``；缺 Token 直接 401
- 所有工具都要求 ``_client_info``（os / agent / model）系统参数
- 建会参数名是 ``time_zone``（带下划线），不是 ``timezone``
- 返回是两层包装：``result.content[].text`` → JSON → ``data.body`` 再是 JSON 字符串，
  真正的错误码藏在 ``data.status_code`` 和 ``body.error`` 里，HTTP 层永远是 200
"""
from __future__ import annotations

import json
import platform
import random
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .config import SETTINGS

# 官方当前端点。若官方调整，改 .env 的 POOL_MCP_URL；自检工具也会把这几个候选都试一遍。
DEFAULT_MCP_URL = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
CANDIDATE_URLS = [
    DEFAULT_MCP_URL,
    "https://mcp.meeting.tencent.com/mcp",
    "https://mcp.meeting.tencent.com/mcp/v1",
    "https://mcp.meeting.tencent.com/sse",
    "https://mcp.meeting.tencent.com/",
]

CLIENT_INFO = {
    "os": platform.system() or "Windows",
    "agent": "meeting-pool",
    "model": "meeting-pool/1.0",
}

TOKEN_HINT = "Token 无效或已失效，请到 meeting.tencent.com/ai-skill.html 用该账号重新获取"
URL_HINT = ("MCP 地址 404：官方端点是 /mcp/wemeet-open/v1，"
            "请在 .env 或后台自检里确认 POOL_MCP_URL")


class DriverError(RuntimeError):
    """驱动层失败，带上给员工看的中文原因。"""


@dataclass
class MeetingResult:
    meeting_code: str = ""
    meeting_id: str = ""
    join_url: str = ""
    password: str = ""
    host_key: str = ""
    raw: str = ""

    def as_dict(self) -> dict:
        return {
            "meeting_code": self.meeting_code,
            "meeting_id": self.meeting_id,
            "join_url": self.join_url,
            "password": self.password,
            "host_key": self.host_key,
        }


# --------------------------------------------------------------------------- 解析工具


def _dig(obj: Any, keys: tuple[str, ...], depth: int = 0) -> str:
    """在任意嵌套结构里找第一个命中的字段，兼容不同返回包装。"""
    if depth > 6:
        return ""
    if isinstance(obj, dict):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        for value in obj.values():
            found = _dig(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _dig(item, keys, depth + 1)
            if found:
                return found
    return ""


def _maybe_json(text: Any) -> Any:
    if isinstance(text, (dict, list)):
        return text
    text = str(text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _parse_sse(text: str) -> dict:
    """MCP 流式响应：取出最后一个带 result/error 的 data 帧。"""
    payload: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = _maybe_json(line[5:].strip())
        if isinstance(chunk, dict) and ("result" in chunk or "error" in chunk):
            payload = chunk
    return payload


def _friendly_error(message: str) -> str:
    text = (message or "").strip()
    low = text.lower()
    if "token not found" in low or "invalid_token" in low or "token" in low and "无效" in text:
        return TOKEN_HINT
    if "permission" in low or "no permission" in low or "无权限" in text:
        return "该账号没有这个操作权限（可能不是会议创建者）"
    if "rate" in low and "limit" in low:
        return "腾讯会议接口限流，稍后重试"
    return text or "上游返回未知错误"


# --------------------------------------------------------------------------- 基类


class BaseDriver:
    name = "base"
    requires_token = False

    def ready(self, account: dict, token: str) -> tuple[bool, str]:
        return True, ""

    def list_meetings(self, token: str, **kwargs) -> list[dict]:
        """上游已有会议（供对账）。非 MCP 模式没有这个概念。"""
        return []

    def create(self, account: dict, token: str, payload: dict) -> MeetingResult:
        raise NotImplementedError

    def cancel(self, account: dict, token: str, booking: dict) -> None:
        return None

    def probe(self, account: dict, token: str) -> tuple[str, str]:
        return "ok", "已通过探测"


# --------------------------------------------------------------------------- 演示


class MockDriver(BaseDriver):
    name = "mock"

    def create(self, account: dict, token: str, payload: dict) -> MeetingResult:
        code = f"{random.randint(100, 999)}{random.randint(100, 999)}{random.randint(100, 999)}"
        return MeetingResult(
            meeting_code=code,
            meeting_id=f"mock-{secrets.token_hex(6)}",
            join_url=f"https://meeting.tencent.com/dm/{code}",
            password=payload.get("password", ""),
            raw=json.dumps({"driver": "mock", "code": code}, ensure_ascii=False),
        )

    def probe(self, account: dict, token: str) -> tuple[str, str]:
        return "ok", "演示模式，无需联网"


# --------------------------------------------------------------------------- 固定会议号


class ManualDriver(BaseDriver):
    """手工登记模式：账号自带一个固定会议号，系统只管排期和分发。"""

    name = "manual"

    def ready(self, account: dict, token: str) -> tuple[bool, str]:
        if not (account.get("fixed_code") or "").strip():
            return False, "该账号未登记固定会议号"
        return True, ""

    def create(self, account: dict, token: str, payload: dict) -> MeetingResult:
        code = (account.get("fixed_code") or "").strip()
        digits = re.sub(r"\D", "", code)
        if len(digits) < 9:
            raise DriverError("固定会议号格式不正确，应为 9 位数字")
        return MeetingResult(
            meeting_code=digits,
            meeting_id="",
            join_url=f"https://meeting.tencent.com/dm/{digits}",
            password=payload.get("password", ""),
            raw=json.dumps({"driver": "manual", "code": digits}, ensure_ascii=False),
        )

    def probe(self, account: dict, token: str) -> tuple[str, str]:
        ok, note = self.ready(account, token)
        return ("ok" if ok else "warn"), note or "固定会议号已登记"


# --------------------------------------------------------------------------- 腾讯会议 MCP


class McpDriver(BaseDriver):
    name = "mcp"
    requires_token = True

    # ---- JSON-RPC 基础
    def _headers(self, token: str, session_id: str = "") -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Skill-Version": SETTINGS.mcp_skill_version,
        }
        if token:
            headers[SETTINGS.mcp_token_header] = token
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _rpc(self, token: str, payload: dict, session_id: str = "",
             url: str | None = None) -> tuple[dict, str]:
        target = url or SETTINGS.mcp_url
        try:
            with httpx.Client(timeout=SETTINGS.mcp_timeout) as client:
                resp = client.post(target, json=payload, headers=self._headers(token, session_id))
        except httpx.HTTPError as exc:
            raise DriverError(f"连不上腾讯会议 MCP（{exc.__class__.__name__}），检查服务器出网") from exc
        if resp.status_code == 401:
            raise DriverError(TOKEN_HINT)
        if resp.status_code == 403:
            raise DriverError("Token 被拒绝（403），可能已在网页端重置")
        if resp.status_code == 404:
            raise DriverError(URL_HINT)
        if resp.status_code >= 400:
            raise DriverError(f"MCP 返回 HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        body = resp.text
        data = _parse_sse(body) if "event-stream" in ctype else _maybe_json(body)
        if not isinstance(data, dict) or not data:
            data = _parse_sse(body) or {}
        return data, resp.headers.get("mcp-session-id", "")

    def _initialize(self, token: str, url: str | None = None) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "meeting-pool", "version": "1.0.0"},
            },
        }
        data, session_id = self._rpc(token, payload, url=url)
        if "error" in data:
            raise DriverError(_friendly_error(data["error"].get("message", "")))
        try:
            with httpx.Client(timeout=SETTINGS.mcp_timeout) as client:
                client.post(
                    url or SETTINGS.mcp_url,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers=self._headers(token, session_id),
                )
        except httpx.HTTPError:
            pass
        return session_id

    def _unwrap(self, result: dict) -> dict:
        """剥掉 MCP 的双层包装，把真正的业务错误抛出来。

        外层：{"content":[{"type":"text","text":"<json>"}]}
        中层：{"_meta":{...}, "data":{"status_code":200,"body":"<json 字符串>"}}
        业务：body 里才是会议字段，出错则是 {"error":"type:business, code:-1, msg:..."}
        """
        if result.get("isError"):
            raise DriverError(_friendly_error(_dig(result, ("text",))[:200]))
        text = _dig(result, ("text",))
        payload = _maybe_json(text) or result.get("structuredContent") or {}
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data")
        if isinstance(data, dict):
            status = data.get("status_code") or data.get("statusCode")
            body = data.get("body")
            body_obj = _maybe_json(body) if isinstance(body, str) else (body or {})
            try:
                status_int = int(status)
            except (TypeError, ValueError):
                status_int = 200
            err = ""
            if isinstance(body_obj, dict):
                err = str(body_obj.get("error") or "")
                err = err or _dig(body_obj, ("msg", "message"))
            if status_int >= 400 or err:
                raise DriverError(_friendly_error(err or f"业务状态码 {status_int}"))
            return body_obj if isinstance(body_obj, dict) else {}
        return payload

    def _call(self, token: str, tool: str, arguments: dict, url: str | None = None) -> dict:
        session_id = self._initialize(token, url)
        args = dict(arguments)
        args.setdefault("_client_info", CLIENT_INFO)
        data, _ = self._rpc(
            token,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            session_id,
            url=url,
        )
        if "error" in data:
            raise DriverError(_friendly_error(data["error"].get("message") or "调用失败"))
        return self._unwrap(data.get("result") or {})

    def _build_args(self, payload: dict) -> dict:
        template = SETTINGS.mcp_args_template
        rendered = (template
                    .replace("{subject}", payload["subject"])
                    .replace("{start}", payload["start"])
                    .replace("{end}", payload["end"])
                    .replace("{password}", payload.get("password", ""))
                    .replace("{timezone}", payload.get("timezone", "Asia/Shanghai")))
        args = _maybe_json(rendered)
        if not isinstance(args, dict) or not args:
            raise DriverError("POOL_MCP_ARGS 模板不是合法 JSON")
        empty = [k for k, v in args.items() if v == ""]
        for key in empty:
            args.pop(key, None)
        return args

    # ---- 对外能力
    def ready(self, account: dict, token: str) -> tuple[bool, str]:
        if not token:
            return False, "该账号还没绑定 Token"
        return True, ""

    def create(self, account: dict, token: str, payload: dict) -> MeetingResult:
        body = self._call(token, SETTINGS.mcp_tool, self._build_args(payload))
        code = _dig(body, ("meeting_code", "meetingCode", "meeting_number", "meetingNumber",
                           "meeting_no", "meetingNo", "code"))
        if not re.fullmatch(r"\d{9}", code or ""):
            match = re.search(r"(?<!\d)(\d{9})(?!\d)", json.dumps(body, ensure_ascii=False))
            if match:
                code = match.group(1)
        join_url = _dig(body, ("join_url", "joinUrl", "join_meeting_url", "share_url", "url"))
        meeting_id = _dig(body, ("meeting_id", "meetingId", "id"))
        password = _dig(body, ("password", "meeting_password")) or payload.get("password", "")
        host_key = _dig(body, ("host_key", "hostKey"))
        if not code and not join_url:
            raise DriverError("建会接口有返回，但没解析出 9 位会议号")
        return MeetingResult(
            meeting_code=code,
            meeting_id=meeting_id,
            join_url=join_url or f"https://meeting.tencent.com/dm/{code}",
            password=password,
            host_key=host_key,
            raw=json.dumps(body, ensure_ascii=False)[:4000],
        )

    def _resolve_meeting_id(self, token: str, code: str) -> str:
        body = self._call(token, "get_meeting_by_code", {"meeting_code": code})
        return _dig(body, ("meeting_id", "meetingId"))

    def list_meetings(self, token: str, page_size: int = 50, max_pages: int = 5) -> list[dict]:
        """拉「未开始 / 进行中」的会议，自动翻页。只读，用来跟号池对账。"""
        out: list[dict] = []
        cursor = ""
        for _ in range(max_pages):
            args: dict[str, Any] = {
                "page_size": page_size,
                "is_show_all_sub_meetings": 1,
                "timezone": "Asia/Shanghai",
            }
            if cursor:
                args["page_token"] = cursor
            body = self._call(token, SETTINGS.mcp_list_tool, args)
            for item in body.get("meeting_info_list") or []:
                if not isinstance(item, dict):
                    continue
                start = str(item.get("start_time") or "").strip()
                end = str(item.get("end_time") or "").strip()
                if not start or not end:
                    continue
                out.append({
                    "meeting_code": str(item.get("meeting_code") or ""),
                    "meeting_id": str(item.get("meeting_id") or ""),
                    "subject": str(item.get("subject") or "未命名会议"),
                    "start_at": start,
                    "end_at": end,
                    "status": str(item.get("status") or ""),
                })
            if not body.get("has_more"):
                break
            cursor = str(body.get("next_page_token") or "")
            if not cursor:
                break
        return out

    def _meeting_state(self, token: str, meeting_id: str) -> str:
        """查会议当前状态；查不到（已被取消/删除）返回「已取消」。"""
        try:
            body = self._call(token, "get_meeting", {"meeting_id": meeting_id, "is_compact": True})
        except DriverError as exc:
            text = str(exc)
            low = text.lower()
            if "不存在" in text or "not found" in low or "已取消" in text or "取消" in text:
                return "已取消"
            return ""  # 判断不了就不阻断
        info = body.get("meeting_info_list") or body
        if isinstance(info, list) and info:
            info = info[0]
        return _dig(info, ("status", "meeting_status")) if isinstance(info, dict) else ""

    def cancel(self, account: dict, token: str, booking: dict) -> None:
        meeting_id = (booking.get("meeting_id") or "").strip()
        code = (booking.get("meeting_code") or "").strip()
        if not meeting_id and code:
            try:
                meeting_id = self._resolve_meeting_id(token, code)
            except DriverError:
                meeting_id = ""
        if not meeting_id:
            raise DriverError("缺 meeting_id，无法在上游取消（可让管理员强制释放）")
        self._call(token, SETTINGS.mcp_cancel_tool, {"meeting_id": meeting_id})

        # 核验：接口说成功不代表真取消了，回查一次状态
        state = self._meeting_state(token, meeting_id)
        if state and state not in ("已取消", "已结束", "cancelled", "canceled"):
            raise DriverError(f"取消未生效：上游仍显示「{state}」")

    def probe(self, account: dict, token: str) -> tuple[str, str]:
        if not token:
            return "warn", "未绑定 Token"
        try:
            self._call(token, SETTINGS.mcp_list_tool, {"page_size": 1})
            return "ok", "Token 有效，会议接口可读"
        except DriverError as exc:
            text = str(exc)
            if text == TOKEN_HINT or "403" in text:
                return "bad", text
            if "404" in text or "/mcp/" in text:
                return "bad", text
            return "warn", text[:180]


DRIVERS: dict[str, BaseDriver] = {
    "mcp": McpDriver(),
    "manual": ManualDriver(),
    "mock": MockDriver(),
}


def get_driver(name: str) -> BaseDriver:
    return DRIVERS.get(name, DRIVERS["mock"])


# --------------------------------------------------------------------------- 端点自检


def diagnose(token: str = "", urls: list[str] | None = None) -> list[dict]:
    """逐个候选端点试：连通性 → tools/list → 带 Token 实调一次。

    不写数据库，只读探测，供后台自检使用。
    实测行为（2026-10）：错误路径一律 404；正确路径缺 Token 可能回 401 或 500；
    tools/list 没有 Token 时不返回内容，所以要判断「路径对不对」看的是不是 404。
    """
    driver = McpDriver()
    targets = urls or [SETTINGS.mcp_url] + [u for u in CANDIDATE_URLS if u != SETTINGS.mcp_url]
    rows: list[dict] = []
    for url in targets:
        row = {"url": url, "http": None, "initialize": "", "tools": 0,
               "token": "", "path_ok": False, "verdict": ""}
        try:
            with httpx.Client(timeout=SETTINGS.mcp_timeout) as client:
                resp = client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "meeting-pool", "version": "1.0.0"}}},
                    headers=driver._headers(token),
                )
            row["http"] = resp.status_code
        except httpx.HTTPError as exc:
            row["initialize"] = f"连不上（{exc.__class__.__name__}）"
            row["verdict"] = "网络不通"
            rows.append(row)
            continue

        http = row["http"]
        if http == 404:
            row["initialize"] = "路径不存在"
            row["verdict"] = "地址错误"
            rows.append(row)
            continue

        row["path_ok"] = True
        if http == 200:
            data = _maybe_json(resp.text) or _parse_sse(resp.text)
            if "result" in data:
                info = (data["result"].get("serverInfo") or {}).get("name", "")
                row["initialize"] = f"OK {info}".strip()
            elif "error" in data:
                row["initialize"] = str(data["error"].get("message", ""))[:60]
            else:
                row["initialize"] = "HTTP 200"
        elif http == 401:
            row["initialize"] = "路径存在，需要 Token"
        elif http >= 500:
            row["initialize"] = f"路径存在（HTTP {http}，通常是没有 Token）"
        else:
            row["initialize"] = f"HTTP {http}"

        if http == 200 or token:
            try:
                session = driver._initialize(token, url)
                with httpx.Client(timeout=SETTINGS.mcp_timeout) as client:
                    resp2 = client.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                        headers=driver._headers(token, session),
                    )
                data2 = _maybe_json(resp2.text) or _parse_sse(resp2.text)
                row["tools"] = len(((data2.get("result") or {}).get("tools") or []))
            except (DriverError, httpx.HTTPError):
                row["tools"] = 0

        if token:
            try:
                body = driver._call(token, SETTINGS.mcp_list_tool, {"page_size": 1}, url=url)
                row["token"] = "可用" if isinstance(body, dict) else "返回结构异常"
            except DriverError as exc:
                row["token"] = str(exc)[:80]
        else:
            row["token"] = "未提供"

        if row["tools"] > 0 and row["token"] in ("可用", "未提供"):
            row["verdict"] = "可用"
        elif row["tools"] > 0:
            row["verdict"] = "地址对，Token 有问题"
        elif token:
            row["verdict"] = "地址对，工具列表拿不到"
        else:
            row["verdict"] = "地址对（带 Token 才能验证）"
        rows.append(row)
    return rows
