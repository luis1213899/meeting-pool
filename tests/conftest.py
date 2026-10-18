"""测试环境：每个用例一个独立临时库 + 固定主密钥，绝不碰真实数据。"""
from __future__ import annotations

import base64
import itertools
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="meeting-pool-test-"))
os.environ["POOL_MASTER_KEY"] = base64.b64encode(b"t" * 32).decode()
os.environ["POOL_ADMIN_PASSWORD"] = "test-pass"
os.environ["POOL_ADMIN_USER"] = "admin"
os.environ["POOL_DB_PATH"] = str(_TMP / "bootstrap.db")
os.environ["POOL_DISABLE_SCHEDULER"] = "1"  # 测试里不开后台线程
os.environ.pop("POOL_DEMO", None)

import pytest  # noqa: E402

from pool import db as dbmod  # noqa: E402
from pool.config import SETTINGS  # noqa: E402

_counter = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    SETTINGS.db_path = _TMP / f"pool-{next(_counter)}.db"
    dbmod.init_db()
    # 测试绝不触发后台自动对账（否则会真连腾讯会议，还会跟用例抢写锁）
    c = dbmod.connect()
    try:
        c.execute("UPDATE settings SET v='0' WHERE k='sync_minutes'")
    finally:
        c.close()
    yield


@pytest.fixture
def conn():
    c = dbmod.connect()
    yield c
    c.close()
