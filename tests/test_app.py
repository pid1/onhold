"""The HTTP surface: the webhook's signature check, the Access guard on /mcp, MCP itself."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest
from conftest import ENV, WEBHOOK_SECRET, brief, conversation
from cryptography.hazmat.primitives.asymmetric import rsa

from onhold import access, app, webhook

pytestmark = pytest.mark.anyio
TEAM = ENV["ACCESS_TEAM_DOMAIN"]
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setattr(app, "service_factory", lambda env: service)
    monkeypatch.setattr(app, "_env", lambda scope: ENV)
    transport = httpx.ASGITransport(app=app.asgi_app)
    return httpx.AsyncClient(transport=transport, base_url="https://onhold.example.com")


@pytest.fixture
def signing_key(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "kid1"

    async def keys(team_domain, transport=None):
        assert team_domain == TEAM
        return {"kid1": jwk}

    monkeypatch.setattr(access, "_signing_keys", keys)
    return key


def token(key, **claims) -> str:
    now = int(time.time())
    body = {
        "aud": ENV["ACCESS_AUD"],
        "iss": f"https://{TEAM}",
        "iat": now,
        "exp": now + 300,
        "email": "owner@example.com",
    } | claims
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": "kid1"})


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.json() == {"ok": True, "paused": False}


# Webhook ---------------------------------------------------------------------


async def test_an_unsigned_or_badly_signed_webhook_is_refused(client, service, api):
    body = json.dumps({"type": "post_call_transcription", "data": {}}).encode()
    assert (await client.post("/webhooks/elevenlabs", content=body)).status_code == 401
    bad = webhook.sign("wrong-secret", body, int(time.time()))
    r = await client.post("/webhooks/elevenlabs", content=body, headers={webhook.HEADER: bad})
    assert r.status_code == 401
    old = webhook.sign(WEBHOOK_SECRET, body, int(time.time()) - 3600)
    r = await client.post("/webhooks/elevenlabs", content=body, headers={webhook.HEADER: old})
    assert r.status_code == 401


async def test_a_signed_webhook_records_the_call(client, service, api, store):
    d = await service.draft_call(brief())
    api.route(
        "POST", "/v1/convai/twilio/outbound-call", {"success": True, "conversation_id": "conv_1"}
    )
    call_id = (await service.place_call(d["draft_id"], True))["call_id"]
    body = json.dumps({"type": "post_call_transcription", "data": conversation(call_id)}).encode()
    sig = webhook.sign(WEBHOOK_SECRET, body, int(time.time()))
    r = await client.post("/webhooks/elevenlabs", content=body, headers={webhook.HEADER: sig})
    assert r.status_code == 200
    assert (await store.get_call(call_id))["outcome"] == "goal_met"


# Access ----------------------------------------------------------------------


async def test_mcp_without_an_access_token_is_refused(client, signing_key):
    r = await client.post("/mcp", json=rpc("tools/list"), headers=MCP_HEADERS)
    assert r.status_code == 403


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-elses-app"},
        {"iss": "https://evil.cloudflareaccess.com"},
        {"exp": int(time.time()) - 300},
    ],
)
async def test_mcp_with_a_wrong_token_is_refused(client, signing_key, claims):
    headers = MCP_HEADERS | {access.HEADER: token(signing_key, **claims)}
    r = await client.post("/mcp", json=rpc("tools/list"), headers=headers)
    assert r.status_code == 403


async def test_unsigned_and_malformed_tokens_are_refused(signing_key):
    good = token(signing_key)
    head, body, _ = good.split(".")
    unsigned = jwt.encode({"aud": ENV["ACCESS_AUD"]}, None, algorithm="none")
    for bad, reason in [
        (unsigned, "algorithm"),
        (f"{head}.{body}.", "signature"),
        ("not.a.jwt", "malformed"),
        ("onlyonepart", "malformed"),
    ]:
        with pytest.raises(access.AccessDenied, match=reason):
            await access.verify(bad, team_domain=TEAM, aud=ENV["ACCESS_AUD"])


async def test_a_token_signed_by_another_key_is_refused(client, signing_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    headers = MCP_HEADERS | {access.HEADER: token(other)}
    assert (await client.post("/mcp", json=rpc("tools/list"), headers=headers)).status_code == 403


async def test_owner_email_limits_who_may_sign_in(signing_key):
    with pytest.raises(access.AccessDenied, match="other than the owner"):
        await access.verify(
            token(signing_key, email="someone@example.com"),
            team_domain=TEAM,
            aud=ENV["ACCESS_AUD"],
            owner_email="owner@example.com",
        )
    claims = await access.verify(
        token(signing_key), team_domain=TEAM, aud=ENV["ACCESS_AUD"], owner_email="Owner@Example.com"
    )
    assert claims["email"] == "owner@example.com"


async def test_access_must_be_configured():
    with pytest.raises(access.AccessDenied, match="not configured"):
        await access.verify("x", team_domain=None, aud=None)


# MCP -------------------------------------------------------------------------


async def test_mcp_lists_the_tools_and_drafts_a_call(client, signing_key, api):
    headers = MCP_HEADERS | {access.HEADER: token(signing_key)}
    r = await client.post(
        "/mcp",
        json=rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        ),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert "owner" in r.json()["result"]["instructions"]
    r = await client.post("/mcp", json=rpc("tools/list", id=2), headers=headers)
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {
        "draft_call",
        "place_call",
        "schedule_call",
        "cancel_call",
        "get_call",
        "list_calls",
        "delete_call",
        "get_menu_paths",
        "delete_menu_paths",
    }
    assert tools["get_menu_paths"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_menu_paths"]["annotations"]["destructiveHint"] is True
    assert tools["place_call"]["annotations"]["destructiveHint"] is True
    assert tools["get_call"]["annotations"]["readOnlyHint"] is True
    r = await client.post(
        "/mcp",
        json=rpc("tools/call", {"name": "draft_call", "arguments": {"brief": brief()}}, id=3),
        headers=headers,
    )
    result = r.json()["result"]
    assert not result.get("isError"), result
    drafted = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    preview = drafted["preview"]
    assert "Test Owner" in preview["first_message"]
    assert api.dials() == 0
    # place_call without confirm is refused, and dials nothing.
    r = await client.post(
        "/mcp",
        json=rpc(
            "tools/call",
            {
                "name": "place_call",
                "arguments": {
                    "draft_id": drafted["draft_id"],
                    "confirm": False,
                },
            },
            id=4,
        ),
        headers=headers,
    )
    assert r.json()["result"]["isError"] is True
    assert api.dials() == 0
