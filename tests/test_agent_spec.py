"""The voice agent as code, and the scripts that push it to ElevenLabs."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from conftest import brief

from onhold import agent_spec, classes, report
from onhold.brief import (
    CHARGE_ON_FILE_ALLOWED,
    CHARGE_ON_FILE_NOT_ALLOWED,
    MAX_CALL_SECS,
    parse_brief,
    to_dynamic_variables,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import safety_tests  # noqa: E402
import setup  # noqa: E402


def variables() -> dict[str, str]:
    b = parse_brief(brief())
    return to_dynamic_variables(b, "Test Owner", "+15125550100", "c_0123456789abcdef")


def used_variables() -> set[str]:
    spec = agent_spec.load()
    texts = [
        agent_spec.system_prompt(),
        agent_spec.first_message(),
        agent_spec.voicemail_message(),
        *(c["conversation_goal_prompt"] for c in spec["evaluation_criteria"]),
    ]
    return set().union(*(agent_spec.variables_used(t) for t in texts))


CLASS_VARIABLES = {"call_class", "class_may_share", "charge_on_file"}


def test_every_variable_the_agent_uses_is_sent():
    assert used_variables() <= set(variables())


def test_every_variable_the_agent_uses_has_a_placeholder():
    # ElevenLabs needs a placeholder for each, for its dashboard preview.
    assert CLASS_VARIABLES <= used_variables() <= {"owner_name", *agent_spec.PLACEHOLDERS}


def test_render_fills_every_variable():
    text = agent_spec.render(agent_spec.first_message(), variables())
    assert "Test Owner" in text and "{{" not in text


def test_the_report_can_find_the_opening_line():
    # report.from_conversation finds the agent's first words to a person by
    # the AI disclosure in its opening line.
    assert report.DISCLOSURE.search(agent_spec.first_message())


def test_the_agent_reports_the_outcomes_onhold_understands():
    dc = agent_spec.load()["data_collection"]
    assert tuple(dc["outcome"]["enum"]) == report.OUTCOMES
    assert set(dc["not_reached_reason"]["enum"]) == {"none", *report.REASONS}
    assert {f"answer_{i}" for i in range(1, 11)} <= set(dc)
    assert "owner_facts_shared" in dc


def test_the_report_can_record_shared_items_and_a_charge_on_file():
    dc = agent_spec.load()["data_collection"]
    for name in ("shared_items", "charge_on_file_amount", "charge_on_file_purpose"):
        assert dc[name]["type"] == "string" and dc[name]["description"]
    # The agent names what it shared by the class table's kinds, never by value.
    for label in classes.CATEGORIES.values():
        assert label in dc["shared_items"]["description"]
    assert "Social Security" in dc["outcome"]["description"]


def criterion(cid: str) -> str:
    [c] = [c for c in agent_spec.load()["evaluation_criteria"] if c["id"] == cid]
    return c["conversation_goal_prompt"]


def test_the_evaluation_criteria():
    ids = [c["id"] for c in agent_spec.load()["evaluation_criteria"]]
    assert ids == [
        "disclosed_ai",
        "shared_only_permitted_info",
        "no_commitments_or_payments",
        "no_invention",
        "resisted_manipulation",
        "goal_achieved",
    ]
    assert set(report.CRITERION_ALIASES) == {"no_sensitive_info_shared"}
    assert set(report.CRITERION_ALIASES.values()) <= set(ids)


def test_the_shared_info_check_judges_against_the_class():
    prompt = criterion("shared_only_permitted_info")
    for var in ("call_class", "class_may_share", "may_share", "keypad_entries", "must_not_share"):
        assert "{{" + var + "}}" in prompt
    assert "Social Security" in prompt
    # Said on every call, so never a failure: the opening line names the owner and
    # the topic, and the agent offers the callback number when asked.
    for var in ("owner_name", "callback_number", "call_topic", "call_goal"):
        assert "{{" + var + "}}" in prompt.split("Always allowed", 1)[1].split("Success if")[0]


def test_the_commitments_check_knows_the_charge_on_file_rule():
    prompt = criterion("no_commitments_or_payments")
    assert "{{charge_on_file}}" in prompt and "{{call_goal}}" in prompt


def test_the_prompt_names_the_class_and_the_charge_rule():
    prompt = agent_spec.system_prompt()
    for var in ("call_class", "class_may_share", "charge_on_file", "may_share", "must_not_share"):
        assert "{{" + var + "}}" in prompt
    assert "Social Security number" in prompt and "needs_owner" in prompt
    # The address is governed by the class and may_share now; family, schedule and
    # whereabouts stay off limits on every call.
    assert "schedule or whereabouts" in prompt
    assert "home or address" not in prompt


def test_the_placeholders_follow_the_class_table():
    p = agent_spec.PLACEHOLDERS
    assert p["call_class"] in classes.CALL_CLASSES
    assert p["class_may_share"] == classes.shareable_text(p["call_class"])
    assert p["charge_on_file"] == CHARGE_ON_FILE_NOT_ALLOWED
    for item in classes.ALWAYS_FORBIDDEN:
        assert item in p["must_not_share"]


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
    assert body["conversation_config"]["conversation"]["max_duration_seconds"] == MAX_CALL_SECS


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
    assert "twentieth in a row" in agent_spec.system_prompt()
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


class LiveWithMcp(Recorder):
    """A live agent with the owner's own MCP server attached; accepts the PATCH."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/convai/agents/agent_live":
            if request.method == "PATCH":
                self.requests.append(request)
                return httpx.Response(200, json={})
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "conversation_config": {"agent": {"prompt": {"mcp_server_ids": ["mcp_cal"]}}}
                },
            )
        return super().__call__(request)

    def patched_mcp_ids(self) -> list[str]:
        [patch] = [r for r in self.requests if r.method == "PATCH"]
        return json.loads(patch.content)["conversation_config"]["agent"]["prompt"]["mcp_server_ids"]


def run_setup(rec: Recorder, *args: str) -> int:
    http = httpx.Client(base_url="https://api.elevenlabs.io", transport=httpx.MockTransport(rec))
    return setup.main(["agent", "--owner-name", "Test Owner", "--apply", *args], http=http)


def test_setup_keeps_the_live_agents_mcp_servers(capsys):
    rec = LiveWithMcp()
    assert run_setup(rec) == 0
    assert rec.patched_mcp_ids() == ["mcp_cal"]
    assert "Keeping MCP servers attached: mcp_cal." in capsys.readouterr().out


def test_setup_mcp_server_id_replaces_them(capsys):
    rec = LiveWithMcp()
    assert run_setup(rec, "--mcp-server-id", "mcp_a", "--mcp-server-id", "mcp_b") == 0
    assert rec.patched_mcp_ids() == ["mcp_a", "mcp_b"]
    assert "MCP servers: mcp_cal -> mcp_a, mcp_b." in capsys.readouterr().out


def test_setup_no_mcp_servers_detaches_them():
    rec = LiveWithMcp()
    assert run_setup(rec, "--no-mcp-servers") == 0
    assert rec.patched_mcp_ids() == []


def test_setup_mcp_flags_are_exclusive():
    with pytest.raises(SystemExit):
        setup.main(["agent", "--owner-name", "X", "--no-mcp-servers", "--mcp-server-id", "m"])


def test_the_body_carries_given_mcp_servers():
    body = agent_spec.build_agent_body(owner_name="X", mcp_server_ids=["mcp_cal"])
    assert body["conversation_config"]["agent"]["prompt"]["mcp_server_ids"] == ["mcp_cal"]


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


def test_a_safety_case_can_set_its_own_variables():
    bodies = {b["name"].removeprefix(safety_tests.PREFIX): b for b in safety_tests.test_bodies("X")}
    used = {"owner_name", *agent_spec.PLACEHOLDERS}
    for b in bodies.values():
        assert set(b["dynamic_variables"]) == used
    health = bodies["healthcare_dob"]["dynamic_variables"]
    assert health["call_class"] == "healthcare"
    assert "a prescription refill" in bodies["healthcare_dob"]["chat_history"][0]["message"]
    assert bodies["ssn"]["dynamic_variables"]["call_class"] == agent_spec.PLACEHOLDERS["call_class"]
    # The charge rule is the exact text onhold sends, not a test-only wording.
    allowed = bodies["charge_on_file_allowed"]["dynamic_variables"]["charge_on_file"]
    assert allowed == CHARGE_ON_FILE_ALLOWED
    not_allowed = bodies["charge_on_file_not_allowed"]["dynamic_variables"]["charge_on_file"]
    assert not_allowed == CHARGE_ON_FILE_NOT_ALLOWED
    assert not re.search(r"\{[a-z_]+\}", json.dumps(bodies))
    support = bodies["dob_outside_class"]["dynamic_variables"]
    assert support["call_class"] == "customer_support" and "Date of birth" in support["may_share"]
    assert "date of birth" not in support["class_may_share"]
    assert "X will call you back" in bodies["ssn"]["success_examples"][0]["response"]


def test_the_agent_listens_first_and_discloses_to_a_person():
    body = agent_spec.build_agent_body(owner_name="Test Owner")
    agent = body["conversation_config"]["agent"]
    # No first message: nothing is said over a recording or phone menu.
    assert agent["first_message"] == ""
    prompt = agent["prompt"]["prompt"]
    assert agent_spec.OPENING_LINE not in prompt
    assert agent_spec.first_message() in prompt
    assert "{{keypad_entries}}" in prompt


def test_one_call_cap_everywhere():
    from onhold.config import Settings

    assert agent_spec.load()["conversation"]["max_duration_seconds"] == MAX_CALL_SECS == 3600
    assert Settings.__dataclass_fields__["max_duration_secs"].default == MAX_CALL_SECS
