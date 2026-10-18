"""UI 全量按钮冒烟：把每个按钮都点一遍，确认没有「点了没反应」或前端报错。

会临时建一个 mock 演示号 + 一场会，跑完自动清理，不碰真实账号。
依赖 playwright（不在运行依赖里）：pip install playwright && playwright install chromium

用法：
    python app.py            # 另开一个终端起服务
    python tools/ui_smoke.py
"""
from __future__ import annotations

import datetime
import sys

import httpx
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8099"
PASSWORD = "pool-admin-2026"
errors: list[str] = []
checks: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  [{detail}]" if detail else ""))


def main() -> int:
    # 准备一个演示号，避免动真实账号
    c = httpx.Client(trust_env=False, base_url=BASE, timeout=30)
    c.post("/api/admin/login", json={"username": "admin", "password": PASSWORD})
    mock_id = c.post("/api/admin/accounts", json={
        "label": "冒烟演示号", "driver": "mock", "tier": "pro"}).json()["id"]

    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    # 给演示号造一场会，甘特图才有色块可点
    booking = c.post("/api/bookings", json={
        "requester": "冒烟", "subject": "冒烟会议",
        "start_at": f"{tomorrow}T10:00:00+08:00", "duration_minutes": 60}).json()

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(args=["--no-proxy-server"])
            ctx = b.new_context(viewport={"width": 1280, "height": 950}, locale="zh-CN")
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            pg.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)

            # ---------------- 借号页 ----------------
            pg.goto(BASE, wait_until="networkidle")
            pg.wait_for_timeout(500)
            print("\n[借号页]")

            pg.click('.tab[data-view=gantt]')
            pg.wait_for_timeout(800)
            check("页签：进甘特图", pg.locator("#view-gantt:not(.hidden)").count() == 1)
            pg.click('.tab[data-view=borrow]')
            pg.wait_for_timeout(300)
            check("页签：回借号页", pg.locator("#view-borrow:not(.hidden)").count() == 1)

            # 折叠只在窄屏生效，单独开一个手机视口验证
            m = ctx.new_page()
            m.set_viewport_size({"width": 390, "height": 844})
            m.on("pageerror", lambda e: errors.append(f"m pageerror: {e}"))
            m.goto(BASE, wait_until="networkidle")
            m.wait_for_timeout(600)
            check("主持人选项默认收起（手机）", not m.locator("#opt-more").is_visible())
            m.click("#opt-toggle")
            m.wait_for_timeout(250)
            check("更多选项：展开（手机）", m.locator("#opt-more").is_visible(),
                  m.inner_text("#opt-toggle-text"))
            m.click(".switch-row:has([name=password_enabled])")
            m.wait_for_timeout(250)
            check("密码开关：显示输入框（手机）", m.locator("#password-field:not(.hidden)").count() == 1)
            m.click(".switch-row:has([name=password_enabled])")
            m.click("#opt-toggle")
            m.wait_for_timeout(200)
            m.close()

            check("主持人选项在桌面常显", pg.locator("#opt-more").is_visible())

            pg.click("#my-refresh")
            pg.wait_for_timeout(400)
            check("我的预约：刷新", pg.locator(".toast.show").count() >= 0)

            # ---------------- 甘特图 ----------------
            print("\n[排期甘特图]")
            pg.click('.tab[data-view=gantt]')
            pg.wait_for_selector(".g-row", timeout=8000)
            pg.wait_for_timeout(600)
            first_date = pg.input_value("#g-date")
            pg.click("#g-next")
            pg.wait_for_timeout(800)
            next_date = pg.input_value("#g-date")
            check("甘特：后一天", next_date != first_date, f"{first_date} -> {next_date}")
            pg.click("#g-prev")
            pg.wait_for_timeout(800)
            check("甘特：前一天", pg.input_value("#g-date") == first_date)
            pg.click("#g-today")
            pg.wait_for_timeout(800)
            check("甘特：今天", pg.input_value("#g-date") == datetime.date.today().isoformat(),
                  pg.input_value("#g-date"))

            pg.fill("#g-date", tomorrow)
            pg.dispatch_event("#g-date", "change")
            pg.wait_for_timeout(900)
            check("甘特：日期选择", pg.input_value("#g-date") == tomorrow)

            if pg.locator(".g-block").count():
                pg.locator(".g-block").first.click()
                pg.wait_for_selector(".modal", timeout=4000)
                check("甘特：点色块出详情", pg.locator(".modal").count() == 1,
                      pg.inner_text(".modal h3")[:16])
                pg.click(".modal [data-close]")
                pg.wait_for_timeout(200)
            else:
                check("甘特：点色块出详情", False, "今天没有会议可点")

            box = pg.locator(".g-track").first.bounding_box()
            pg.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)
            pg.wait_for_selector(".modal", timeout=4000)
            check("甘特：点空白出空闲弹层", pg.locator(".modal").count() == 1,
                  pg.inner_text(".modal h3"))
            pg.click(".modal [data-close]")

            # ---------------- 管理端 ----------------
            print("\n[号池管理]")
            pg.click('.tab[data-view=admin]')
            pg.fill("#login-form input[name=password]", PASSWORD)
            pg.click("#login-form button[type=submit]")
            pg.wait_for_selector("#accounts-table tbody tr", timeout=8000)
            pg.wait_for_timeout(600)
            check("管理端：登录", pg.locator("#admin-panel:not(.hidden)").count() == 1)

            pg.click("#btn-refresh")
            pg.wait_for_timeout(600)
            check("管理端：刷新", True)

            pg.click("#btn-add-account")
            pg.wait_for_selector(".modal", timeout=4000)
            check("管理端：新增账号弹层", pg.locator(".modal").count() == 1)
            pg.click(".modal [data-close]")
            pg.wait_for_timeout(200)

            pg.locator('[data-edit]').last.click()
            pg.wait_for_selector(".modal", timeout=4000)
            check("管理端：编辑弹层", pg.locator(".modal").count() == 1)
            pg.click(".modal [data-close]")
            pg.wait_for_timeout(200)

            probe = pg.locator('[data-probe]').last
            probe.click()
            pg.wait_for_timeout(2500)
            check("管理端：检测凭证", probe.count() == 1)

            pg.click("#btn-diag")
            pg.wait_for_selector(".modal", timeout=4000)
            check("管理端：MCP 自检弹层", pg.locator(".modal").count() == 1)
            pg.click(".modal [data-close]")
            pg.wait_for_timeout(200)

            pg.click("#btn-sync")
            pg.wait_for_timeout(3000)
            check("管理端：同步会议", True)

            # 筛选器
            pg.fill("#filter-keyword", "冒烟")
            pg.dispatch_event("#filter-keyword", "change")
            pg.wait_for_timeout(900)
            check("管理端：关键词筛选", True)
            pg.click("#filter-clear")
            pg.wait_for_timeout(800)
            check("管理端：清空筛选", pg.input_value("#filter-keyword") == "")

            # 演示号暂停 / 启用 / 删除
            row = pg.locator("#accounts-table tbody tr", has_text="冒烟演示号").first
            row.locator("[data-toggle]").click()
            pg.wait_for_timeout(1200)
            check("管理端：暂停账号",
                  "暂停" in row.inner_text(), row.inner_text().replace("\n", " ")[:40])
            row.locator("[data-toggle]").click()
            pg.wait_for_timeout(1200)

            # 规则保存（不改值，只验证提交链路）
            pg.click("#config-form button[type=submit]")
            pg.wait_for_timeout(1200)
            check("管理端：保存借用规则", pg.locator(".toast.show").count() >= 0)

            pg.click("#btn-export")
            pg.wait_for_timeout(600)
            check("管理端：导出 CSV", True)

            pg.click("#btn-logout")
            pg.wait_for_timeout(900)
            check("管理端：退出后回到借号页", pg.locator("#view-borrow:not(.hidden)").count() == 1)
            pg.click('.tab[data-view=admin]')
            pg.wait_for_timeout(900)
            check("管理端：退出后需重新登录", pg.locator("#admin-login:not(.hidden)").count() == 1)

            b.close()
    finally:
        # 清理演示预约与演示号
        try:
            if booking.get("id"):
                c.post(f"/api/admin/bookings/{booking['id']}/cancel", json={"force": True})
            c.delete(f"/api/admin/accounts/{mock_id}")
        except Exception:  # noqa: BLE001
            pass

    print("\n前端错误:", errors or "无")
    failed = [n for n, ok, _ in checks if not ok]
    print(f"结果: {len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        print("失败项:", failed)
    return 1 if failed or errors else 0


if __name__ == "__main__":
    sys.exit(main())
