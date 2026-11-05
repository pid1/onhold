"""Cloudflare Access in front of /mcp, checked again here.

Access (with Managed OAuth) runs the sign-in with Claude, and only requests
carrying a token it issued reach the Worker. onhold does not take that on
faith: every MCP request must carry the ``Cf-Access-Jwt-Assertion`` header
Access adds, RS256-signed by one of the team's keys, issued by the team domain
and for this application's AUD, and not expired. If ``OWNER_EMAIL`` is set, it
must be that person. So a mistake in the Access setup cannot open the phone
to anyone else.

Without ``ACCESS_TEAM_DOMAIN`` and ``ACCESS_AUD`` every MCP request is refused.
"""

from __future__ import annotations

import base64
import binascii
import json
import time

import httpx

HEADER = "cf-access-jwt-assertion"
KEYS_TTL = 3600
LEEWAY = 60
_keys: dict[str, tuple[float, dict[str, dict]]] = {}


class AccessDenied(Exception):
    pass


def _b64url(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


async def _signing_keys(team_domain: str, transport=None) -> dict[str, dict]:
    """The team's public keys as JWKs, by key id."""
    cached = _keys.get(team_domain)
    if cached and time.monotonic() - cached[0] < KEYS_TTL:
        return cached[1]
    async with httpx.AsyncClient(transport=transport, timeout=10) as client:
        resp = await client.get(f"https://{team_domain}/cdn-cgi/access/certs")
    resp.raise_for_status()
    keys = {
        k["kid"]: k for k in resp.json().get("keys", []) if k.get("kty") == "RSA" and "kid" in k
    }
    _keys[team_domain] = (time.monotonic(), keys)
    return keys


def rs256_verify(jwk: dict, data: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 with SHA-256, as JWT's RS256."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    n = int.from_bytes(_b64url(jwk["n"]), "big")
    e = int.from_bytes(_b64url(jwk["e"]), "big")
    try:
        RSAPublicNumbers(e, n).public_key().verify(
            signature, data, padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature:
        return False
    return True


async def verify(
    token: str | None,
    *,
    team_domain: str | None,
    aud: str | None,
    owner_email: str | None = None,
    transport=None,
    now: float | None = None,
) -> dict:
    """The verified claims, or AccessDenied with a short reason (never the token)."""
    if not team_domain or not aud:
        raise AccessDenied("Access is not configured (ACCESS_TEAM_DOMAIN, ACCESS_AUD)")
    if not token:
        raise AccessDenied("no Access token")
    try:
        head_b64, body_b64, sig_b64 = token.split(".")
        header = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(body_b64))
        signature = _b64url(sig_b64)
    except ValueError, binascii.Error:
        raise AccessDenied("malformed Access token") from None
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise AccessDenied("malformed Access token")
    if header.get("alg") != "RS256":
        raise AccessDenied("unexpected token algorithm")
    keys = await _signing_keys(team_domain, transport)
    jwk = keys.get(header.get("kid"))
    if jwk is None:
        _keys.pop(team_domain, None)  # keys may have rotated; refetch next time
        raise AccessDenied("unknown signing key")
    if not rs256_verify(jwk, f"{head_b64}.{body_b64}".encode(), signature):
        raise AccessDenied("bad signature")
    now = time.time() if now is None else now
    exp, iat = claims.get("exp"), claims.get("iat")
    if not isinstance(exp, int | float) or exp < now - LEEWAY:
        raise AccessDenied("expired token")
    if not isinstance(iat, int | float) or iat > now + LEEWAY:
        raise AccessDenied("token issued in the future")
    audiences = claims.get("aud")
    audiences = [audiences] if isinstance(audiences, str) else audiences or []
    if aud not in audiences:
        raise AccessDenied("token is for another application")
    if claims.get("iss") != f"https://{team_domain}":
        raise AccessDenied("token from another issuer")
    if owner_email and str(claims.get("email") or "").lower() != owner_email.lower():
        raise AccessDenied("signed in as someone other than the owner")
    return claims
