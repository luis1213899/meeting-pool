"""Token 加密、口令校验、管理会话签名。

设计原则：账号 Token 只以密文落库，接口层永不回传明文。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import SETTINGS

_PREFIX = "v1"


def _key() -> bytes:
    raw = base64.b64decode(SETTINGS.master_key)
    if len(raw) != 32:
        raise ValueError("POOL_MASTER_KEY 必须是 32 字节的 base64")
    return raw


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    nonce = os.urandom(12)
    box = AESGCM(_key()).encrypt(nonce, plain.encode("utf-8"), b"meeting-pool")
    return f"{_PREFIX}:" + base64.b64encode(nonce + box).decode()


def decrypt_secret(cipher: str) -> str:
    if not cipher:
        return ""
    if not cipher.startswith(_PREFIX + ":"):
        raise ValueError("密文版本不支持")
    blob = base64.b64decode(cipher.split(":", 1)[1])
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], b"meeting-pool").decode("utf-8")


def mask_secret(value: str) -> str:
    """只保留尾 4 位，用于界面展示。"""
    if not value:
        return ""
    tail = value[-4:]
    return f"••••{tail}"


def hint_of(value: str) -> str:
    return mask_secret(value)


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt:" + base64.b64encode(salt).decode() + ":" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split(":")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


# 管理口令在进程内以 scrypt 形式缓存，避免每次请求都重算
_ADMIN_HASH = hash_password(SETTINGS.admin_password)


def check_admin(username: str, password: str) -> bool:
    if not hmac.compare_digest(username.strip(), SETTINGS.admin_user):
        return False
    return hmac.compare_digest(password, SETTINGS.admin_password) or verify_password(
        password, _ADMIN_HASH
    )


SESSION_TTL = 12 * 3600


def sign_session(username: str) -> str:
    issued = int(time.time())
    payload = f"{username}|{issued}"
    sig = hmac.new(SETTINGS.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session(token: str) -> Optional[str]:
    try:
        username, issued, sig = token.split("|")
    except ValueError:
        return None
    payload = f"{username}|{issued}"
    expect = hmac.new(SETTINGS.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    if int(time.time()) - int(issued) > SESSION_TTL:
        return None
    return username
