"""Seal directly entered provider keys for one Windows user and one endpoint."""

from __future__ import annotations

import base64
import re

from desktop.core import secret_protection

PROTECTED_KEY_FIELD = "wittycodeProtectedApiKey"
_MAX_KEY_LENGTH = 16_384
# A key must be safe as an HTTP header value, without transformation into an environment reference.
_KEY = re.compile(r"[\x21-\x7e]+")


class ProviderSecretError(Exception):
    pass


def validate_key(key: str) -> None:
    if len(key) > _MAX_KEY_LENGTH or not _KEY.fullmatch(key):
        raise ProviderSecretError("API Key 只能包含可见的 ASCII 字符，不能有空白或换行。")


def endpoint(kind: str, base_url: str) -> tuple[str, str]:
    # Both HTTP callers strip trailing slashes when building the request URL. Keep the rest exact:
    # grouping normalization is not a security boundary (e.g. it drops fragments and folds case).
    return kind, base_url.strip().rstrip("/")


def _entropy(kind: str, base_url: str) -> bytes:
    protocol, address = endpoint(kind, base_url)
    return f"WittyCode.ProviderApiKey.v1\0{protocol}\0{address}".encode("utf-8")


def seal(key: str, kind: str, base_url: str) -> str:
    validate_key(key)
    try:
        protected = secret_protection.crypt(key.encode("ascii"), entropy=_entropy(kind, base_url))
    except (secret_protection.ProtectionError, UnicodeError):
        raise ProviderSecretError("当前 Windows 用户无法加密 API Key。未保存更改，请重试或改用环境变量。") from None
    return base64.b64encode(protected).decode("ascii")


def unseal(value: str | None, kind: str, base_url: str) -> str:
    try:
        if not value:
            raise ValueError()
        encrypted = base64.b64decode(value, validate=True)
        key = secret_protection.crypt(encrypted, decrypt=True, entropy=_entropy(kind, base_url)).decode("ascii")
        validate_key(key)
    except (ValueError, secret_protection.ProtectionError, ProviderSecretError):
        raise ProviderSecretError("无法读取已保存的 API Key。请在原 Windows 用户和接口地址下使用，或重新输入密钥。") from None
    return key
