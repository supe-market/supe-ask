from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_callback_token() -> str:
    return secrets.token_urlsafe(32)


def hash_callback_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_callback_token(token: str, expected_hash: str | None) -> bool:
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_callback_token(token), expected_hash)
