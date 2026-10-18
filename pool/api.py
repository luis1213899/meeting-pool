"""HTTP 接口层（FastAPI）。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import service
from .config import SETTINGS
from .db import audit, connect, get_settings, init_db
from .drivers import diagnose
from .security import check_admin, decrypt_secret, sign_session, verify_session

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE = "pool_admin"


async def _reaper() -> None:
    """后台推进预约状态：到点开始、结束释放。"""
    while True:
        try:
            service.reap()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="会议号池", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

_buckets: dict[str, deque] = defaultdict(deque)
LIMITS = {
    "book": (12, 60),     # 60 秒内最多 12 次借号
    "login": (8, 300),    # 5 分钟内最多 8 次登录尝试
}


def reset_rate_limits() -> None:
    """供测试与运维重置限流计数。"""
    _buckets.clear()


def _rate_limit(key: str, ip: str) -> None:
    limit, window = LIMITS[key]
    bucket = _buckets[f"{key}:{ip}"]
    now_ts = time.time()
    while bucket and now_ts - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="操作太频繁，请稍后再试")
    bucket.append(now_ts)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def require_admin(request: Request) -> str:
    token = request.cookies.get(COOKIE, "")
    username = verify_session(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="请先登录管理后台")
    return username


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None


# --------------------------------------------------------------------------- 员工端


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/state")
def state():
    return service.public_state()


@app.get("/api/timeline")
def timeline(date: Optional[str] = None):
    try:
        return service.day_timeline(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD") from None


@app.post("/api/bookings")
async def create_booking(request: Request):
    ip = _client_ip(request)
    _rate_limit("book", ip or "unknown")
    payload = await _body(request)
    try:
        return service.create_booking(payload, ip=ip)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.get("/api/bookings/{booking_id}")
def read_booking(booking_id: int, token: Optional[str] = None):
    try:
        return service.get_booking(booking_id, token)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@app.post("/api/bookings/{booking_id}/cancel")
async def cancel_booking_public(booking_id: int, request: Request):
    payload = await _body(request) if request.headers.get("content-length") else {}
    token = payload.get("token") if payload else None
    try:
        return service.cancel_booking(booking_id, token=token, ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@app.post("/api/my")
async def my_bookings(request: Request):
    payload = await _body(request)
    tokens = [str(t) for t in (payload.get("tokens") or []) if t][:30]
    return {"items": service.list_my_bookings(tokens)}


# --------------------------------------------------------------------------- 管理端


@app.post("/api/admin/login")
async def admin_login(request: Request, response: Response):
    ip = _client_ip(request)
    _rate_limit("login", ip or "unknown")
    payload = await _body(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not check_admin(username, password):
        conn = connect()
        try:
            audit(conn, "admin.login.fail", username, "admin", "", ip)
        finally:
            conn.close()
        raise HTTPException(status_code=401, detail="账号或口令不正确")
    token = sign_session(username)
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=12 * 3600)
    conn = connect()
    try:
        audit(conn, "admin.login", username, "admin", "", ip)
    finally:
        conn.close()
    return {"ok": True, "username": username, "demo_mode": SETTINGS.demo_mode,
            "default_password": SETTINGS.default_admin_password}


@app.post("/api/admin/logout")
def admin_logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/admin/me")
def admin_me(request: Request):
    """未登录也返回 200，避免控制台刷 401；前端看 authed 字段。"""
    token = request.cookies.get(COOKIE, "")
    username = verify_session(token) if token else None
    if not username:
        return {"authed": False}
    return {"authed": True, "username": username, "demo_mode": SETTINGS.demo_mode,
            "default_password": SETTINGS.default_admin_password}


@app.get("/api/admin/overview")
def admin_overview(username: str = Depends(require_admin)):
    return service.admin_overview()


@app.get("/api/admin/accounts")
def admin_accounts(username: str = Depends(require_admin)):
    conn = connect()
    try:
        return {"items": service.list_accounts(conn)}
    finally:
        conn.close()


@app.post("/api/admin/accounts")
async def admin_create_account(request: Request, username: str = Depends(require_admin)):
    payload = await _body(request)
    conn = connect()
    try:
        data = service.save_account(conn, payload, actor=username, ip=_client_ip(request))
        data.pop("token_cipher", None)
        return data
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        conn.close()


@app.patch("/api/admin/accounts/{account_id}")
async def admin_update_account(account_id: int, request: Request, username: str = Depends(require_admin)):
    payload = await _body(request)
    conn = connect()
    try:
        data = service.save_account(conn, payload, actor=username, account_id=account_id,
                                    ip=_client_ip(request))
        data.pop("token_cipher", None)
        return data
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    finally:
        conn.close()


@app.delete("/api/admin/accounts/{account_id}")
def admin_delete_account(account_id: int, request: Request, username: str = Depends(require_admin)):
    conn = connect()
    try:
        service.delete_account(conn, account_id, actor=username, ip=_client_ip(request))
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    finally:
        conn.close()


@app.post("/api/admin/accounts/{account_id}/probe")
def admin_probe_account(account_id: int, username: str = Depends(require_admin)):
    conn = connect()
    try:
        return service.probe_account(conn, account_id, actor=username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    finally:
        conn.close()


@app.post("/api/admin/mcp/diagnose")
async def admin_mcp_diagnose(request: Request, username: str = Depends(require_admin)):
    """逐个候选端点探测连通性、工具数与 Token 有效性。"""
    payload = await _body(request)
    token = str(payload.get("token") or "").strip()
    account_id = payload.get("account_id")
    account_label = ""
    if account_id and not token:
        conn = connect()
        try:
            acc = service.get_account(conn, int(account_id))
            account_label = acc["label"]
            token = decrypt_secret(acc["token_cipher"]) if acc["token_cipher"] else ""
        except ValueError:
            raise HTTPException(status_code=404, detail="账号不存在") from None
        finally:
            conn.close()
    rows = diagnose(token)
    working = [r["url"] for r in rows if r.get("verdict") == "可用"]
    if not working:
        working = [r["url"] for r in rows if r.get("path_ok") and r.get("tools")]
    return {
        "rows": rows,
        "current_url": SETTINGS.mcp_url,
        "token_from": account_label or ("手工填写" if token else "未提供"),
        "working_url": working[0] if working else "",
    }


@app.get("/api/admin/bookings")
def admin_bookings(date: Optional[str] = None, status: Optional[str] = None,
                   keyword: str = "", username: str = Depends(require_admin)):
    return {"items": service.list_bookings(day=date, status=status, keyword=keyword)}


@app.post("/api/admin/bookings/{booking_id}/cancel")
def admin_cancel_booking(booking_id: int, request: Request, username: str = Depends(require_admin)):
    try:
        return service.cancel_booking(booking_id, token=None, actor=f"admin:{username}",
                                      ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.get("/api/admin/audit")
def admin_audit(username: str = Depends(require_admin)):
    return {"items": service.list_audit()}


@app.get("/api/admin/config")
def admin_get_config(username: str = Depends(require_admin)):
    conn = connect()
    try:
        return get_settings(conn)
    finally:
        conn.close()


@app.patch("/api/admin/config")
async def admin_set_config(request: Request, username: str = Depends(require_admin)):
    payload = await _body(request)
    return service.update_config(payload, actor=username)


@app.get("/api/admin/export.csv")
def admin_export(date_from: str = "", date_to: str = "", username: str = Depends(require_admin)):
    csv_text = service.export_bookings_csv(date_from, date_to)
    return PlainTextResponse(
        "\ufeff" + csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="meeting-pool.csv"'},
    )


# --------------------------------------------------------------------------- 静态资源

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
