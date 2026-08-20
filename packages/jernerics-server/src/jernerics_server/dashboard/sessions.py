"""Signed dashboard session tokens: stdlib HMAC, no extra dependency.

A token is ``base64url(payload).base64url(hmac_sha256)`` over the compact
payload ``{"sub": "dashboard", "exp": <unix>, "jti": <uuid>}``. The signing
secret is 32 random bytes persisted beside the tracking database at
``dashboard_secret`` (0600, created on first start); it is never the API
key, never stored in the DB, and never logged.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path

DEFAULT_TTL_S = 12 * 60 * 60
SECRET_FILE_NAME = "dashboard_secret"
_SECRET_BYTES = 32


def load_or_create_secret(db_dir: str | Path) -> bytes:
    """Load the dashboard signing secret, creating it (0600) on first start."""
    path = Path(db_dir) / SECRET_FILE_NAME
    if path.exists():
        data = path.read_bytes()
        if data:
            return data
    data = secrets.token_bytes(_SECRET_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_bytes()
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return data


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


class SessionSigner:
    """Issues and verifies HMAC-SHA256 session tokens.

    ``verify`` is constant-time over the signature segment and rejects
    expired, malformed, and revoked tokens. Revocation (logout) keeps the
    token's ``jti`` in an in-process registry until its natural expiry;
    the server runs single-process, so that is the whole logout story.
    """

    def __init__(self, secret: bytes) -> None:
        self._secret = secret
        self._revoked: dict[str, int] = {}

    def sign(self, ttl_s: float = DEFAULT_TTL_S) -> str:
        payload = {
            "sub": "dashboard",
            "exp": int(time.time() + ttl_s),
            "jti": uuid.uuid4().hex,
        }
        segment = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(
            hmac.new(self._secret, segment.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{segment}.{signature}"

    def verify(self, token: str) -> bool:
        segment, separator, signature = token.partition(".")
        if not separator or not segment or not signature:
            return False
        expected = hmac.new(
            self._secret, segment.encode("ascii"), hashlib.sha256
        ).digest()
        encoded = _b64encode(expected).encode("ascii")
        if not hmac.compare_digest(encoded, signature.encode("ascii")):
            return False
        try:
            payload = json.loads(_b64decode(segment))
        except ValueError:
            return False
        if payload.get("sub") != "dashboard":
            return False
        exp = payload.get("exp")
        jti = payload.get("jti")
        if not isinstance(exp, int) or not isinstance(jti, str):
            return False
        if exp <= time.time():
            return False
        return jti not in self._revoked

    def revoke(self, token: str) -> None:
        """Drop the token's jti so it stops verifying (logout)."""
        segment, separator, _ = token.partition(".")
        if not separator:
            return
        try:
            payload = json.loads(_b64decode(segment))
        except ValueError:
            return
        exp = payload.get("exp")
        jti = payload.get("jti")
        if isinstance(exp, int) and isinstance(jti, str):
            self._revoked[jti] = exp
            self._prune()

    def _prune(self) -> None:
        now = time.time()
        for jti, exp in list(self._revoked.items()):
            if exp <= now:
                del self._revoked[jti]
