"""Fakes for everything outside the Worker: ElevenLabs, Pushover, Access, the clock.

No test talks to the network. Numbers use the fictional 555-01xx range and the
owner is "Test Owner".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from onhold.calls import Service
from onhold.config import Settings
from onhold.elevenlabs import ElevenLabs
from onhold.store import SqliteStore

API_KEY = "sk-test-not-a-real-key-123"
WEBHOOK_SECRET = "wsec_test_not_real"
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)  # 09:00 in America/Chicago (CST)

ENV = {
    "ELEVENLABS_API_KEY": API_KEY,
    "ELEVENLABS_WEBHOOK_SECRET": WEBHOOK_SECRET,
    "AGENT_ID": "agent_test",
    "PHONE_NUMBER_ID": "phnum_test",
    "ASSISTANT_NUMBER": "+15125550100",
    "OWNER_NAME": "Test Owner",
    "CALLBACK_NUMBER": "+15125550142",
    "PUSHOVER_TOKEN": "po_token_test",
    "PUSHOVER_USER": "po_user_test",
    "ACCESS_TEAM_DOMAIN": "example.cloudflareaccess.com",
    "ACCESS_AUD": "aud_test",
}


def brief(**overrides) -> dict:
    data = {
        "callee_name": "Example County Tax Office",
        "to_number": "+15125550123",
        "topic": "a licence plate renewal",
        "goal": "Find out whether the plate renewal can be done by mail and what it costs.",
        "questions": ["Can it be renewed by mail?", "What is the fee?"],
        "may_share": ["The car is a passenger vehicle registered in the county."],
        "must_not_share": ["The owner's employer"],
        "give_up_when": "they need the owner in person or an ID number",
        "callee_timezone": "America/Chicago",
    }
    data.update(overrides)
    return data


@dataclass
class FakeAPI:
    """ElevenLabs and Pushover. Unrouted requests get a 404, so nothing is silently fine."""

    routes: dict = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def route(self, method: str, path: str, body=None, status: int = 200) -> None:
        self.routes[(method, path)] = (status, body if body is not None else {})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"detail": f"no route {key}"})
        status, body = self.routes[key]
        return httpx.Response(status, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sent(self, method: str, path: str) -> list[dict]:
        out = []
        for r in self.requests:
            if r.method == method and r.url.path == path:
                try:
                    out.append(json.loads(r.content) if r.content else {})
                except ValueError:
                    out.append(dict(httpx.QueryParams(r.content.decode())))
        return out

    def dials(self) -> int:
        return len(self.sent("POST", "/v1/convai/twilio/outbound-call")) + len(
            self.sent("POST", "/v1/convai/batch-calling/submit")
        )


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api() -> FakeAPI:
    a = FakeAPI()
    a.route("POST", "/1/messages.json", {"status": 1})
    return a


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> SqliteStore:
    s = SqliteStore(":memory:")
    s.migrate()
    return s


@pytest.fixture
def settings() -> Settings:
    return Settings.load(ENV)


@pytest.fixture
def service(api, clock, store, settings) -> Service:
    return Service(
        store,
        ElevenLabs(API_KEY, transport=api.transport),
        settings,
        now=clock,
        pushover_transport=api.transport,
    )


def conversation(call_id: str, conv_id: str = "conv_1", **over) -> dict:
    """A finished conversation, as the webhook's data and GET /conversations/{id} carry it."""
    data = {
        "agent_id": "agent_test",
        "conversation_id": conv_id,
        "status": "done",
        "metadata": {
            "start_time_unix_secs": int(NOW.timestamp()) + 60,
            "call_duration_secs": 240,
            "termination_reason": "end_call tool was called",
            "cost": 1200,
        },
        "transcript": [
            {"role": "agent", "message": "Hi, I'm an AI assistant...", "time_in_call_secs": 0},
            {"role": "user", "message": "Tax office, how can I help?", "time_in_call_secs": 4},
        ],
        "analysis": {
            "transcript_summary": "Renewal can be done by mail; the fee is $51.75.",
            "data_collection_results": {
                "outcome": {"value": "goal_met"},
                "not_reached_reason": {"value": "none"},
                "answer_1": {"value": "Yes, by mail."},
                "answer_2": {"value": "$51.75"},
                "follow_ups": {"value": "Mail the form\nInclude a copy of the license"},
                "owner_facts_shared": {"value": "The car is registered in the county"},
            },
            "evaluation_criteria_results": {
                "disclosed_ai": {"result": "success"},
                "no_sensitive_info_shared": {"result": "success"},
            },
        },
        "conversation_initiation_client_data": {"dynamic_variables": {"onhold_call_id": call_id}},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    return data
