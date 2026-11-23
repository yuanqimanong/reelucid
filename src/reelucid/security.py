from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from reelucid.domain import ReelucidError
from reelucid.settings import AppConfig


def secret_file(path: Path, value: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value + "\n")
    return value


class Security:
    def __init__(self, config: AppConfig) -> None:
        self.token = os.environ.get("REELUCID_ADMIN_TOKEN") or secret_file(
            config.admin_token_file, secrets.token_urlsafe(32)
        )
        if len(self.token) < 24:
            raise ReelucidError("WEAK_ADMIN_TOKEN", "管理员 Token 至少需要 24 个字符。")
        key = secret_file(config.master_key_file, base64.urlsafe_b64encode(os.urandom(32)).decode())
        try:
            self.cipher = AESGCM(base64.urlsafe_b64decode(key))
        except (ValueError, TypeError) as exc:
            raise ReelucidError("INVALID_MASTER_KEY", "主密钥格式错误，请恢复原密钥文件。") from exc
        self.sessions = URLSafeTimedSerializer(
            hashlib.sha256((key + self.token).encode()).hexdigest(), salt="reelucid-session-v1"
        )

    def valid_token(self, value: str) -> bool:
        return hmac.compare_digest(value.encode(), self.token.encode())

    def session(self) -> str:
        return self.sessions.dumps({"role": "admin"})

    def valid_session(self, value: str) -> bool:
        try:
            return self.sessions.loads(value, max_age=12 * 3600) == {"role": "admin"}
        except BadSignature, SignatureExpired:
            return False

    def encrypt(self, text: str) -> str:
        nonce = os.urandom(12)
        return base64.urlsafe_b64encode(
            nonce + self.cipher.encrypt(nonce, text.encode(), b"reelucid-secret-v1")
        ).decode()

    def decrypt(self, encrypted: str) -> str:
        try:
            value = base64.urlsafe_b64decode(encrypted)
            return self.cipher.decrypt(value[:12], value[12:], b"reelucid-secret-v1").decode()
        except (InvalidTag, ValueError, UnicodeError) as exc:
            raise ReelucidError(
                "CREDENTIAL_UNAVAILABLE", "凭据无法解密，请检查主密钥或重新配置。"
            ) from exc
