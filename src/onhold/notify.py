"""One Pushover message to the owner when a call ends. Best-effort: never raises."""

from __future__ import annotations

import logging

import httpx

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
log = logging.getLogger(__name__)


async def pushover(
    token: str | None,
    user: str | None,
    title: str,
    message: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    if not token or not user:
        return False
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            resp = await client.post(
                PUSHOVER_URL,
                data={
                    "token": token,
                    "user": user,
                    "title": title[:250],
                    "message": message[:1024],
                },
            )
        return resp.status_code == 200
    except httpx.HTTPError as e:
        log.warning("pushover failed: %s", type(e).__name__)
        return False
