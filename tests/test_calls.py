"""The call lifecycle: draft, the owner's go-ahead, dialing, and the report."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import brief, conversation

from onhold.brief import BriefError
from onhold.calls import UNTRUSTED_KEY, CallError
from onhold.config import Settings

pytestmark = pytest.mark.anyio

OUTBOUND = "/v1/convai/twilio/outbound-call"
BATCH = "/v1/convai/batch-calling/submit"


def ok_outbound(api, conv_id="conv_1"):
    api.route("POST", OUTBOUND, {"success": True, "conversation_id": conv_id, "callSid": "CA1"})


def window(**over):
    w = {"timezone": "America/Chicago", "start": "2026-11-06T09:00", "end": "2026-11-06T12:00"}
    w.update(over)
    return w


# Drafting --------------------------------------------------------------------


async def test_a_draft_shows_everything_and_dials_nothing(service, api):
    d = await service.draft_call(brief(reference="todo:1"))
    p = d["preview"]
    assert d["draft_id"].startswith("d_")
    assert p["mode"] == "now" and p["use"] == "place_call"
    assert "on behalf of Test Owner" in p["first_message"]
    assert "a licence plate renewal" in p["first_message"]
    assert "{{" not in p["first_message"] and "{{" not in p["voicemail_message"]
    # The deployment's callback number, spoken in groups.
    assert "512-555-0142" in p["voicemail_message"]
    assert p["dynamic_variables"]["onhold_call_id"] == p["call_id"]
    assert p["reference"] == "todo:1"
    assert "owner" in d["next"] and "confirm=true" in d["next"]
    assert api.requests == []


async def test_drafting_outside_calling_hours_is_refused(service, clock):
    clock.now = clock.now.replace(hour=3)  # 21:00 the evening before, in Chicago
    with pytest.raises(BriefError, match="08:00-20:45"):
        await service.draft_call(brief())


async def test_the_assistant_cannot_call_itself(service):
    with pytest.raises(BriefError, match="its own number"):
        await service.draft_call(brief(to_number="+15125550100"))


async def test_a_scheduled_draft_says_when(service):
    d = await service.draft_call(brief(callee_timezone=None, call_window=window()))
    assert d["preview"]["mode"] == "scheduled"
    assert d["preview"]["dials_at_local"] == "2026-11-06T09:00-06:00"


# The go-ahead ----------------------------------------------------------------


@pytest.mark.parametrize("confirm", [False, "yes", 1])
async def test_placing_needs_confirm_true(service, api, confirm):
    d = await service.draft_call(brief())
    ok_outbound(api)
    with pytest.raises(CallError, match="confirm must be true"):
        await service.place_call(d["draft_id"], confirm)
    assert api.dials() == 0


async def test_a_draft_places_one_call(service, api):
    d = await service.draft_call(brief())
    ok_outbound(api)
    r = await service.place_call(d["draft_id"], True)
    assert r["status"] == "dialing" and r["call_id"] == d["preview"]["call_id"]
    [sent] = api.sent("POST", OUTBOUND)
    assert sent["to_number"] == "+15125550123"
    assert sent["call_recording_enabled"] is False
    variables = sent["conversation_initiation_client_data"]["dynamic_variables"]
    assert variables == d["preview"]["dynamic_variables"]
    with pytest.raises(CallError, match="already used"):
        await service.place_call(d["draft_id"], True)
    assert api.dials() == 1


async def test_an_expired_draft_is_refused(service, api, clock):
    d = await service.draft_call(brief())
    ok_outbound(api)
    clock.advance(minutes=31)
    with pytest.raises(CallError, match="expired"):
        await service.place_call(d["draft_id"], True)
    assert api.dials() == 0


async def test_paused_refuses_every_call(api, clock, store):
    from conftest import API_KEY, ENV

    from onhold.calls import Service
    from onhold.elevenlabs import ElevenLabs

    paused = Service(
        store,
        ElevenLabs(API_KEY, transport=api.transport),
        Settings.load({**ENV, "PAUSED": "true"}),
        now=clock,
    )
    d = await paused.draft_call(brief())
    ok_outbound(api)
    with pytest.raises(CallError, match="paused"):
        await paused.place_call(d["draft_id"], True)
    assert api.dials() == 0


async def test_a_refused_call_frees_the_draft(service, api, store):
    d = await service.draft_call(brief())
    api.route("POST", OUTBOUND, {"detail": "quota"}, status=429)
    with pytest.raises(CallError, match="nothing was dialled"):
        await service.place_call(d["draft_id"], True)
    assert await store.get_call(d["preview"]["call_id"]) is None
    ok_outbound(api)
    assert (await service.place_call(d["draft_id"], True))["status"] == "dialing"


async def test_a_windowed_draft_must_be_scheduled(service, api):
    d = await service.draft_call(brief(callee_timezone=None, call_window=window()))
    ok_outbound(api)
    with pytest.raises(CallError, match="schedule_call"):
        await service.place_call(d["draft_id"], True)
    # The refusal did not use the draft up.
    api.route("POST", BATCH, {"id": "batch_1", "status": "pending"})
    r = await service.schedule_call(d["draft_id"], True)
    assert r["status"] == "scheduled"
    [sent] = api.sent("POST", BATCH)
    assert sent["scheduled_time_unix"] == int(
        (service._now().replace(day=6, hour=15, minute=0)).timestamp()
    )


async def test_cancel_a_scheduled_call(service, api):
    d = await service.draft_call(brief(callee_timezone=None, call_window=window()))
    api.route("POST", BATCH, {"id": "batch_1", "status": "pending"})
    r = await service.schedule_call(d["draft_id"], True)
    api.route("POST", "/v1/convai/batch-calling/batch_1/cancel", {"status": "cancelled"})
    assert (await service.cancel_call(r["call_id"]))["status"] == "cancelled"
    with pytest.raises(CallError, match="only a scheduled call"):
        await service.cancel_call(r["call_id"])


# Reports ---------------------------------------------------------------------


async def placed(service, api, conv_id="conv_1", **b):
    d = await service.draft_call(brief(**b))
    ok_outbound(api, conv_id)
    return (await service.place_call(d["draft_id"], True))["call_id"]


async def test_the_post_call_webhook_writes_the_report(service, api, store, clock):
    call_id = await placed(service, api, reference="todo:7")
    clock.advance(minutes=5)
    r = await service.ingest_webhook(
        {"type": "post_call_transcription", "data": conversation(call_id)}
    )
    assert r == f"recorded {call_id}"
    call = await store.get_call(call_id)
    assert call["status"] == "ended" and call["outcome"] == "goal_met" and call["reason"] is None
    assert call["answers_json"] == [
        {"question": "Can it be renewed by mail?", "answered": True, "answer": "Yes, by mail."},
        {"question": "What is the fee?", "answered": True, "answer": "$51.75"},
    ]
    assert call["follow_ups_json"] == ["Mail the form", "Include a copy of the license"]
    assert call["duration_secs"] == 240 and call["cost"] == 1200
    assert call["transcript_purge_at"] == int((clock.now + timedelta(days=30)).timestamp())
    # One Pushover message, with the headline.
    [push] = api.sent("POST", "/1/messages.json")
    assert push["message"] == (
        "Example County Tax Office: goal met. 2 of 2 answered. 2 follow-ups."
    )
    assert call["notified_at"]
    # A repeat delivery changes nothing and sends nothing.
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    assert len(api.sent("POST", "/1/messages.json")) == 1


async def test_get_call_marks_what_the_callee_said_as_untrusted(service, api):
    call_id = await placed(service, api)
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    v = await service.get_call(call_id, include_transcript=True)
    assert v["outcome"] == "goal_met" and v["answered"] == "2 of 2"
    assert "summary" not in v and "answers" not in v
    untrusted = v[UNTRUSTED_KEY]
    assert untrusted["summary"].startswith("Renewal can be done by mail")
    assert untrusted["transcript"][1] == {
        "role": "them",
        "at_secs": 4,
        "message": "Tax office, how can I help?",
    }


async def test_nobody_speaking_means_not_reached(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(
        call_id,
        transcript=[
            {"role": "agent", "message": "Hi, I'm an AI assistant", "time_in_call_secs": 0}
        ],
    )
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["outcome"] == "not_reached" and call["reason"] == "no_answer"
    assert "nobody on the other end spoke" in call["details_json"]["checks"][0]


async def test_a_very_short_call_cannot_have_met_its_goal(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(call_id, metadata={"call_duration_secs": 12})
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["outcome"] == "partial"


async def test_unanswered_questions_are_marked(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(call_id)
    results = conv["analysis"]["data_collection_results"]
    results["outcome"] = {"value": "partial"}
    results["answer_2"] = {"value": "not answered"}
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["outcome"] == "partial"
    assert call["answers_json"][1] == {
        "question": "What is the fee?",
        "answered": False,
        "answer": None,
    }


async def test_hold_timeout_at_the_duration_cap(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(call_id, metadata={"call_duration_secs": 900})
    conv["analysis"]["data_collection_results"]["outcome"] = {"value": "not_reached"}
    conv["analysis"]["data_collection_results"]["not_reached_reason"] = {"value": "none"}
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert (call["outcome"], call["reason"]) == ("not_reached", "hold_timeout")


async def test_a_call_that_never_connected(service, api, store):
    call_id = await placed(service, api)
    await service.ingest_webhook(
        {
            "type": "call_initiation_failure",
            "data": {
                "agent_id": "agent_test",
                "conversation_id": "conv_1",
                "failure_reason": "busy",
            },
        }
    )
    call = await store.get_call(call_id)
    assert (call["status"], call["outcome"], call["reason"]) == ("ended", "not_reached", "busy")
    [push] = api.sent("POST", "/1/messages.json")
    assert push["message"] == "Example County Tax Office: not reached (busy)."


async def test_webhooks_for_other_agents_or_calls_are_ignored(service, api):
    call_id = await placed(service, api)
    other = conversation(call_id, agent_id="agent_someone_else")
    assert (
        await service.ingest_webhook({"type": "post_call_transcription", "data": other})
    ).startswith("ignored")
    stranger = conversation("c_ffffffffffffffff", conv_id="conv_unknown")
    assert await service.ingest_webhook({"type": "post_call_transcription", "data": stranger}) == (
        "ignored: not an onhold call"
    )


# When the webhook never comes ------------------------------------------------


async def test_reconcile_fetches_a_call_the_webhook_missed(service, api, store, clock):
    call_id = await placed(service, api)
    api.route("GET", "/v1/convai/conversations/conv_1", conversation(call_id))
    clock.advance(minutes=10)
    assert await service.reconcile() == {"checked": 0, "changed": 0}
    clock.advance(minutes=15)
    assert await service.reconcile() == {"checked": 1, "changed": 1}
    assert (await store.get_call(call_id))["outcome"] == "goal_met"


async def test_reconcile_follows_a_scheduled_call_to_its_conversation(service, api, store, clock):
    d = await service.draft_call(brief(callee_timezone=None, call_window=window()))
    api.route("POST", BATCH, {"id": "batch_1", "status": "pending"})
    call_id = (await service.schedule_call(d["draft_id"], True))["call_id"]
    api.route(
        "GET",
        "/v1/convai/batch-calling/batch_1",
        {
            "status": "completed",
            "recipients": [{"status": "completed", "conversation_id": "conv_9"}],
        },
    )
    api.route("GET", "/v1/convai/conversations/conv_9", conversation(call_id, conv_id="conv_9"))
    clock.now = clock.now.replace(day=6, hour=15, minute=30)
    assert (await service.reconcile())["changed"] == 1
    call = await store.get_call(call_id)
    assert call["conversation_id"] == "conv_9" and call["outcome"] == "goal_met"


async def test_get_call_refreshes_a_running_call(service, api, clock):
    call_id = await placed(service, api)
    api.route("GET", "/v1/convai/conversations/conv_1", {"status": "in-progress"})
    clock.advance(minutes=3)
    v = await service.get_call(call_id)
    assert v["status"] == "in_progress" and v["retry_after_seconds"] == 60
    api.route("GET", "/v1/convai/conversations/conv_1", conversation(call_id))
    v = await service.get_call(call_id)
    assert v["status"] == "ended" and "retry_after_seconds" not in v


async def test_a_call_with_no_result_for_a_day_is_given_up(service, api, store, clock):
    call_id = await placed(service, api)
    api.route("GET", "/v1/convai/conversations/conv_1", {"status": "processing"})
    clock.advance(hours=25)
    await service.reconcile()
    assert (await store.get_call(call_id))["status"] == "failed"


# Listing, purging, deleting ----------------------------------------------------


async def test_list_calls_filters(service, api, clock):
    a = await placed(service, api, reference="todo:1")
    clock.advance(minutes=1)
    b = await placed(service, api, conv_id="conv_2", reference="todo:2")
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(a)})
    got = await service.list_calls(outcome="goal_met")
    assert [c["call_id"] for c in got["calls"]] == [a]
    got = await service.list_calls(reference="todo:2")
    assert [c["call_id"] for c in got["calls"]] == [b]
    got = await service.list_calls()
    assert [c["call_id"] for c in got["calls"]] == [b, a]
    with pytest.raises(CallError, match="outcome must be"):
        await service.list_calls(outcome="great")


async def test_transcripts_are_purged_after_30_days(service, api, store, clock):
    call_id = await placed(service, api)
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    clock.advance(days=29)
    assert (await service.purge())["transcripts"] == 0
    clock.advance(days=2)
    assert (await service.purge())["transcripts"] == 1
    call = await store.get_call(call_id)
    assert call["transcript_json"] is None and call["outcome"] == "goal_met"
    v = await service.get_call(call_id, include_transcript=True)
    assert "deleted after 30 days" in v[UNTRUSTED_KEY]["transcript"]


async def test_delete_needs_confirm_and_a_finished_call(service, api, store):
    call_id = await placed(service, api)
    with pytest.raises(CallError, match="cancel or wait"):
        await service.delete_call(call_id, True)
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    with pytest.raises(CallError, match="confirm must be true"):
        await service.delete_call(call_id, False)
    assert (await service.delete_call(call_id, True))["deleted"]
    assert await store.get_call(call_id) is None
