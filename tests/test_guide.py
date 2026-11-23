"""The brief guide: examples that validate, the tool, the resource and the instructions."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from mcp_helpers import call_tool, mcp_client, rpc

from onhold import classes, guide
from onhold.brief import parse_brief
from onhold.mcp_server import INSTRUCTIONS

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)  # a Friday, 09:00 in Chicago


@pytest.fixture
def client(service, monkeypatch):
    return mcp_client(service, monkeypatch)


@pytest.mark.parametrize("call_class", classes.CALL_CLASSES)
def test_every_example_brief_validates(call_class):
    example = guide.examples(NOW)[call_class]
    assert example["call_class"] == call_class
    parse_brief(example, now=NOW)


def test_example_briefs_validate_apart_from_call_class():
    """Runs before cc-brief merges: everything but call_class passes today's checks."""
    for example in guide.examples(NOW).values():
        parse_brief({k: v for k, v in example.items() if k != "call_class"}, now=NOW)


def test_example_window_is_the_next_weekday_morning():
    window = guide.examples(NOW)["government"]["call_window"]
    assert window["start"] == "2026-11-23T09:00"  # Monday


def test_the_guide_is_compact_and_covers_each_class():
    g = guide.guide(now=NOW)
    assert set(g["examples"]) == set(classes.CALL_CLASSES)
    assert len(json.dumps(g)) < 3400  # about 800 tokens


def test_example_numbers_are_fictional():
    for example in guide.examples(NOW).values():
        for key in ("to_number", "callback_number"):
            if key in example:
                assert example[key].startswith("+151255501")


@pytest.mark.anyio
async def test_get_brief_guide_tool(client):
    full = await call_tool(client, "get_brief_guide", {})
    assert set(full["examples"]) == set(classes.CALL_CLASSES)
    assert full["checklist"] and full["tips"]
    one = await call_tool(client, "get_brief_guide", {"call_class": "government"})
    assert one["call_class"] == "government"
    assert one["example"]["callee_name"] == "Example County Tax Office"
    assert "license plate" in one["allows"]
    bad = await call_tool(client, "get_brief_guide", {"call_class": "medical"})
    assert bad["error"]["code"] == "invalid_arguments"
    assert bad["error"]["issues"][0]["field"] == "call_class"


@pytest.mark.anyio
async def test_the_guide_is_a_readable_resource(client):
    listed = (await rpc(client, "resources/list"))["result"]["resources"]
    assert "onhold://guide" in [r["uri"] for r in listed]
    read = (await rpc(client, "resources/read", {"uri": "onhold://guide"}))["result"]
    body = json.loads(read["contents"][0]["text"])
    assert set(body["examples"]) == set(classes.CALL_CLASSES)


def test_instructions_point_to_the_guide_and_name_every_class():
    assert "get_brief_guide" in INSTRUCTIONS
    for c in classes.CALL_CLASSES:
        assert classes.describe(c) in INSTRUCTIONS
    for item in classes.ALWAYS_FORBIDDEN:
        assert item in INSTRUCTIONS
    assert "keypad_entries" in INSTRUCTIONS and "get_menu_paths" in INSTRUCTIONS
    assert len(INSTRUCTIONS.split()) < 230
