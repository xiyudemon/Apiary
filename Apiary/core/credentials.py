from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional


#: PBKDF2 迭代次数，兼顾本地登录耗时与暴力破解成本。
PBKDF2_ITERATIONS = 120000
_ALGORITHM = "sha256"
_PREFIX = "pbkdf2_sha256"


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """把明文密码转换为带随机盐的 PBKDF2 摘要串。

    摘要串格式为 ``pbkdf2_sha256$迭代次数$盐$摘要``，因此历史数据不受后续参数调整影响。
    """
    if not isinstance(password, str):
        raise TypeError("password 必须是字符串")
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{_PREFIX}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def is_hashed(stored: Optional[str]) -> bool:
    return isinstance(stored, str) and stored.startswith(_PREFIX + "$")


def verify_password(password: str, stored: Optional[str]) -> bool:
    """校验密码。

    兼容历史明文记录：命中明文时同样返回 True，由调用方决定是否立即迁移为摘要。
    """
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    if not is_hashed(stored):
        return stored == password

    try:
        _algorithm, iterations, salt_hex, digest_hex = stored.split("$", 3)
        rounds = int(iterations)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (TypeError, ValueError):
        return False

    digest = hashlib.pbkdf2_hmac(_ALGORITHM, password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(digest, expected)
