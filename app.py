"""会议号池入口。

    python app.py                     # 按 .env 里的 POOL_HOST / POOL_PORT 启动
    python app.py --port 9000         # 临时换端口
    python app.py --host 0.0.0.0      # 局域网内其他机器直接访问（注意口令强度）
"""
from __future__ import annotations

import argparse

import uvicorn

from pool.api import app
from pool.config import SETTINGS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="会议号池")
    parser.add_argument("--host", default=SETTINGS.host)
    parser.add_argument("--port", type=int, default=SETTINGS.port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run("pool.api:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, log_level="info")
