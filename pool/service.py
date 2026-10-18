"""号池业务核心：分配、借还、看板。"""
from __future__ import annotations

import csv
import io
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .db import TZ, audit, connect, get_settings, update_settings
from .drivers import DriverError, get_driver
from .security import (
    decrypt_secret,
    encrypt_secret,
    hint_of,
)

ACTIVE_STATUSES = ("pending", "active")
HOLD_STATUSES = ("pending", "active", "done")


# --------------------------------------------------------------------------- 时间


def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ)
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间不能为空")
    text = text.replace("/", "-").replace("Z", "+00:00")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
        except ValueError:
            continue
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def floor_minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


# --------------------------------------------------------------------------- 序列化


def account_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "label": row["label"],
        "owner_name": row["owner_name"],
        "owner_contact": row["owner_contact"],
        "tier": row["tier"],
        "driver": row["driver"],
        "token_hint": row["token_hint"],
        "has_token": bool(row["token_cipher"]),
        "fixed_code": row["fixed_code"],
        "capacity": row["capacity"],
        "max_minutes": row["max_minutes"],
        "daily_limit": row["daily_limit"],
        "status": row["status"],
        "health": row["health"],
        "health_note": row["health_note"],
        "checked_at": row["checked_at"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def booking_row(row: sqlite3.Row, account_label: str = "") -> dict:
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "account_label": account_label,
        "requester": row["requester"],
        "dept": row["dept"],
        "contact": row["contact"],
        "subject": row["subject"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "status": row["status"],
        "meeting_code": row["meeting_code"],
        "meeting_id": row["meeting_id"],
        "join_url": row["join_url"],
        "password": row["password"],
        "host_key": row["host_key"] if "host_key" in row.keys() else "",
        "driver": row["driver"],
        "fail_reason": row["fail_reason"],
        "created_at": row["created_at"],
    }


def share_text(booking: dict) -> str:
    start = parse_dt(booking["start_at"])
    end = parse_dt(booking["end_at"])
    lines = [
        f"【会议邀请】{booking['subject']}",
        f"时间：{start.month}月{start.day}日 {start:%H:%M}-{end:%H:%M}",
        f"会议号：{booking['meeting_code'] or '见入会链接'}",
    ]
    if booking.get("password"):
        lines.append(f"入会密码：{booking['password']}")
    if booking.get("join_url"):
        lines.append(f"入会链接：{booking['join_url']}")
    lines.append(f"发起人：{booking['requester']}")
    return "\n".join(lines)


def attach_share(booking: dict) -> dict:
    booking["share_text"] = share_text(booking)
    return booking


# --------------------------------------------------------------------------- 账号


def list_accounts(conn: sqlite3.Connection, with_stats: bool = True) -> list[dict]:
    rows = conn.execute("SELECT * FROM accounts ORDER BY status='active' DESC, id ASC").fetchall()
    out = [account_row(r) for r in rows]
    if not with_stats:
        return out
    today = now().date()
    week_start = today - timedelta(days=6)
    for acc in out:
        t_rows = conn.execute(
            "SELECT status, start_at, end_at FROM bookings WHERE account_id=? AND status IN ('pending','active','done') AND start_at>=?",
            (acc["id"], datetime.combine(week_start, datetime.min.time(), TZ).isoformat()),
        ).fetchall()
        acc["week_count"] = len(t_rows)
        acc["today_count"] = sum(1 for r in t_rows if parse_dt(r["start_at"]).date() == today)
        acc["week_minutes"] = sum(
            int((parse_dt(r["end_at"]) - parse_dt(r["start_at"])).total_seconds() // 60) for r in t_rows
        )
        current = conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE account_id=? AND status IN ('pending','active') AND start_at<=? AND end_at>=?",
            (acc["id"], now().isoformat(timespec="seconds"), now().isoformat(timespec="seconds")),
        ).fetchone()
        acc["busy_now"] = bool(current["c"] if current else 0)
        acc["fixed_code_hint"] = ("*" * max(0, len(acc["fixed_code"]) - 4) + acc["fixed_code"][-4:]) if acc["fixed_code"] else ""
    return out


def get_account(conn: sqlite3.Connection, account_id: int) -> dict:
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError("账号不存在")
    data = account_row(row)
    data["token_cipher"] = row["token_cipher"]
    return data


def save_account(conn: sqlite3.Connection, payload: dict, actor: str = "admin",
                 account_id: Optional[int] = None, ip: str = "") -> dict:
    label = (payload.get("label") or "").strip()
    if not label:
        raise ValueError("账号名称必填")
    driver = (payload.get("driver") or "mcp").strip()
    if driver not in {"mcp", "manual", "mock"}:
        raise ValueError("不支持的接入方式")
    token_input = payload.get("token", None)
    fields = {
        "label": label[:60],
        "owner_name": (payload.get("owner_name") or "").strip()[:40],
        "owner_contact": (payload.get("owner_contact") or "").strip()[:60],
        "tier": (payload.get("tier") or "pro").strip()[:20],
        "driver": driver,
        "fixed_code": (payload.get("fixed_code") or "").strip()[:20],
        "capacity": int(payload.get("capacity") or 100),
        "max_minutes": int(payload.get("max_minutes") or 240),
        "daily_limit": int(payload.get("daily_limit") or 6),
        "status": (payload.get("status") or "active").strip(),
        "note": (payload.get("note") or "").strip()[:200],
        "updated_at": iso(now()),
    }
    if driver == "manual" and not fields["fixed_code"]:
        raise ValueError("固定会议号模式必须填写会议号")
    if fields["status"] not in {"active", "paused", "disabled"}:
        raise ValueError("状态不合法")

    if account_id is None:
        token_cipher, token_hint = "", ""
        if isinstance(token_input, str) and token_input.strip():
            token_cipher = encrypt_secret(token_input.strip())
            token_hint = hint_of(token_input.strip())
        elif driver == "mcp":
            raise ValueError("MCP 模式必须填写账号 Token")
        cur = conn.execute(
            """INSERT INTO accounts(label, owner_name, owner_contact, tier, driver, token_cipher,
                 token_hint, fixed_code, capacity, max_minutes, daily_limit, status, note,
                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fields["label"], fields["owner_name"], fields["owner_contact"], fields["tier"],
             fields["driver"], token_cipher, token_hint, fields["fixed_code"], fields["capacity"],
             fields["max_minutes"], fields["daily_limit"], fields["status"], fields["note"],
             fields["updated_at"], fields["updated_at"]),
        )
        account_id = int(cur.lastrowid)
        audit(conn, "account.create", actor, f"account:{account_id}", label, ip)
    else:
        existing = get_account(conn, account_id)
        token_cipher = existing["token_cipher"]
        token_hint = existing["token_hint"]
        if isinstance(token_input, str):
            stripped = token_input.strip()
            if stripped:
                token_cipher = encrypt_secret(stripped)
                token_hint = hint_of(stripped)
            elif payload.get("clear_token"):
                token_cipher, token_hint = "", ""
        if driver == "mcp" and not token_cipher:
            raise ValueError("MCP 模式必须绑定 Token")
        conn.execute(
            """UPDATE accounts SET label=?, owner_name=?, owner_contact=?, tier=?, driver=?,
                 token_cipher=?, token_hint=?, fixed_code=?, capacity=?, max_minutes=?,
                 daily_limit=?, status=?, note=?, updated_at=? WHERE id=?""",
            (fields["label"], fields["owner_name"], fields["owner_contact"], fields["tier"],
             fields["driver"], token_cipher, token_hint, fields["fixed_code"], fields["capacity"],
             fields["max_minutes"], fields["daily_limit"], fields["status"], fields["note"],
             fields["updated_at"], account_id),
        )
        audit(conn, "account.update", actor, f"account:{account_id}", label, ip)
    return get_account(conn, account_id)


def delete_account(conn: sqlite3.Connection, account_id: int, actor: str = "admin", ip: str = "") -> None:
    live = conn.execute(
        "SELECT COUNT(*) c FROM bookings WHERE account_id=? AND status IN ('pending','active')",
        (account_id,),
    ).fetchone()
    if live and live["c"]:
        raise ValueError("该账号还有未结束的借用记录，请先结束或取消")
    label = conn.execute("SELECT label FROM accounts WHERE id=?", (account_id,)).fetchone()
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    audit(conn, "account.delete", actor, f"account:{account_id}", label["label"] if label else "", ip)


def probe_account(conn: sqlite3.Connection, account_id: int, actor: str = "admin") -> dict:
    acc = get_account(conn, account_id)
    driver = get_driver(acc["driver"])
    token = decrypt_secret(acc["token_cipher"]) if acc["token_cipher"] else ""
    health, note = driver.probe(acc, token)
    conn.execute(
        "UPDATE accounts SET health=?, health_note=?, checked_at=? WHERE id=?",
        (health, note[:200], iso(now()), account_id),
    )
    audit(conn, "account.probe", actor, f"account:{account_id}", f"{health}: {note}"[:180])
    data = get_account(conn, account_id)
    data.pop("token_cipher", None)
    return data


# --------------------------------------------------------------------------- 分配


def _overlaps(conn: sqlite3.Connection, account_id: int, start: datetime, end: datetime,
              buffer_minutes: int) -> bool:
    lo = (start - timedelta(minutes=buffer_minutes)).isoformat(timespec="seconds")
    hi = (end + timedelta(minutes=buffer_minutes)).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT COUNT(*) c FROM bookings
           WHERE account_id=? AND status IN ('pending','active')
             AND start_at < ? AND end_at > ?""",
        (account_id, hi, lo),
    ).fetchone()
    return bool(row and row["c"])


def _daily_count(conn: sqlite3.Connection, account_id: int, day: date) -> int:
    pre = f"{day.isoformat()}T00:00:00"
    suf = f"{day.isoformat()}T23:59:59"
    row = conn.execute(
        """SELECT COUNT(*) c FROM bookings WHERE account_id=? AND status IN ('pending','active','done')
           AND start_at>=? AND start_at<=?""",
        (account_id, pre, suf),
    ).fetchone()
    return int(row["c"] if row else 0)


def _weekly_used(conn: sqlite3.Connection, requester: str) -> int:
    since = (now() - timedelta(days=7)).isoformat(timespec="seconds")
    row = conn.execute(
        """SELECT COUNT(*) c FROM bookings WHERE requester=? AND status IN ('pending','active','done')
           AND start_at>=?""",
        (requester, since),
    ).fetchone()
    return int(row["c"] if row else 0)


TIER_RANK = {"free": 1, "pro": 2, "biz": 3}


def _candidates(conn: sqlite3.Connection, start: datetime, end: datetime,
                duration: int, cfg: dict) -> list[tuple[dict, str]]:
    rows = conn.execute("SELECT * FROM accounts WHERE status='active'").fetchall()
    picked: list[tuple[dict, str]] = []
    for row in rows:
        acc = account_row(row)
        token = decrypt_secret(row["token_cipher"]) if row["token_cipher"] else ""
        driver = get_driver(acc["driver"])
        ok, reason = driver.ready(acc, token)
        if not ok:
            continue
        if duration > acc["max_minutes"]:
            continue
        if _daily_count(conn, acc["id"], start.date()) >= acc["daily_limit"]:
            continue
        if _overlaps(conn, acc["id"], start, end, cfg["buffer_minutes"]):
            continue
        acc["_token"] = token
        acc["_today"] = _daily_count(conn, acc["id"], start.date())
        picked.append((acc, reason))
    # 负载均衡：当日用得少的优先；同级再看类型与编号
    picked.sort(key=lambda item: (item[0]["_today"], -TIER_RANK.get(item[0]["tier"], 2), item[0]["id"]))
    return picked


def _validate(payload: dict, cfg: dict) -> dict:
    requester = (payload.get("requester") or "").strip()
    subject = (payload.get("subject") or "").strip() or "临时会议"
    if not requester:
        raise ValueError("请填写你的名字")
    if len(requester) > 20:
        raise ValueError("名字太长了")
    try:
        start = floor_minute(parse_dt(payload.get("start_at")))
    except ValueError as exc:
        raise ValueError(f"开始时间格式不正确：{exc}") from exc
    try:
        duration = int(payload.get("duration_minutes") or 60)
    except (TypeError, ValueError):
        raise ValueError("时长必须是数字") from None
    if duration < cfg["min_minutes"] or duration > cfg["max_minutes"]:
        raise ValueError(f"单场时长需在 {cfg['min_minutes']}–{cfg['max_minutes']} 分钟之间")
    end = start + timedelta(minutes=duration)
    current = now()
    if start < current - timedelta(minutes=10):
        raise ValueError("开始时间已经过去了，请选一个未来的时段")
    if start.date() > (current + timedelta(days=cfg["window_days"])).date():
        raise ValueError(f"最多只能提前 {cfg['window_days']} 天预约")
    if start.date() != end.date():
        raise ValueError("会议不能跨天")
    if start.hour < cfg["work_hour_start"] or end.hour > cfg["work_hour_end"] or \
            (end.hour == cfg["work_hour_end"] and end.minute > 0):
        raise ValueError(f"可借时段为每天 {cfg['work_hour_start']}:00–{cfg['work_hour_end']}:00")
    return {
        "requester": requester,
        "dept": (payload.get("dept") or "").strip()[:30],
        "contact": (payload.get("contact") or "").strip()[:40],
        "subject": subject[:60],
        "note": (payload.get("note") or "").strip()[:120],
        "start": start,
        "end": end,
        "duration": duration,
        "password": (payload.get("password") or "").strip()[:6],
        "timezone": "Asia/Shanghai",
    }


# --------------------------------------------------------------------------- 借号


def create_booking(payload: dict, ip: str = "") -> dict:
    conn = connect()
    try:
        cfg = get_settings(conn)
        data = _validate(payload, cfg)
        if cfg["max_per_week"] > 0:
            used = _weekly_used(conn, data["requester"])
            if used >= cfg["max_per_week"]:
                raise ValueError(f"{data['requester']} 本周已借 {used} 次，达到上限（{cfg['max_per_week']} 次/周）")
        if not data["password"]:
            data["password"] = f"{secrets.randbelow(900000) + 100000}"

        tried: list[str] = []
        last_error = ""
        for _attempt in range(3):
            conn.execute("BEGIN IMMEDIATE")
            try:
                candidates = _candidates(conn, data["start"], data["end"], data["duration"], cfg)
                if not candidates:
                    conn.execute("ROLLBACK")
                    raise ValueError(
                        "这个时段号池已经满了，换个时间或缩短时长再试。"
                        + (f"（已尝试 {len(tried)} 个号：{', '.join(tried)}）" if tried else "")
                    )
                account = candidates[0][0]
                manage_token = secrets.token_urlsafe(18)
                cur = conn.execute(
                    """INSERT INTO bookings(account_id, requester, dept, contact, subject, start_at,
                         end_at, status, password, driver, manage_token, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                    (account["id"], data["requester"], data["dept"], data["contact"], data["subject"],
                     iso(data["start"]), iso(data["end"]), data["password"], account["driver"],
                     manage_token, iso(now()), iso(now())),
                )
                booking_id = int(cur.lastrowid)
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            tried.append(account["label"])

            driver = get_driver(account["driver"])
            try:
                result = driver.create(
                    account,
                    account.get("_token", ""),
                    {
                        "subject": data["subject"],
                        "start": iso(data["start"]),
                        "end": iso(data["end"]),
                        "password": data["password"],
                        "timezone": data["timezone"],
                    },
                )
            except (DriverError, Exception) as exc:  # noqa: BLE001
                last_error = str(exc)[:200]
                conn.execute(
                    "UPDATE bookings SET status='failed', fail_reason=?, updated_at=? WHERE id=?",
                    (last_error, iso(now()), booking_id),
                )
                conn.execute(
                    "UPDATE accounts SET health='warn', health_note=?, checked_at=? WHERE id=?",
                    (last_error[:180], iso(now()), account["id"]),
                )
                audit(conn, "booking.fail", data["requester"], f"booking:{booking_id}",
                      f"{account['label']} / {last_error[:120]}", ip)
                continue

            conn.execute(
                """UPDATE bookings SET status='pending', meeting_code=?, meeting_id=?, join_url=?,
                     password=?, host_key=?, raw=?, updated_at=? WHERE id=?""",
                (result.meeting_code, result.meeting_id, result.join_url,
                 result.password or data["password"], result.host_key, result.raw,
                 iso(now()), booking_id),
            )
            row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
            booking = attach_share(booking_row(row, account["label"]))
            booking["manage_token"] = manage_token
            audit(conn, "booking.create", data["requester"], f"booking:{booking_id}",
                  f"{account['label']} {iso(data['start'])} {data['duration']}min", ip)
            return booking

        raise ValueError(f"号池暂时开不出会：{last_error or '没有可用账号'}")
    finally:
        conn.close()


def get_booking(booking_id: int, token: Optional[str] = None) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            """SELECT b.*, a.label AS account_label FROM bookings b
               LEFT JOIN accounts a ON a.id=b.account_id WHERE b.id=?""",
            (booking_id,),
        ).fetchone()
        if not row:
            raise ValueError("预约不存在")
        if token is not None and token != row["manage_token"]:
            raise PermissionError("管理口令不匹配")
        booking = attach_share(booking_row(row, row["account_label"] or ""))
        booking["manage_token"] = row["manage_token"]
        return booking
    finally:
        conn.close()


def list_my_bookings(tokens: list[str]) -> list[dict]:
    if not tokens:
        return []
    conn = connect()
    try:
        marks = ",".join("?" * len(tokens))
        rows = conn.execute(
            f"""SELECT b.*, a.label AS account_label FROM bookings b
                LEFT JOIN accounts a ON a.id=b.account_id
                WHERE b.manage_token IN ({marks}) ORDER BY b.start_at DESC LIMIT 30""",
            tuple(tokens),
        ).fetchall()
        out = []
        for row in rows:
            item = attach_share(booking_row(row, row["account_label"] or ""))
            item["manage_token"] = row["manage_token"]
            out.append(item)
        return out
    finally:
        conn.close()


def cancel_booking(booking_id: int, token: Optional[str] = None, actor: str = "",
                   ip: str = "", call_upstream: bool = True) -> dict:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not row:
            raise ValueError("预约不存在")
        if token is not None and token != row["manage_token"]:
            raise PermissionError("管理口令不匹配")
        if row["status"] in {"cancelled", "done", "failed"}:
            raise ValueError("该预约已经结束了，无需取消")
        acc_row = conn.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone()
        note = ""
        if call_upstream and acc_row and row["meeting_code"] and acc_row["driver"] == "mcp" and acc_row["token_cipher"]:
            try:
                driver = get_driver("mcp")
                driver.cancel(account_row(acc_row), decrypt_secret(acc_row["token_cipher"]), dict(row))
            except Exception as exc:  # noqa: BLE001
                note = f"上游取消失败（本地已释放）：{exc}"[:180]
        conn.execute(
            "UPDATE bookings SET status='cancelled', updated_at=?, fail_reason=? WHERE id=?",
            (iso(now()), note, booking_id),
        )
        audit(conn, "booking.cancel", actor or row["requester"], f"booking:{booking_id}",
              note[:160], ip)
        return {"id": booking_id, "status": "cancelled", "note": note}
    finally:
        conn.close()


def reap() -> int:
    """把到点/过期的预约推进状态，释放账号占用。"""
    conn = connect()
    try:
        current = now().isoformat(timespec="seconds")
        changed = conn.execute(
            "UPDATE bookings SET status='active', updated_at=? WHERE status='pending' AND start_at<=? AND end_at>?",
            (iso(now()), current, current),
        ).rowcount
        changed += conn.execute(
            "UPDATE bookings SET status='done', updated_at=? WHERE status IN ('pending','active') AND end_at<=?",
            (iso(now()), current),
        ).rowcount
        return int(changed or 0)
    finally:
        conn.close()


# --------------------------------------------------------------------------- 看板


def public_state() -> dict:
    conn = connect()
    try:
        cfg = get_settings(conn)
        current = now()
        total = conn.execute("SELECT COUNT(*) c FROM accounts WHERE status='active'").fetchone()["c"]
        busy = conn.execute(
            "SELECT COUNT(DISTINCT account_id) c FROM bookings WHERE status IN ('pending','active') AND start_at<=? AND end_at>=?",
            (current.isoformat(timespec="seconds"), current.isoformat(timespec="seconds")),
        ).fetchone()["c"]
        today = current.date().isoformat()
        today_count = conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE status IN ('pending','active','done') AND start_at>=? AND start_at<=?",
            (f"{today}T00:00:00", f"{today}T23:59:59"),
        ).fetchone()["c"]
        return {
            "accounts_active": total,
            "accounts_idle_now": max(0, total - busy),
            "today_bookings": today_count,
            "buffer_minutes": cfg["buffer_minutes"],
            "max_per_week": cfg["max_per_week"],
            "window_days": cfg["window_days"],
            "min_minutes": cfg["min_minutes"],
            "max_minutes": cfg["max_minutes"],
            "work_hour_start": cfg["work_hour_start"],
            "work_hour_end": cfg["work_hour_end"],
            "notice": cfg["notice"],
            "server_time": iso(current),
        }
    finally:
        conn.close()


def day_timeline(day: Optional[str] = None) -> dict:
    conn = connect()
    try:
        target = date.fromisoformat(day) if day else now().date()
        accounts = conn.execute("SELECT * FROM accounts WHERE status='active' ORDER BY id").fetchall()
        rows = conn.execute(
            """SELECT b.*, a.label AS account_label FROM bookings b
               LEFT JOIN accounts a ON a.id=b.account_id
               WHERE b.status IN ('pending','active','done') AND b.start_at>=? AND b.start_at<=?
               ORDER BY b.start_at""",
            (f"{target.isoformat()}T00:00:00", f"{target.isoformat()}T23:59:59"),
        ).fetchall()
        blocks: dict[int, list[dict]] = {}
        for r in rows:
            blocks.setdefault(r["account_id"], []).append({
                "id": r["id"],
                "subject": r["subject"],
                "requester": r["requester"],
                "start_at": r["start_at"],
                "end_at": r["end_at"],
                "status": r["status"],
            })
        return {
            "date": target.isoformat(),
            "accounts": [
                {
                    "id": a["id"],
                    "label": a["label"],
                    "tier": a["tier"],
                    "status": a["status"],
                    "health": a["health"],
                    "blocks": blocks.get(a["id"], []),
                }
                for a in accounts
            ],
        }
    finally:
        conn.close()


def admin_overview() -> dict:
    conn = connect()
    try:
        cfg = get_settings(conn)
        current = now()
        week_ago = (current - timedelta(days=7)).isoformat(timespec="seconds")
        today = current.date().isoformat()
        accounts = list_accounts(conn)
        active_accounts = [a for a in accounts if a["status"] == "active"]

        def scalar(sql: str, args=()) -> int:
            row = conn.execute(sql, args).fetchone()
            return int(row[0] or 0) if row else 0

        today_count = scalar(
            "SELECT COUNT(*) FROM bookings WHERE status IN ('pending','active','done') AND start_at>=? AND start_at<=?",
            (f"{today}T00:00:00", f"{today}T23:59:59"),
        )
        today_minutes = sum(
            int((parse_dt(r["end_at"]) - parse_dt(r["start_at"])).total_seconds() // 60)
            for r in conn.execute(
                "SELECT start_at, end_at FROM bookings WHERE status IN ('pending','active','done') AND start_at>=? AND start_at<=?",
                (f"{today}T00:00:00", f"{today}T23:59:59"),
            ).fetchall()
        )
        week_total = scalar(
            "SELECT COUNT(*) FROM bookings WHERE status IN ('pending','active','done') AND start_at>=?",
            (week_ago,),
        )
        week_failed = scalar(
            "SELECT COUNT(*) FROM bookings WHERE status='failed' AND created_at>=?", (week_ago,)
        )
        hour_rows = conn.execute(
            "SELECT start_at FROM bookings WHERE status IN ('pending','active','done') AND start_at>=?",
            (week_ago,),
        ).fetchall()
        hours = {h: 0 for h in range(cfg["work_hour_start"], cfg["work_hour_end"])}
        for r in hour_rows:
            h = parse_dt(r["start_at"]).hour
            if h in hours:
                hours[h] += 1
        upcoming = conn.execute(
            """SELECT b.*, a.label AS account_label FROM bookings b
               LEFT JOIN accounts a ON a.id=b.account_id
               WHERE b.status IN ('pending','active') AND b.end_at>=?
               ORDER BY b.start_at LIMIT 8""",
            (current.isoformat(timespec="seconds"),),
        ).fetchall()
        return {
            "accounts_total": len(accounts),
            "accounts_active": len(active_accounts),
            "accounts_idle_now": sum(1 for a in active_accounts if not a["busy_now"]),
            "accounts_attention": sum(1 for a in accounts if a["health"] in {"bad", "warn"} or (not a["has_token"] and a["driver"] == "mcp")),
            "today_bookings": today_count,
            "today_minutes": today_minutes,
            "week_bookings": week_total,
            "week_failed": week_failed,
            "failure_rate": round(week_failed / max(1, week_total + week_failed) * 100, 1),
            "hour_heat": [{"hour": h, "count": c} for h, c in hours.items()],
            "account_usage": sorted(
                [{"label": a["label"], "week_count": a["week_count"], "week_minutes": a["week_minutes"],
                  "status": a["status"], "health": a["health"]} for a in accounts],
                key=lambda x: -x["week_count"],
            )[:12],
            "upcoming": [attach_share(booking_row(r, r["account_label"] or "")) for r in upcoming],
            "config": cfg,
            "server_time": iso(current),
        }
    finally:
        conn.close()


def list_bookings(day: Optional[str] = None, status: Optional[str] = None,
                  keyword: str = "", limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        sql = """SELECT b.*, a.label AS account_label FROM bookings b
                 LEFT JOIN accounts a ON a.id=b.account_id WHERE 1=1"""
        args: list[Any] = []
        if day:
            sql += " AND b.start_at>=? AND b.start_at<=?"
            args += [f"{day}T00:00:00", f"{day}T23:59:59"]
        if status:
            sql += " AND b.status=?"
            args.append(status)
        if keyword:
            sql += " AND (b.subject LIKE ? OR b.requester LIKE ? OR b.meeting_code LIKE ?)"
            like = f"%{keyword}%"
            args += [like, like, like]
        sql += " ORDER BY b.start_at DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [attach_share(booking_row(r, r["account_label"] or "")) for r in rows]
    finally:
        conn.close()


def export_bookings_csv(day_from: str = "", day_to: str = "") -> str:
    conn = connect()
    try:
        sql = """SELECT b.*, a.label AS account_label FROM bookings b
                 LEFT JOIN accounts a ON a.id=b.account_id WHERE 1=1"""
        args: list[Any] = []
        if day_from:
            sql += " AND b.start_at>=?"
            args.append(f"{day_from}T00:00:00")
        if day_to:
            sql += " AND b.start_at<=?"
            args.append(f"{day_to}T23:59:59")
        sql += " ORDER BY b.start_at DESC"
        rows = conn.execute(sql, tuple(args)).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["预约ID", "主题", "借用人", "部门", "联系方式", "账号", "开始", "结束",
                         "状态", "会议号", "入会链接", "密码", "创建时间"])
        for r in rows:
            writer.writerow([r["id"], r["subject"], r["requester"], r["dept"], r["contact"],
                             r["account_label"] or "", r["start_at"], r["end_at"], r["status"],
                             r["meeting_code"], r["join_url"], r["password"], r["created_at"]])
        return buf.getvalue()
    finally:
        conn.close()


def list_audit(limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_config(patch: dict, actor: str = "admin") -> dict:
    conn = connect()
    try:
        cfg = update_settings(conn, patch)
        audit(conn, "config.update", actor, "settings", str(patch)[:180])
        return cfg
    finally:
        conn.close()
