"""把 /tenxunhuiyiyuding/ 从「静态预约页 + 18084 后端」切到「号池 8099」。

幂等：重复跑不会再改；改前自动备份，nginx -t 失败自动回滚。
在服务器上以 root 执行：python3 patch_nginx.py
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

CONF = pathlib.Path(os.environ.get("NGINX_CONF", "/etc/nginx/conf.d/site.conf"))
BLOCK = pathlib.Path(os.environ.get(
    "POOL_NGINX_BLOCK", pathlib.Path(__file__).with_name("nginx-location.conf")))
PROXY_MARK = "proxy_pass http://127.0.0.1:8099/"

OLD_API = re.compile(r"\n[ \t]*location \^~ /tenxunhuiyiyuding/api/ \{.*?\n[ \t]*\}\n", re.S)
OLD_PAGE = re.compile(r"\n[ \t]*location \^~ /tenxunhuiyiyuding/ \{.*?\n[ \t]*\}\n", re.S)


def main() -> int:
    text = CONF.read_text(encoding="utf-8")
    if PROXY_MARK in text:
        print("已经是号池路由，无需修改")
        return 0

    backup = CONF.with_name(f"relay-production.conf.bak-meeting-pool-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(CONF, backup)
    print("备份:", backup)

    # 先删旧的 api 转发（18084），再换静态 alias 为反向代理
    patched = OLD_API.sub("\n", text)
    block = BLOCK.read_text(encoding="utf-8")
    patched, count = OLD_PAGE.subn("\n" + block, patched, count=1)
    if count != 1:
        print("没找到 /tenxunhuiyiyuding/ 的 location 块，放弃（未修改任何文件）")
        return 2

    CONF.write_text(patched, encoding="utf-8")
    test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if test.returncode != 0:
        CONF.write_text(text, encoding="utf-8")
        print("nginx -t 失败，已回滚：", test.stderr.strip()[:400])
        return 3
    reload_proc = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
    print("nginx -t ok，reload:", "ok" if reload_proc.returncode == 0 else reload_proc.stderr[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
