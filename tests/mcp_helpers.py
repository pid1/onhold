"""Call onhold's MCP tools in process, through the real ASGI app, with Access faked."""

from __future__ import annotations

import json

import httpx
from conftest import ENV

from onhold import access, app

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def mcp_client(service, monkeypatch) -> httpx.AsyncClient:
    async def allow(*args, **kwargs):
        return {"email": "owner@example.com"}

    monkeypatch.setattr(app, "service_factory", lambda env: service)
    monkeypatch.setattr(app, "_env", lambda scope: ENV)
    monkeypatch.setattr(access, "verify", allow)
    transport = httpx.ASGITransport(app=app.asgi_app)
    return httpx.AsyncClient(transport=transport, base_url="https://onhold.example.com")


async def rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = await client.post("/mcp", json=body, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


async def call_tool(client: httpx.AsyncClient, name: str, arguments: dict) -> dict:
    """The tool's result: {"error": payload} for an isError result, else its JSON."""
    result = (await rpc(client, "tools/call", {"name": name, "arguments": arguments}))["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return {"error": json.loads(text)}
    return result.get("structuredContent") or json.loads(text)
