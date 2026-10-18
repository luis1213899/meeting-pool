"""运行配置：环境变量 + 落盘的本地密钥。"""
from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TZ_NAME = "Asia/Shanghai"


def load_env(path: Path | None = None) -> None:
    """极简 .env 读取，不引入额外依赖。已存在的环境变量优先。"""
    path = path or BASE_DIR / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


def _load_local_secret() -> dict:
    path = BASE_DIR / ".pool_secret"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    data = {
        "master_key": base64.b64encode(os.urandom(32)).decode(),
        "session_secret": secrets.token_hex(32),
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8099
    admin_user: str = "admin"
    admin_password: str = "admin123"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "pool.db")
    master_key: str = ""
    session_secret: str = ""
    mcp_url: str = "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
    mcp_tool: str = "schedule_meeting"
    mcp_cancel_tool: str = "cancel_meeting"
    mcp_list_tool: str = "get_user_meetings"
    mcp_token_header: str = "X-Tencent-Meeting-Token"
    mcp_skill_version: str = "v1.0.14"
    # 官方参数名是 time_zone（带下划线），且所有工具都要 _client_info（代码里自动注入）
    mcp_args_template: str = (
        '{"subject":"{subject}","start_time":"{start}","end_time":"{end}",'
        '"password":"{password}","time_zone":"{timezone}"}'
    )
    mcp_timeout: float = 20.0
    demo_mode: bool = False

    @property
    def default_admin_password(self) -> bool:
        return self.admin_password in {"admin123", "请改成你自己的强密码", ""}


def load_settings() -> Settings:
    load_env()
    secret = _load_local_secret()
    s = Settings()
    s.host = os.environ.get("POOL_HOST", s.host)
    s.port = int(os.environ.get("POOL_PORT", s.port))
    s.admin_user = os.environ.get("POOL_ADMIN_USER", s.admin_user)
    s.admin_password = os.environ.get("POOL_ADMIN_PASSWORD", s.admin_password)
    raw_db = os.environ.get("POOL_DB_PATH", "").strip()
    if raw_db:
        p = Path(raw_db)
        s.db_path = p if p.is_absolute() else (BASE_DIR / p)
    s.master_key = os.environ.get("POOL_MASTER_KEY", "").strip() or secret["master_key"]
    s.session_secret = secret["session_secret"]
    s.mcp_url = os.environ.get("POOL_MCP_URL", s.mcp_url).strip()
    s.mcp_tool = os.environ.get("POOL_MCP_TOOL", s.mcp_tool).strip()
    s.mcp_cancel_tool = os.environ.get("POOL_MCP_CANCEL_TOOL", s.mcp_cancel_tool).strip()
    s.mcp_list_tool = os.environ.get("POOL_MCP_LIST_TOOL", s.mcp_list_tool).strip()
    s.mcp_token_header = os.environ.get("POOL_MCP_TOKEN_HEADER", s.mcp_token_header).strip()
    s.mcp_skill_version = os.environ.get("POOL_MCP_SKILL_VERSION", s.mcp_skill_version).strip()
    s.mcp_args_template = os.environ.get("POOL_MCP_ARGS", s.mcp_args_template).strip()
    s.mcp_timeout = float(os.environ.get("POOL_MCP_TIMEOUT", s.mcp_timeout))
    s.demo_mode = _bool("POOL_DEMO", False)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    return s


SETTINGS = load_settings()
