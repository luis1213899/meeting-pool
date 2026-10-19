"""HTTP 接口层（FastAPI）。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import service
from .config import SETTINGS
from .db import audit, connect, get_settings, init_db
from .drivers import diagnose
from .security import check_admin, decrypt_secret, sign_session, verify_session

logger = logging.getLogger("pool.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE = "pool_admin"

_WAL_INTERVAL = 4 * 3600  # WAL checkpoint 间隔（秒）


async def _reaper() -> None:
    """后台推进预约状态，定期对账，定期触发 WAL checkpoint。"""
    last_sync = time.time()
    last_wal = time.time()
    while True:
        try:
            await asyncio.to_thread(service.reap)
            conn = connect()
            try:
                interval = int(get_settings(conn).get("sync_minutes") or 0) * 60
            finally:
                conn.close()
            if interval and time.time() - last_sync >= interval:
                last_sync = time.time()
                logger.info("pool.sync.start")
                try:
                    result = await asyncio.to_thread(service.sync_all, "scheduler")
                    logger.info("pool.sync.done accounts=%d imported=%d missing=%d cancel_failed=%d errors=%d",
                                result.get("accounts", 0), result.get("imported", 0),
                                result.get("missing", 0), result.get("cancel_failed", 0),
                                len(result.get("errors", [])))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("pool.sync.error error=%s", exc)
            if time.time() - last_wal >= _WAL_INTERVAL:
                last_wal = time.time()
                try:
                    conn2 = connect()
                    try:
                        conn2.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    finally:
                        conn2.close()
                    logger.info("pool.wal_checkpoint.done")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("pool.wal_checkpoint.error error=%s", exc)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(30)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    # 测试或多实例调试时可关掉后台线程（POOL_DISABLE_SCHEDULER=1）
    task = None if os.environ.get("POOL_DISABLE_SCHEDULER") == "1" else asyncio.create_task(_reaper())
    try:
        yield
    finally:
        if task:
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
        booking = service.create_booking(payload, ip=ip)
        logger.info("booking.create id=%s account_id=%s requester=%s subject=%s start=%s",
                    booking.get("id"), booking.get("account_id"),
                    payload.get("requester", ""), payload.get("subject", ""),
                    payload.get("start_at", ""))
        return booking
    except ValueError as exc:
        logger.warning("booking.create.failed ip=%s requester=%s reason=%s",
                       ip, payload.get("requester", ""), exc)
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
        result = service.cancel_booking(booking_id, token=token, ip=_client_ip(request))
        logger.info("booking.cancel id=%s by=user", booking_id)
        return result
    except ValueError as exc:
        logger.warning("booking.cancel.failed id=%s reason=%s", booking_id, exc)
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
async def admin_cancel_booking(booking_id: int, request: Request, username: str = Depends(require_admin)):
    payload: dict = {}
    if request.headers.get("content-length") not in (None, "0"):
        payload = await _body(request)
    try:
        return service.cancel_booking(booking_id, token=None, actor=f"admin:{username}",
                                      ip=_client_ip(request), force=bool(payload.get("force")))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.post("/api/admin/bookings/{booking_id}/retry-cancel")
def admin_retry_cancel(booking_id: int, request: Request, username: str = Depends(require_admin)):
    try:
        return service.retry_cancel(booking_id, actor=f"admin:{username}", ip=_client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.post("/api/admin/sync")
def admin_sync(username: str = Depends(require_admin)):
    """立即跟腾讯会议对账：拉上游会议、核验取消是否真的生效。"""
    try:
        result = service.sync_all(actor=f"admin:{username}")
        logger.info("admin.sync.done actor=%s accounts=%d imported=%d missing=%d cancel_failed=%d",
                    username, result.get("accounts", 0), result.get("imported", 0),
                    result.get("missing", 0), result.get("cancel_failed", 0))
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("admin.sync.error actor=%s error=%s", username, exc)
        raise HTTPException(status_code=502, detail=f"对账失败：{exc}") from None


@app.get("/api/admin/external")
def admin_external(date: Optional[str] = None, username: str = Depends(require_admin)):
    return {"items": service.external_meetings(day=date)}


@app.get("/api/admin/problems")
def admin_problems(username: str = Depends(require_admin)):
    return {"items": service.problem_bookings()}


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
    gen = service.stream_bookings_csv(date_from, date_to)
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="meeting-pool.csv"'},
    )


@app.get("/booking/{booking_id}", response_class=Response)
def booking_detail_page(booking_id: int, token: Optional[str] = None):
    """可分享的预约详情页：发给与会者查看会议信息和实时状态。"""
    try:
        booking = service.get_booking(booking_id, token)
    except ValueError:
        return Response(content=_detail_error("预约不存在"), media_type="text/html; charset=utf-8", status_code=404)
    except PermissionError:
        return Response(content=_detail_error("链接无效或已过期"), media_type="text/html; charset=utf-8", status_code=403)
    return Response(content=_detail_html(booking), media_type="text/html; charset=utf-8")


def _detail_error(msg: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>无法查看</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  display:grid;place-items:center;min-height:100vh;margin:0;background:#f6f7f9;color:#333a47;}}
.box{{text-align:center;padding:40px 24px;}}h1{{font-size:18px;margin:0 0 8px;}}
p{{color:#6b7585;font-size:14px;margin:0;}}</style></head>
<body><div class="box"><h1>😕 {msg}</h1><p>请向会议发起人索取正确的分享链接。</p></div></body></html>"""


def _detail_html(b: dict) -> str:
    from datetime import datetime as _dt
    def _fmt(iso: str) -> str:
        try:
            d = _dt.fromisoformat(iso)
            return f"{d.month}月{d.day}日 {d.hour:02d}:{d.minute:02d}"
        except Exception:
            return iso[:16]

    status_map = {"pending": ("待开始", "#1f6bff"), "active": ("进行中", "#10a05a"),
                  "done": ("已结束", "#6b7585"), "cancelled": ("已取消", "#6b7585"), "failed": ("失败", "#d92d20")}
    status_text, status_color = status_map.get(b.get("status", ""), ("未知", "#6b7585"))
    start = _fmt(b.get("start_at", ""))
    end_d = b.get("end_at", "")
    end_time = ""
    try:
        d = _dt.fromisoformat(end_d)
        end_time = f"{d.hour:02d}:{d.minute:02d}"
    except Exception:
        pass
    join_url = b.get("join_url", "") or ""
    meeting_code = b.get("meeting_code", "") or "—"
    password = b.get("password", "") or ""
    subject = b.get("subject", "") or "会议"
    requester = b.get("requester", "") or ""
    account_label = b.get("account_label", "") or ""
    host_key = b.get("host_key", "") or ""

    qr_script = f'<script>if(window.QR){{var c=document.getElementById("qr");if(c)QR.toCanvas("{join_url}",c,6);}}</script>' if join_url else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{subject} · 会议详情</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  background:#f6f7f9;color:#171c26;font-size:14px;line-height:1.6;
  -webkit-font-smoothing:antialiased;}}
.wrap{{max-width:480px;margin:0 auto;padding:24px 16px 48px;}}
.card{{background:#fff;border:1px solid #e2e5ec;border-radius:16px;
  padding:22px 20px;box-shadow:0 1px 2px rgba(16,24,40,.04),0 8px 24px -12px rgba(16,24,40,.1);}}
.card+.card{{margin-top:12px;}}
.badge{{display:inline-block;padding:3px 10px;border-radius:99px;
  font-size:12px;font-weight:600;color:#fff;background:{status_color};margin-bottom:10px;}}
h1{{font-size:20px;font-weight:700;line-height:1.3;margin-bottom:4px;}}
.sub{{font-size:13px;color:#6b7585;margin-bottom:16px;}}
.code-wrap{{text-align:center;margin:16px 0 4px;}}
.code{{font-size:36px;font-weight:700;letter-spacing:.06em;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  background:#f6f7f9;border:1px solid #e2e5ec;border-radius:12px;
  display:inline-block;padding:8px 20px;}}
.code-hint{{font-size:12px;color:#9aa2b2;margin-top:4px;}}
.kv{{display:grid;grid-template-columns:80px 1fr;gap:7px 12px;font-size:13.5px;margin-top:16px;}}
.kv dt{{color:#6b7585;}}
.kv dd{{color:#171c26;word-break:break-all;}}
.kv dd a{{color:#1f6bff;text-decoration:none;}}
.kv dd a:hover{{text-decoration:underline;}}
.btn{{display:block;width:100%;padding:13px;border-radius:10px;border:none;
  background:#1f6bff;color:#fff;font-size:15px;font-weight:600;
  font-family:inherit;cursor:pointer;text-align:center;text-decoration:none;
  margin-top:20px;transition:.15s;}}
.btn:hover{{background:#0b5cff;}}
.qr-wrap{{text-align:center;padding:12px 0 4px;}}
.qr-lbl{{font-size:12px;color:#9aa2b2;margin-bottom:8px;}}
#qr{{border-radius:8px;image-rendering:pixelated;border:1px solid #e2e5ec;background:#fff;padding:4px;}}
.qr-hint{{font-size:11.5px;color:#9aa2b2;margin-top:6px;}}
.foot{{text-align:center;font-size:12px;color:#9aa2b2;margin-top:20px;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <span class="badge">{status_text}</span>
    <h1>{subject}</h1>
    <div class="sub">{start}{"–"+end_time if end_time else ""}{" · "+requester if requester else ""}</div>
    <div class="code-wrap">
      <div class="code">{meeting_code}</div>
      <div class="code-hint">会议号</div>
    </div>
    <dl class="kv">
      {"<dt>入会密码</dt><dd><b>"+password+"</b></dd>" if password else "<dt>入会密码</dt><dd>未设置</dd>"}
      {"<dt>入会链接</dt><dd><a href='"+join_url+"' target='_blank' rel='noopener'>点击入会</a></dd>" if join_url else ""}
      {"<dt>发起人</dt><dd>"+requester+"</dd>" if requester else ""}
      {"<dt>账号</dt><dd>"+account_label+"</dd>" if account_label else ""}
      {"<dt>主持人密钥</dt><dd>"+host_key+"<br><small style='color:#9aa2b2'>仅主持人使用</small></dd>" if host_key else ""}
    </dl>
    {"<a class='btn' href='"+join_url+"' target='_blank' rel='noopener'>立即入会</a>" if join_url else ""}
  </div>
  {"<div class='card'><div class='qr-wrap'><div class='qr-lbl'>扫码入会</div><canvas id='qr'></canvas><div class='qr-hint'>手机扫码直接进入腾讯会议</div></div></div>" if join_url else ""}
  <div class="foot">由会议号池生成 · 仅供本次会议使用</div>
</div>
{"<script src='/qr.js'></script>" if join_url else ""}
{qr_script}
</body>
</html>"""


# --------------------------------------------------------------------------- 静态资源

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
