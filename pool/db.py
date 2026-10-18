"""账号池数据层：SQLite，单文件，开箱即用。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import SETTINGS

TZ = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT    NOT NULL,
    owner_name    TEXT    NOT NULL DEFAULT '',
    owner_contact TEXT    NOT NULL DEFAULT '',
    tier          TEXT    NOT NULL DEFAULT 'pro',      -- free / pro / biz
    driver        TEXT    NOT NULL DEFAULT 'mcp',      -- mcp / manual / mock
    token_cipher  TEXT    NOT NULL DEFAULT '',
    token_hint    TEXT    NOT NULL DEFAULT '',
    fixed_code    TEXT    NOT NULL DEFAULT '',
    capacity      INTEGER NOT NULL DEFAULT 100,
    max_minutes   INTEGER NOT NULL DEFAULT 480,
    daily_limit   INTEGER NOT NULL DEFAULT 6,
    status        TEXT    NOT NULL DEFAULT 'active',   -- active / paused / disabled
    health        TEXT    NOT NULL DEFAULT 'unknown',  -- ok / warn / bad / unknown
    health_note   TEXT    NOT NULL DEFAULT '',
    checked_at    TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    requester    TEXT    NOT NULL,
    dept         TEXT    NOT NULL DEFAULT '',
    contact      TEXT    NOT NULL DEFAULT '',
    subject      TEXT    NOT NULL,
    start_at     TEXT    NOT NULL,
    end_at       TEXT    NOT NULL,
    status       TEXT    NOT NULL,                     -- pending / active / done / cancelled / failed
    meeting_code TEXT    NOT NULL DEFAULT '',
    meeting_id   TEXT    NOT NULL DEFAULT '',
    join_url     TEXT    NOT NULL DEFAULT '',
    password     TEXT    NOT NULL DEFAULT '',
    host_key     TEXT    NOT NULL DEFAULT '',
    driver       TEXT    NOT NULL DEFAULT '',
    manage_token TEXT    NOT NULL,
    fail_reason  TEXT    NOT NULL DEFAULT '',
    raw          TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE INDEX IF NOT EXISTS idx_bookings_slot ON bookings(account_id, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_bookings_status ON bookings(status, start_at);

CREATE TABLE IF NOT EXISTS audit_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    ip         TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "buffer_minutes": "5",       # 同一账号两场会之间的交接间隔
    "max_per_week": "5",         # 每人每周最多借几次
    "window_days": "14",         # 最多提前几天预约
    "min_minutes": "15",
    "max_minutes": "240",        # 单场最长时长（小时级会议请走正式流程）
    "work_hour_start": "8",
    "work_hour_end": "23",
    "notice": "用完请及时在「我的预约」里结束，方便同事接着用。",
}


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or SETTINGS.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """轻量迁移：老库缺列时补上，避免升级即报错。"""
    wanted = {
        "bookings": {
            "host_key": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in wanted.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(db_path: Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        now = datetime.now(TZ).isoformat(timespec="seconds")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (key, value))
        conn.execute("INSERT OR IGNORE INTO settings(k, v) VALUES('schema_version', '1')")
        conn.execute(
            "INSERT OR IGNORE INTO settings(k, v) VALUES('installed_at', ?)", (now,)
        )
    finally:
        conn.close()


def get_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT k, v FROM settings").fetchall()
    raw = {r["k"]: r["v"] for r in rows}
    out = dict(raw)
    for key in ("buffer_minutes", "max_per_week", "window_days", "min_minutes", "max_minutes",
                "work_hour_start", "work_hour_end"):
        try:
            out[key] = int(raw.get(key, DEFAULT_SETTINGS[key]))
        except (TypeError, ValueError):
            out[key] = int(DEFAULT_SETTINGS[key])
    return out


def update_settings(conn: sqlite3.Connection, patch: dict) -> dict:
    allowed = set(DEFAULT_SETTINGS)
    for key, value in patch.items():
        if key not in allowed:
            continue
        conn.execute(
            "INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )
    return get_settings(conn)


def audit(conn: sqlite3.Connection, action: str, actor: str = "", target: str = "",
          detail: str = "", ip: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_logs(actor, action, target, detail, ip, created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, target, detail, ip, datetime.now(TZ).isoformat(timespec="seconds")),
    )
