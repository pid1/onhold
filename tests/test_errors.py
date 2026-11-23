"""Every tool failure reaches the caller as {code, message, issues, request_id}.

Includes the regression for the Nov 23 draft_call failures, which the caller
saw only as "Error executing tool draft_call".
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from conftest import brief
from mcp_helpers import call_tool, mcp_client

pytestmark = pytest.mark.anyio

# 2026-11-23 08:40 in America/Chicago (CST): the morning the brief was drafted.
NOV23_MORNING = datetime(2026, 11, 23, 14, 40, tzinfo=UTC)


@pytest.fixture
def client(service, monkeypatch):
    return mcp_client(service, monkeypatch)


def assert_shape(err: dict, code: str) -> None:
    assert set(err) == {"code", "message", "issues", "request_id"}, err
    assert err["code"] == code
    assert err["message"] and err["message"] != "Error executing tool draft_call"
    assert err["request_id"].startswith("r_")
    for i in err["issues"]:
        assert {"field", "problem"} <= set(i)


def fields(err: dict) -> list[str]:
    return [i["field"] for i in err["issues"]]


async def test_a_refused_brief_is_a_structured_field_level_error(client, api):
    err = (await call_tool(client, "draft_call", {"brief": brief(callee_timezone=None)}))["error"]
    assert_shape(err, "invalid_brief")
    assert any("callee_timezone" in f for f in fields(err))
    assert api.dials() == 0


async def test_a_brief_failing_the_schema_lists_every_field(client):
    bad = brief(to_number=12, questions=["x" * 300])
    bad.pop("goal")
    err = (await call_tool(client, "draft_call", {"brief": bad}))["error"]
    assert_shape(err, "invalid_brief")
    got = fields(err)
    for expected in ("goal", "to_number", "questions"):
        assert any(expected in f for f in got), got
    assert all(f.startswith("brief") for f in got), got


async def test_arguments_failing_the_sdk_schema_are_structured(client):
    err = (await call_tool(client, "place_call", {"draft_id": "nope", "confirm": True}))["error"]
    assert_shape(err, "invalid_arguments")
    assert fields(err) == ["draft_id"]
    assert "expected" in err["issues"][0]
    err = (await call_tool(client, "draft_call", {}))["error"]
    assert_shape(err, "invalid_arguments")
    assert fields(err) == ["brief"]


async def test_a_call_error_is_structured(client):
    err = await call_tool(client, "place_call", {"draft_id": "d_" + "0" * 16, "confirm": False})
    assert_shape(err["error"], "refused")


async def test_an_unexpected_exception_is_logged_and_hidden(client, service, monkeypatch, caplog):
    async def boom(brief):
        raise RuntimeError("database exploded near +15125550199")

    monkeypatch.setattr(service, "draft_call", boom)
    with caplog.at_level(logging.INFO, logger="onhold.mcp_server"):
        err = (await call_tool(client, "draft_call", {"brief": brief()}))["error"]
    assert_shape(err, "internal_error")
    assert "exploded" not in str(err) and "555" not in str(err)
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert err["request_id"] in record.getMessage()
    assert record.exc_info and "database exploded" in caplog.text
    assert "Traceback" in caplog.text


# The Nov 23 failure -------------------------------------------------------------------

NOV23_MAY_SHARE = [
    "Patient name: Jordan Example",
    "Callback number: 512-555-0142",
    "Date of birth: March 4, 1980",
    "Pharmacy: Example Pharmacy on Elm",
    "Taking Examplol 1 mg weekly",
]


def nov23_brief(**over) -> dict:
    return brief(
        callee_name="Example Family Clinic",
        to_number="+15125550131",
        topic="a prescription dose change",
        goal="Find out whether the doctor approved the dose change.",
        questions=[
            "Has the doctor approved the change?",
            "When will it be sent?",
            "Which pharmacy?",
        ],
        may_share=NOV23_MAY_SHARE,
        must_not_share=[],
        give_up_when="they need the patient on the line",
        callee_timezone="America/Chicago",
        reference="refill-escalation",
        **over,
    )


async def test_nov23_brief_drafts_as_a_healthcare_call(client, clock, api):
    """With call classes, the labelled callback number is fine to share with a clinic."""
    clock.now = NOV23_MORNING
    out = await call_tool(
        client, "draft_call", {"brief": nov23_brief() | {"call_class": "healthcare"}}
    )
    assert "error" not in out, out
    assert out["preview"]["call_class"] == "healthcare"
    assert api.dials() == 0


async def test_a_brief_with_a_call_window_is_refused_on_that_field(client, clock, api):
    """The Nov 23 brief as first sent, with a window: scheduling is gone, said by field."""
    clock.now = NOV23_MORNING
    window = {"timezone": "America/Chicago", "start": "2026-11-23T09:05", "end": "2026-11-23T12:00"}
    b = nov23_brief(call_class="healthcare", call_window=window)
    err = (await call_tool(client, "draft_call", {"brief": b}))["error"]
    assert_shape(err, "invalid_brief")
    assert fields(err) == ["brief.call_window"], err
    [i] = err["issues"]
    assert "scheduling was removed" in i["problem"] and "place_call" in i["problem"]
    assert api.dials() == 0
