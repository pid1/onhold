"""Checking an ElevenLabs webhook's signature.

ElevenLabs signs each webhook with the workspace's webhook secret and sends
``ElevenLabs-Signature: t=<unix seconds>,v0=<hex HMAC-SHA256 of "<t>.<body>">``.
A request is accepted only with a valid signature made within ``TOLERANCE``
of now, so a captured webhook cannot be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac

HEADER = "elevenlabs-signature"
TOLERANCE = 30 * 60


class BadSignature(Exception):
    pass


def sign(secret: str, body: bytes, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v0={mac.hexdigest()}"


def verify(secret: str, body: bytes, header: str | None, now: int) -> None:
    if not header:
        raise BadSignature("no signature")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        timestamp = int(parts.get("t", ""))
    except ValueError:
        raise BadSignature("no timestamp") from None
    if abs(now - timestamp) > TOLERANCE:
        raise BadSignature("timestamp outside tolerance")
    expected = sign(secret, body, timestamp).split("v0=", 1)[1]
    if not hmac.compare_digest(expected, parts.get("v0", "")):
        raise BadSignature("signature mismatch")
