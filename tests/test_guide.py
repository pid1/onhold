"""The brief guide: examples that validate, the tool, the resource and the instructions."""

from __future__ import annotations

import json

import pytest
from mcp_helpers import call_tool, mcp_client, rpc

from onhold import classes, guide
from onhold.brief import parse_brief
from onhold.mcp_server import INSTRUCTIONS


@pytest.fixture
def client(service, monkeypatch):
    return mcp_client(service, monkeypatch)


@pytest.mark.parametrize("call_class", classes.CALL_CLASSES)
def test_every_example_brief_validates(call_class):
    example = guide.examples()[call_class]
    assert example["call_class"] == call_class
    parse_brief(example)


def test_example_briefs_validate_apart_from_call_class():
    """Runs before cc-brief merges: everything but call_class passes today's checks."""
    for example in guide.examples().values():
        parse_brief({k: v for k, v in example.items() if k != "call_class"})


def test_examples_and_checklist_never_mention_scheduling():
    for example in guide.examples().values():
        assert example["callee_timezone"] == guide.EXAMPLE_TZ
        assert "call_window" not in example
    text = json.dumps(guide.guide()).lower()
    assert "call_window" not in text and "schedul" not in text
    assert "callee_timezone" in " ".join(guide.checklist())


def test_the_guide_is_compact_and_covers_each_class():
    g = guide.guide()
    assert set(g["examples"]) == set(classes.CALL_CLASSES)
    assert len(json.dumps(g)) < 3400  # about 800 tokens


def test_example_numbers_are_fictional():
    for example in guide.examples().values():
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
    assert "schedule_call" not in INSTRUCTIONS and "call_window" not in INSTRUCTIONS
    assert "callee_timezone" in INSTRUCTIONS
    assert len(INSTRUCTIONS.split()) < 230
