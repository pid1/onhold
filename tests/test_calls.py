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
OLD_WINDOW = {"timezone": "America/Chicago", "start": "2026-11-06T09:00", "end": "2026-11-06T12:00"}


def ok_outbound(api, conv_id="conv_1"):
    api.route("POST", OUTBOUND, {"success": True, "conversation_id": conv_id, "callSid": "CA1"})


# Drafting --------------------------------------------------------------------


async def test_a_draft_shows_everything_and_dials_nothing(service, api):
    d = await service.draft_call(brief(reference="todo:1"))
    p = d["preview"]
    assert d["draft_id"].startswith("d_")
    assert not {"mode", "use", "dials_at_local"} & set(p)
    assert "on behalf of Test Owner" in p["first_message"]
    assert "a licence plate renewal" in p["first_message"]
    assert "{{" not in p["first_message"] and "{{" not in p["voicemail_message"]
    # The deployment's callback number, spoken in groups.
    assert "512-555-0142" in p["voicemail_message"]
    assert p["dynamic_variables"]["onhold_call_id"] == p["call_id"]
    assert p["reference"] == "todo:1"
    assert "owner" in d["next"] and "place_call(draft_id, confirm=true)" in d["next"]
    assert "schedule" not in d["next"]
    assert api.requests == []


async def test_drafting_outside_calling_hours_is_refused(service, clock):
    clock.now = clock.now.replace(hour=2, minute=30)  # 20:30 the evening before, in Chicago
    with pytest.raises(BriefError, match="08:00-20:00"):
        await service.draft_call(brief())


async def test_the_assistant_cannot_call_itself(service):
    with pytest.raises(BriefError, match="its own number"):
        await service.draft_call(brief(to_number="+15125550100"))


async def test_a_brief_with_a_call_window_is_refused(service, api):
    with pytest.raises(BriefError, match="scheduling was removed") as e:
        await service.draft_call(brief(call_window=OLD_WINDOW))
    assert [i["field"] for i in e.value.issues] == ["brief.call_window"]
    assert api.requests == []


async def test_a_brief_without_callee_timezone_is_refused(service):
    data = brief()
    del data["callee_timezone"]
    with pytest.raises(BriefError, match="callee_timezone"):
        await service.draft_call(data)


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


async def test_placing_outside_calling_hours_is_refused_and_frees_the_draft(service, api, clock):
    clock.now = clock.now.replace(day=6, hour=1, minute=50)  # 19:50 in Chicago
    d = await service.draft_call(brief())
    ok_outbound(api)
    clock.advance(minutes=15)  # 20:05: a one-hour call could run past 21:00
    with pytest.raises(BriefError, match="08:00-20:00") as e:
        await service.place_call(d["draft_id"], True)
    assert e.value.issues[0]["field"] == "brief.callee_timezone"
    assert api.dials() == 0
    clock.advance(minutes=-10)  # 19:55, and the draft is still unused
    assert (await service.place_call(d["draft_id"], True))["status"] == "dialing"


async def test_a_draft_saved_with_a_call_window_cannot_be_placed(service, api, store, clock):
    """A draft from before scheduling was removed: refused, nothing dialled."""
    now = int(clock.now.timestamp())
    data = brief(call_window=OLD_WINDOW)
    del data["callee_timezone"]
    await store.insert_draft(
        {
            "id": "d_0123456789abcdef",
            "created_at": now,
            "expires_at": now + 1800,
            "brief_json": data,
            "preview_json": {"call_id": "c_0123456789abcdef", "dynamic_variables": {}},
        }
    )
    ok_outbound(api)
    with pytest.raises(BriefError, match="scheduling was removed"):
        await service.place_call("d_0123456789abcdef", True)
    assert api.dials() == 0
    assert await store.get_call("c_0123456789abcdef") is None


def old_row(call_id: str, status: str, created_at: int) -> dict:
    """A calls row as schedule_call or cancel_call wrote it before scheduling was removed."""
    data = brief(call_window=OLD_WINDOW, reference="todo:old")
    del data["callee_timezone"]
    return {
        "id": call_id,
        "draft_id": "d_" + call_id[2:],
        "created_at": created_at,
        "updated_at": created_at,
        "mode": "scheduled",
        "callee_name": data["callee_name"],
        "to_number": data["to_number"],
        "topic": data["topic"],
        "reference": "todo:old",
        "brief_json": data,
        "status": status,
        "batch_id": "batch_1",
        "scheduled_for": created_at + 86400,
    }


async def test_old_scheduled_and_cancelled_rows_still_read(service, api, store, clock):
    created = int(clock.now.timestamp()) - 3 * 86400
    await store.insert_call(old_row("c_000000000000000a", "scheduled", created))
    await store.insert_call(old_row("c_000000000000000b", "cancelled", created + 60))
    clock.advance(minutes=5)
    for call_id, status in (
        ("c_000000000000000a", "scheduled"),
        ("c_000000000000000b", "cancelled"),
    ):
        v = await service.get_call(call_id)
        assert v["status"] == status and v["mode"] == "scheduled"
        assert v["reference"] == "todo:old"
        assert "retry_after_seconds" not in v  # not a running call
    listed = await service.list_calls(reference="todo:old")
    assert [c["status"] for c in listed["calls"]] == ["cancelled", "scheduled"]
    assert [c["call_id"] for c in (await service.list_calls(status="scheduled"))["calls"]] == [
        "c_000000000000000a"
    ]
    # The cron leaves them alone and nothing asks ElevenLabs about a batch.
    assert await service.reconcile() == {"checked": 0, "changed": 0}
    assert api.requests == []
    # And they can be deleted like any finished call.
    assert (await service.delete_call("c_000000000000000a", True))["deleted"]


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


def hung_up_on_the_opening(call_id: str, ended: str = "Call ended by remote party") -> dict:
    """A real failure: a menu, hold, a rep, the opening line, and they hang up."""
    conv = conversation(
        call_id,
        metadata={"termination_reason": ended},
        transcript=[
            {
                "role": "user",
                "message": "Thank you for calling the tax office.",
                "time_in_call_secs": 1,
            },
            {"role": "user", "message": "For registration, press 2.", "time_in_call_secs": 6},
            {"role": "agent", "message": "", "time_in_call_secs": 9},
            {
                "role": "user",
                "message": "Please hold for the next representative.",
                "time_in_call_secs": 12,
            },
            {"role": "user", "message": "Tax office, this is Sam.", "time_in_call_secs": 226},
            {
                "role": "agent",
                "message": "Hi, I'm an AI assistant calling on behalf of Test Owner. "
                "I'm calling about a licence plate renewal.",
                "time_in_call_secs": 229,
            },
        ],
    )
    results = conv["analysis"]["data_collection_results"]
    results["answer_1"] = {"value": ""}
    results["answer_2"] = {"value": ""}
    results["follow_ups"] = {"value": ""}
    results["owner_facts_shared"] = {"value": "Test Owner"}
    conv["analysis"]["evaluation_criteria_results"]["goal_achieved"] = {"result": "success"}
    return conv


async def test_a_hang_up_after_the_opening_line_is_not_reached(service, api, store):
    call_id = await placed(service, api)
    conv = hung_up_on_the_opening(call_id)
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert (call["outcome"], call["reason"]) == ("not_reached", "hung_up")
    checks = call["details_json"]["checks"]
    assert "they hung up before the agent said more than its opening line" in checks[0]
    assert call["details_json"]["ended_because"] == "Call ended by remote party"
    assert call["evaluation_json"]["goal_achieved"] == "unknown"
    assert "goal_achieved evaluation said success; corrected to unknown" in checks[-1]
    assert not any(a["answered"] for a in call["answers_json"])
    [push] = api.sent("POST", "/1/messages.json")
    assert push["message"] == ("Example County Tax Office: not reached (hung up). 0 of 2 answered.")


@pytest.mark.parametrize("reported", ["partial", "needs_owner"])
async def test_a_hang_up_after_the_opening_line_overrides_any_reached_outcome(
    service, api, store, reported
):
    call_id = await placed(service, api)
    conv = hung_up_on_the_opening(call_id)
    conv["analysis"]["data_collection_results"]["outcome"] = {"value": reported}
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert (call["outcome"], call["reason"]) == ("not_reached", "hung_up")
    # Only a goal_met that was corrected changes the evaluation.
    assert call["evaluation_json"]["goal_achieved"] == "success"


async def test_a_hang_up_after_a_conversation_is_not_overridden(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(call_id, metadata={"termination_reason": "Call ended by remote party"})
    conv["transcript"].append(
        {"role": "agent", "message": "Can it be renewed by mail?", "time_in_call_secs": 9}
    )
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["outcome"] == "goal_met" and "checks" not in call["details_json"]


async def test_the_agent_hanging_up_after_its_opening_line_is_not_a_hang_up(service, api, store):
    call_id = await placed(service, api)
    conv = hung_up_on_the_opening(call_id, ended="end_call tool was called")
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    # Still not goal_met: nothing was answered.
    assert (call["outcome"], call["reason"]) == ("partial", None)
    assert "none of the 2 questions was answered" in call["details_json"]["checks"][0]
    assert call["evaluation_json"]["goal_achieved"] == "failure"


async def test_goal_met_with_no_answers_is_partial(service, api, store):
    call_id = await placed(service, api)
    conv = conversation(call_id)
    results = conv["analysis"]["data_collection_results"]
    results["answer_1"] = {"value": "not answered"}
    results["answer_2"] = {"value": "unknown"}
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["outcome"] == "partial"
    assert call["details_json"]["checks"] == [
        "Reported goal_met, but none of the 2 questions was answered: marked partial."
    ]


async def test_goal_met_with_no_questions_needs_no_answers(service, api, store):
    call_id = await placed(service, api, questions=[])
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    call = await store.get_call(call_id)
    assert call["outcome"] == "goal_met" and "checks" not in call["details_json"]


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
    conv = conversation(call_id, metadata={"call_duration_secs": 3600})
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


async def test_keypad_entries_are_purged_with_the_transcript(service, api, store, clock):
    entry = {"label": "Account", "value": "12345678901234567"}
    call_id = await placed(service, api, keypad_entries=[entry])
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    assert (await store.get_call(call_id))["brief_json"]["keypad_entries"] == [entry]
    clock.advance(days=31)
    await service.purge()
    b = (await store.get_call(call_id))["brief_json"]
    assert "keypad_entries" not in b and b["topic"] == "a licence plate renewal"


async def test_delete_needs_confirm_and_a_finished_call(service, api, store):
    call_id = await placed(service, api)
    with pytest.raises(CallError, match="wait for the call to end"):
        await service.delete_call(call_id, True)
    await service.ingest_webhook({"type": "post_call_transcription", "data": conversation(call_id)})
    with pytest.raises(CallError, match="confirm must be true"):
        await service.delete_call(call_id, False)
    assert (await service.delete_call(call_id, True))["deleted"]
    assert await store.get_call(call_id) is None


async def test_get_call_reports_the_evaluation_and_the_old_criterion_id(service, api):
    call_id = await placed(service, api)
    conv = conversation(call_id)
    conv["analysis"]["data_collection_results"]["shared_items"] = {"value": "date of birth"}
    conv["analysis"]["evaluation_criteria_results"] = {
        "disclosed_ai": {"result": "success"},
        "shared_only_permitted_info": {"result": "failure"},
    }
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    v = await service.get_call(call_id)
    assert v["evaluation"]["no_sensitive_info_shared"] == "failure"
    assert "no_sensitive_info_shared" in v["deprecated_aliases"]
    assert v["attention"].count("Safety check failed") == 1
    assert "no_sensitive_info_shared" not in v["attention"]
    # This brief has no call_class: it is judged as customer_support.
    assert v["evaluation_details"] == {
        "call_class": "customer_support",
        "class_may_share": [
            "name",
            "callback number",
            "address",
            "customer account number",
            "order number",
        ],
        "shared_items": ["date of birth"],
        "shared_other": False,
    }


async def test_the_stored_evaluation_follows_the_brief_rules(service, api, store):
    call_id = await placed(
        service,
        api,
        callee_name="Example Family Pharmacy",
        call_class="healthcare",
        topic="a prescription refill",
        goal="Find out whether the Zentrovil refill is ready.",
        questions=["Is the refill ready?"],
        may_share=["Date of birth: March 3, 1980"],
    )
    conv = conversation(call_id)
    results = conv["analysis"]["data_collection_results"]
    del results["answer_1"], results["answer_2"]
    results["qa"] = {"value": "Q: Is Test Owner's refill ready yet? || A: Yes, it is."}
    results["shared_items"] = {"value": "name\ndate of birth"}
    conv["analysis"]["evaluation_criteria_results"] = {
        "shared_only_permitted_info": {"result": "failure"}
    }
    await service.ingest_webhook({"type": "post_call_transcription", "data": conv})
    call = await store.get_call(call_id)
    assert call["answers_json"] == [
        {"question": "Is the refill ready?", "answered": True, "answer": "Yes, it is."}
    ]
    assert call["evaluation_json"]["shared_only_permitted_info"] == "success"
    assert "Marked success." in call["details_json"]["checks"][-1]
