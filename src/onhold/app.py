"""The web app: /mcp (behind Cloudflare Access), the ElevenLabs webhook and /healthz.

On Cloudflare the Worker hands each request its bindings in ``scope["env"]``
(secrets, vars and the ``DB`` D1 database). Run anywhere else (``uvicorn
onhold.app:app``) it reads os.environ and keeps a SQLite file at ``ONHOLD_DB``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from onhold import access, webhook
from onhold.calls import Service
from onhold.config import ConfigError, Settings
from onhold.elevenlabs import ElevenLabs
from onhold.mcp_server import current_service, http_app
from onhold.store import D1Store, SqliteStore, Store

log = logging.getLogger(__name__)
logging.getLogger("mcp").setLevel(logging.WARNING)

_local_store: SqliteStore | None = None


def _env(scope: Scope) -> Any:
    return scope.get("env") or dict(os.environ)


def _store(env: Any) -> Store:
    db = env.get("DB") if isinstance(env, dict) else getattr(env, "DB", None)
    if db is not None and not isinstance(db, str):
        return D1Store(db)
    global _local_store
    if _local_store is None:
        _local_store = SqliteStore(os.environ.get("ONHOLD_DB", "onhold.db"))
        _local_store.migrate()
    return _local_store


def build_service(env: Any, store: Store | None = None) -> Service:
    settings = Settings.load(env)
    return Service(store or _store(env), ElevenLabs(settings.elevenlabs_api_key), settings)


# Tests swap this for a function returning a Service wired to fakes.
service_factory = build_service


async def healthz(request: Request):
    try:
        settings = Settings.load(_env(request.scope))
    except ConfigError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return JSONResponse({"ok": True, "paused": settings.paused})


async def elevenlabs_webhook(request: Request):
    body = await request.body()
    try:
        service = service_factory(_env(request.scope))
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    try:
        webhook.verify(
            service.settings.webhook_secret,
            body,
            request.headers.get(webhook.HEADER),
            int(time.time()),
        )
    except webhook.BadSignature as e:
        log.warning("webhook refused: %s", e)
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "not JSON"}, status_code=400)
    result = await service.ingest_webhook(payload)
    log.info("webhook: %s", result)
    return JSONResponse({"ok": True})


app = Starlette(
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/webhooks/elevenlabs", elevenlabs_webhook, methods=["POST"]),
    ]
)


async def _deny(send: Send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def guarded_mcp(scope: Scope, receive: Receive, send: Send) -> None:
    """Check the Access token, then serve the MCP request with this request's Service."""
    if scope["type"] != "http":
        return await http_app(scope, receive, send)
    env = _env(scope)
    try:
        service = service_factory(env)
    except ConfigError as e:
        return await _deny(send, 503, str(e))
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    try:
        await access.verify(
            headers.get(access.HEADER),
            team_domain=service.settings.access_team_domain,
            aud=service.settings.access_aud,
            owner_email=service.settings.owner_email,
        )
    except access.AccessDenied as e:
        log.warning("mcp refused: %s", e)
        return await _deny(send, 403, "forbidden")
    token = current_service.set(service)
    try:
        await http_app(scope, receive, send)
    finally:
        current_service.reset(token)


class _Router:
    """/mcp and /mcp/ go to the guarded MCP app; everything else to the routes above."""

    def __init__(self, routes: Starlette):
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in ("/mcp", "/mcp/"):
            return await guarded_mcp(dict(scope, path="/mcp/"), receive, send)
        return await self.routes(scope, receive, send)


asgi_app = _Router(app)
