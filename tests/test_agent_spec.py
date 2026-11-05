"""The voice agent as code, and the scripts that push it to ElevenLabs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import NOW, brief

from onhold import agent_spec, report
from onhold.brief import parse_brief, to_dynamic_variables

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import safety_tests  # noqa: E402
import setup  # noqa: E402


def variables() -> dict[str, str]:
    b = parse_brief(brief(), now=NOW)
    return to_dynamic_variables(b, "Test Owner", "+15125550100", "c_0123456789abcdef")


def test_every_variable_the_agent_uses_is_sent():
    spec = agent_spec.load()
    texts = [
        agent_spec.system_prompt(),
        agent_spec.first_message(),
        agent_spec.voicemail_message(),
        *(c["conversation_goal_prompt"] for c in spec["evaluation_criteria"]),
    ]
    used = set().union(*(agent_spec.variables_used(t) for t in texts))
    assert used <= set(variables())
    # ...and ElevenLabs has a placeholder for each, for its dashboard preview.
    assert used <= {"owner_name", *agent_spec.PLACEHOLDERS}


def test_render_fills_every_variable():
    text = agent_spec.render(agent_spec.first_message(), variables())
    assert "Test Owner" in text and "{{" not in text


def test_the_agent_reports_the_outcomes_onhold_understands():
    dc = agent_spec.load()["data_collection"]
    assert tuple(dc["outcome"]["enum"]) == report.OUTCOMES
    assert set(dc["not_reached_reason"]["enum"]) == {"none", *report.REASONS}
    assert {f"answer_{i}" for i in range(1, 11)} <= set(dc)
    assert "owner_facts_shared" in dc


def test_the_sensitive_info_check_sees_what_may_be_shared():
    [c] = [
        c for c in agent_spec.load()["evaluation_criteria"] if c["id"] == "no_sensitive_info_shared"
    ]
    assert "{{may_share}}" in c["conversation_goal_prompt"]


def test_nothing_personal_is_in_the_agent_files():
    spec = agent_spec.load()
    for key in ("owner_name", "phone_number_id", "rally_mcp"):
        assert key not in spec
    assert spec["pronunciation_rules"] == []


def test_the_body_carries_the_owner_and_no_recording():
    body = agent_spec.build_agent_body(owner_name="Test Owner")
    agent = body["conversation_config"]["agent"]
    assert agent["dynamic_variables"]["dynamic_variable_placeholders"]["owner_name"] == "Test Owner"
    assert agent["prompt"]["mcp_server_ids"] == []
    assert body["platform_settings"]["privacy"]["record_voice"] is False
    assert body["conversation_config"]["conversation"]["max_duration_seconds"] == 900


def test_the_webhook_is_attached_only_when_given():
    assert (
        "workspace_overrides"
        not in agent_spec.build_agent_body(owner_name="X")["platform_settings"]
    )
    body = agent_spec.build_agent_body(owner_name="X", webhook_id="wh_1")
    hooks = body["platform_settings"]["workspace_overrides"]["webhooks"]
    assert hooks == {
        "post_call_webhook_id": "wh_1",
        "events": ["transcript", "call_initiation_failure"],
    }


def test_the_hold_rule_counts_skip_turns():
    assert "fifth in a row" in agent_spec.system_prompt()
    assert agent_spec.load()["tools"]["skip_turn_wait_secs"] == 90


# Scripts ---------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v2/voices":
            return httpx.Response(
                200, json={"voices": [{"voice_id": "Cz0K1kOv9tD8l0b5Qu53", "name": "onhold - Jon"}]}
            )
        if path == "/v1/convai/agents":
            return httpx.Response(
                200, json={"agents": [{"agent_id": "agent_live", "name": "onhold"}]}
            )
        if path == "/v1/convai/agents/agent_live":
            return httpx.Response(200, json={"conversation_config": {}})
        return httpx.Response(404, json={})


def test_setup_dry_run_reads_but_writes_nothing(capsys):
    rec = Recorder()
    http = httpx.Client(base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(rec))
    assert setup.main(["agent", "--owner-name", "Test Owner"], http=http) == 0
    assert all(r.method == "GET" for r in rec.requests)
    out = capsys.readouterr().out
    assert "DRY RUN: PATCH https://api.elevenlabs.io/v1/convai/agents/agent_live" in out
    assert "AGENT_ID=agent_live" in out


def test_setup_needs_an_owner_name(monkeypatch):
    monkeypatch.delenv("OWNER_NAME", raising=False)
    http = httpx.Client(
        base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(Recorder())
    )
    assert setup.main(["agent"], http=http) == 1


def test_setup_redacts_the_key():
    s = setup.Setup(http=None, apply=False, key="sk-secret-value")
    s.say("header xi-api-key: sk-secret-value")
    assert s.out == ["header xi-api-key: [redacted]"]


def test_setup_apply_needs_a_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    assert setup.main(["agent", "--owner-name", "X", "--apply"]) == 1


@pytest.mark.parametrize("owner", ["Test Owner", "Alex Rivera"])
def test_safety_tests_name_the_configured_owner(owner):
    bodies = safety_tests.test_bodies(owner)
    text = json.dumps(bodies)
    assert "{owner_name}" not in text and "{{owner_name}}" not in text
    assert owner in text
    assert len(bodies) == len(safety_tests.load_cases()["cases"]) + len(
        safety_tests.load_cases().get("simulations", [])
    )
