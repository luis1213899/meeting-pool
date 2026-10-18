"""演示数据工具。

    python seed_demo.py                     # 追加 5 个演示账号
    python seed_demo.py --reset             # 清空账号与预约后重建
    python seed_demo.py --reset --bookings  # 连今天的占用也一起造出来
    python seed_demo.py --purge             # 只删演示数据，保留真实账号
    python seed_demo.py --purge --apply     # 真正执行删除（默认是预演）

`--purge` 只清理 seed 造出来的东西：mock 演示号、seed 的固定号、它们的预约、
以及这些动作留下的审计记录。真实 MCP 账号、真实预约、真实操作日志一律不动。
"""
from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timedelta

from pool.db import TZ, audit, connect, init_db
from pool.security import encrypt_secret

DEMO = [
    dict(label="市场部 · 张伟的会员号", owner_name="张伟", owner_contact="微信 zw_mkt", tier="pro",
         driver="mock", note="年会/客户沟通常用", max_minutes=240, daily_limit=6),
    dict(label="销售部 · 李娜的会员号", owner_name="李娜", owner_contact="微信 lina_sales", tier="pro",
         driver="mock", note="客户演示专用，会前记得静音", max_minutes=180, daily_limit=4),
    dict(label="HR · 王芳的会员号", owner_name="王芳", owner_contact="135****8821", tier="biz",
         driver="mock", note="面试与培训，支持 300 人", capacity=300, max_minutes=480, daily_limit=3),
    dict(label="技术部 · 陈明的会员号", owner_name="陈明", owner_contact="微信 chenming_dev", tier="pro",
         driver="mock", note="技术评审", max_minutes=240, daily_limit=5),
    dict(label="行政 · 公共固定号", owner_name="行政部", owner_contact="内线 8012", tier="pro",
         driver="manual", fixed_code="826341057", note="固定会议号，随借随用", max_minutes=180,
         daily_limit=8),
]


def add_demo_bookings(conn) -> int:
    """造几条今天的占用，让时间轴看起来像真在用。"""
    accounts = conn.execute("SELECT id, label, driver FROM accounts ORDER BY id").fetchall()
    if not accounts:
        return 0
    today = datetime.now(TZ).date()
    plan = [
        (0, 9, 30, 45, "晨会 · 昨日数据复盘", "李娜", "销售部", "done"),
        (1, 10, 0, 60, "客户方案沟通（外部）", "周恒", "市场部", "done"),
        (2, 14, 0, 90, "新人培训", "王芳", "HR", "done"),
        (0, 16, 0, 30, "供应商比价", "陈明", "采购", "done"),
        (3, 20, 30, 60, "设计评审 · 首页改版", "赵一鸣", "设计部", "pending"),
        (4, 21, 0, 30, "临时对齐", "孙悦", "运营", "pending"),
    ]
    count = 0
    for idx, hh, mm, minutes, subject, who, dept, status in plan:
        acc = accounts[idx % len(accounts)]
        start = datetime.combine(today, datetime.min.time(), TZ) + timedelta(hours=hh, minutes=mm)
        end = start + timedelta(minutes=minutes)
        code = f"{secrets.randbelow(900) + 100}{secrets.randbelow(900) + 100}{secrets.randbelow(900) + 100}"
        conn.execute(
            """INSERT INTO bookings(account_id, requester, dept, contact, subject, start_at, end_at,
                 status, meeting_code, meeting_id, join_url, password, driver, manage_token, raw,
                 created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (acc["id"], who, dept, "", subject, start.isoformat(timespec="seconds"),
             end.isoformat(timespec="seconds"), status, code, f"demo-{secrets.token_hex(4)}",
             f"https://meeting.tencent.com/dm/{code}", f"{secrets.randbelow(900000) + 100000}",
             acc["driver"], secrets.token_urlsafe(18),
             '{"demo": true}', start.isoformat(timespec="seconds"), start.isoformat(timespec="seconds")),
        )
        count += 1
    audit(conn, "seed.bookings", "system", "bookings", f"{count} 条演示预约")
    return count


FIXTURE_HINTS = ("冒烟", "自检", "示例", "demo-token")


def purge_demo(conn, apply: bool = False) -> dict:
    """删除演示/测试数据。真实账号、真实预约、真实操作日志一律不动。

    清理三类：
    1. seed 造的演示账号（mock 驱动或 seed 固定名称）及其预约
    2. 联调夹具账号（名称含「冒烟 / 自检 / 示例」）
    3. 孤儿审计：预约早已被删、却还留着的 booking.* 记录
    """
    labels = [item["label"] for item in DEMO]
    marks = ",".join("?" * len(labels))
    rows = conn.execute(
        f"""SELECT id, label, driver FROM accounts
            WHERE driver='mock' OR label IN ({marks})""",
        tuple(labels),
    ).fetchall()
    ids = [r["id"] for r in rows]
    id_marks = ",".join("?" * len(ids)) if ids else ""

    bookings = []
    if ids:
        bookings = conn.execute(
            f"SELECT id, subject, requester FROM bookings WHERE account_id IN ({id_marks})",
            tuple(ids),
        ).fetchall()

    # 审计：seed 自身 + 落在演示账号上的借用/账号操作（括号必须包住 OR，否则会误删真实记录）
    audit_ids = [
        r["id"] for r in conn.execute("SELECT id FROM audit_logs WHERE action LIKE 'seed.%'")
    ]
    if labels:
        like = " OR ".join(["detail LIKE ?"] * len(labels))
        audit_ids += [
            r["id"] for r in conn.execute(
                f"""SELECT id FROM audit_logs
                    WHERE (action LIKE 'booking.%' OR action LIKE 'account.%')
                      AND ({like})""",
                tuple(f"%{x}%" for x in labels),
            )
        ]

    # 联调夹具账号留下的账号操作日志
    hints_like = " OR ".join(["target LIKE ? OR detail LIKE ?"] * len(FIXTURE_HINTS))
    hint_args: list[str] = []
    for hint in FIXTURE_HINTS:
        hint_args += [f"%{hint}%", f"%{hint}%"]
    audit_ids += [
        r["id"] for r in conn.execute(
            f"SELECT id FROM audit_logs WHERE ({hints_like})", tuple(hint_args)
        )
    ]

    # 孤儿预约日志：target 形如 booking:12，但 12 已经不在 bookings 里
    live_ids = {r["id"] for r in conn.execute("SELECT id FROM bookings")}
    for r in conn.execute("SELECT id, target FROM audit_logs WHERE action LIKE 'booking.%'"):
        target = (r["target"] or "").strip()
        if target.startswith("booking:"):
            try:
                if int(target.split(":", 1)[1]) not in live_ids:
                    audit_ids.append(r["id"])
            except ValueError:
                pass

    # 孤儿账号日志：account:N 指向已删除的账号
    live_accounts = {r["id"] for r in conn.execute("SELECT id FROM accounts")}
    for r in conn.execute("SELECT id, target FROM audit_logs WHERE action LIKE 'account.%'"):
        target = (r["target"] or "").strip()
        if target.startswith("account:"):
            try:
                if int(target.split(":", 1)[1]) not in live_accounts:
                    audit_ids.append(r["id"])
            except ValueError:
                pass

    # 调试期留下的重复登录 / 重复清理记录，各留一份（清理记录本身每次重写）
    for pattern, keep in (("admin.login", 3), ("purge.demo", 0)):
        extra = [
            r["id"] for r in conn.execute(
                f"SELECT id FROM audit_logs WHERE action=? ORDER BY id DESC LIMIT -1 OFFSET ?",
                (pattern, keep),
            )
        ]
        audit_ids += extra

    summary = {
        "accounts": [r["label"] for r in rows],
        "bookings": [f'#{b["id"]} {b["subject"]}' for b in bookings],
        "audit": len(set(audit_ids)),
    }
    if not apply:
        return summary

    if ids:
        conn.execute(f"DELETE FROM bookings WHERE account_id IN ({id_marks})", tuple(ids))
        conn.execute(f"DELETE FROM accounts WHERE id IN ({id_marks})", tuple(ids))
    if audit_ids:
        conn.execute(
            f"DELETE FROM audit_logs WHERE id IN ({','.join('?' * len(audit_ids))})",
            tuple(audit_ids),
        )
    audit(conn, "purge.demo", "admin", "demo",
          f'清理 {len(summary["accounts"])} 个演示账号、{len(summary["bookings"])} 条预约')
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="清空后重建")
    parser.add_argument("--bookings", action="store_true", help="同时造今天的演示占用")
    parser.add_argument("--purge", action="store_true", help="删除演示数据，保留真实账号")
    parser.add_argument("--apply", action="store_true", help="配合 --purge：真正执行删除")
    args = parser.parse_args()

    init_db()
    conn = connect()
    try:
        if args.purge:
            result = purge_demo(conn, apply=args.apply)
            head = "已清理" if args.apply else "预演（加 --apply 才会真删）"
            print(f"{head}：")
            print(f'  演示账号 {len(result["accounts"])} 个：' + ("、".join(result["accounts"]) or "无"))
            print(f'  预约 {len(result["bookings"])} 条：' + ("、".join(result["bookings"]) or "无"))
            print(f'  审计记录 {result["audit"]} 条')
            if not args.apply:
                print("\n确认无误后执行：python seed_demo.py --purge --apply")
            return
        if args.reset:
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM accounts")
            conn.execute("UPDATE sqlite_sequence SET seq=0 WHERE name IN ('bookings','accounts')")
        stamp = datetime.now(TZ).isoformat(timespec="seconds")
        for item in DEMO:
            token = "" if item["driver"] != "mcp" else "demo-token-" + secrets.token_hex(8)
            conn.execute(
                """INSERT INTO accounts(label, owner_name, owner_contact, tier, driver, token_cipher,
                     token_hint, fixed_code, capacity, max_minutes, daily_limit, status, health,
                     health_note, checked_at, note, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'active','ok','演示数据',?,?,?,?)""",
                (item["label"], item["owner_name"], item["owner_contact"], item["tier"],
                 item["driver"], encrypt_secret(token) if token else "",
                 ("••••" + token[-4:]) if token else "", item.get("fixed_code", ""),
                 item.get("capacity", 100), item["max_minutes"], item["daily_limit"],
                 stamp, item.get("note", ""), stamp, stamp),
            )
        audit(conn, "seed.demo", "system", "accounts", f"{len(DEMO)} 个演示账号")
        extra = ""
        if args.bookings:
            n = add_demo_bookings(conn)
            extra = f"，{n} 条今日演示占用"
        print(f"已写入 {len(DEMO)} 个演示账号{extra}。启动：python app.py")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
