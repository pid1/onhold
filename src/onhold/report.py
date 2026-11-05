"""Turn what ElevenLabs reports about a call into onhold's report row.

Two inputs have the same shape: the ``data`` of a ``post_call_transcription``
webhook and ``GET /v1/convai/conversations/{id}``. A third,
``call_initiation_failure``, covers calls that never connected.

The voice agent picks an ``outcome`` at the end of every call (its data
collection, configured in ``agent/agent.json``). That pick is checked here
against what the phone network and the clock say, because the agent is the
one party that cannot see whether it was talking to a person:

* no turn from the other side means nobody was reached, whatever was picked;
* a call under ``MIN_GOAL_SECS`` cannot have met its goal;
* a call cut off at the duration cap while waiting is a hold timeout.

Every adjustment is recorded in ``details.checks`` so the report says why.
"""

from __future__ import annotations

import json
import re
from typing import Any

OUTCOMES = ("goal_met", "partial", "needs_owner", "not_reached")
REASONS = (
    "no_answer",
    "busy",
    "voicemail",
    "phone_tree_dead_end",
    "hold_timeout",
    "wrong_number",
    "hung_up",
    "call_failed",
)
MAX_QUESTIONS = 10
MIN_GOAL_SECS = 20
NOT_ANSWERED = {"", "not answered", "none", "n/a", "unknown"}
DETAIL_FIELDS = (
    "callback_requested",
    "callback_details",
    "appointment_details",
    "reference_number",
    "do_not_call_requested",
    "info_requested_not_available",
)
KEYPAD_TOOL = "play_keypad_touch_tone"
# One menu choice: 1-4 of 0-9 * #, after dropping ElevenLabs' w/W pauses. Anything
# longer could be an extension, an ID or an account number, and is never kept.
PRESS = re.compile(r"^[0-9*#]{1,4}$")
MAX_MENU_STEPS = 10
MAX_OPTION_CHARS = 80
# What an option note may not carry: any run of 5 or more digits.
OPTION_DIGITS = re.compile(r"(?:\d[ -]?){5,}")
# Twilio's failure reasons as ElevenLabs relays them.
FAILURE_REASONS = {"busy": "busy", "no-answer": "no_answer", "no_answer": "no_answer"}


def _lines(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [line.strip(" -•\t") for line in str(value).splitlines() if line.strip(" -•\t")]


def _collected(analysis: dict) -> dict[str, Any]:
    results = analysis.get("data_collection_results") or {}
    return {k: (v or {}).get("value") for k, v in results.items()}


def _transcript(turns: list[dict]) -> list[dict]:
    out = []
    for t in turns or []:
        msg = (t.get("message") or "").strip()
        if msg:
            out.append(
                {
                    "role": "agent" if t.get("role") == "agent" else "them",
                    "at_secs": t.get("time_in_call_secs", 0),
                    "message": msg,
                }
            )
    return out


def from_conversation(conv: dict, questions: list[str], max_duration_secs: int) -> dict:
    """The report fields for a finished conversation (webhook data or GET)."""
    meta = conv.get("metadata") or {}
    analysis = conv.get("analysis") or {}
    data = _collected(analysis)
    transcript = _transcript(conv.get("transcript") or [])
    duration = meta.get("call_duration_secs") or 0
    started = meta.get("start_time_unix_secs")
    termination = str(meta.get("termination_reason") or "")
    checks: list[str] = []

    outcome = data.get("outcome") if data.get("outcome") in OUTCOMES else None
    reason = data.get("not_reached_reason") if data.get("not_reached_reason") in REASONS else None
    heard_them = any(t["role"] == "them" for t in transcript)

    if outcome is None:
        outcome = "partial" if heard_them else "not_reached"
        checks.append("The agent did not report an outcome; it was worked out from the call.")
    if not heard_them and outcome != "not_reached":
        checks.append(f"Reported {outcome}, but nobody on the other end spoke: not reached.")
        outcome = "not_reached"
    if outcome == "goal_met" and duration < MIN_GOAL_SECS:
        checks.append(f"Reported goal_met, but the call lasted {duration}s: marked partial.")
        outcome = "partial"
    if "voicemail" in termination.lower() and outcome == "not_reached":
        reason = "voicemail"
    if outcome == "not_reached" and reason is None:
        if duration >= max_duration_secs - 5:
            reason = "hold_timeout"
        elif heard_them:
            reason = "hung_up"
        else:
            reason = "no_answer"
    if outcome != "not_reached":
        reason = None

    answers = []
    for i, q in enumerate(questions[:MAX_QUESTIONS], 1):
        value = data.get(f"answer_{i}")
        text = str(value).strip() if value is not None else ""
        answered = text.lower() not in NOT_ANSWERED
        answers.append({"question": q, "answered": answered, "answer": text if answered else None})

    details = {k: data.get(k) for k in DETAIL_FIELDS if data.get(k) not in (None, "")}
    details["ended_because"] = termination or None
    if checks:
        details["checks"] = checks

    evaluation = {
        k: (v or {}).get("result")
        for k, v in (analysis.get("evaluation_criteria_results") or {}).items()
    }
    return {
        "status": "ended",
        "outcome": outcome,
        "reason": reason,
        "summary": analysis.get("transcript_summary") or None,
        "answers_json": answers,
        "follow_ups_json": _lines(data.get("follow_ups")),
        "details_json": details,
        "shared_json": _lines(data.get("owner_facts_shared")),
        "evaluation_json": evaluation,
        "conversation_id": conv.get("conversation_id"),
        "started_at": started,
        "ended_at": (started + duration) if started else None,
        "duration_secs": duration,
        "cost": meta.get("cost"),
        "transcript_json": transcript,
    }


def _option(reason: Any) -> str:
    text = " ".join(str(reason or "").split())[:MAX_OPTION_CHARS].strip()
    return "" if OPTION_DIGITS.search(text) else text


def menu_presses(conv: dict) -> list[dict] | None:
    """The keypad presses the agent made, in order: ``[{"press", "option"}]``.

    Read from the transcript's tool calls, the record of what was actually
    pressed, not from anything the agent or the callee said. ``option`` is the
    agent's own reason for the press. A press that failed is left out. Returns
    None when the presses cannot be a menu path (too many, or one too long to be
    a menu choice), so nothing is learned from that call.
    """
    turns = conv.get("transcript") or []
    failed = {
        r.get("request_id")
        for t in turns
        for r in t.get("tool_results") or []
        if r.get("tool_name") == KEYPAD_TOOL and r.get("is_error")
    }
    steps = []
    for t in turns:
        for c in t.get("tool_calls") or []:
            if c.get("tool_name") != KEYPAD_TOOL or c.get("tool_has_been_called") is False:
                continue
            if c.get("request_id") in failed:
                continue
            params = c.get("params_as_json")
            try:
                params = json.loads(params) if isinstance(params, str) else (params or {})
            except ValueError:
                return None
            tones = re.sub(r"[wW]", "", str(params.get("dtmf_tones") or ""))
            if not PRESS.match(tones):
                return None
            steps.append({"press": tones, "option": _option(params.get("reason"))})
    if len(steps) > MAX_MENU_STEPS:
        return None
    return steps


def from_failure(data: dict) -> dict:
    """The report fields for a call that never connected."""
    raw = str(data.get("failure_reason") or "").lower()
    reason = FAILURE_REASONS.get(raw, "call_failed")
    return {
        "status": "ended",
        "outcome": "not_reached",
        "reason": reason,
        "summary": None,
        "details_json": {"ended_because": f"call did not connect ({raw or 'unknown'})"},
        "conversation_id": data.get("conversation_id"),
        "duration_secs": 0,
    }


def call_ref(data: dict) -> str | None:
    """The onhold call id carried in the dynamic variables, when present."""
    client = data.get("conversation_initiation_client_data") or {}
    value = (client.get("dynamic_variables") or {}).get("onhold_call_id")
    return value if isinstance(value, str) and value else None


def headline(call: dict) -> str:
    """One line for Pushover: 'Example Dental: partial. 1 of 2 answered.'"""
    outcome = call.get("outcome") or call.get("status")
    if outcome == "not_reached" and call.get("reason"):
        outcome = f"not reached ({call['reason'].replace('_', ' ')})"
    parts = [f"{call['callee_name']}: {str(outcome).replace('_', ' ')}."]
    answers = call.get("answers_json") or []
    if answers:
        got = sum(1 for a in answers if a["answered"])
        parts.append(f"{got} of {len(answers)} answered.")
    follow_ups = call.get("follow_ups_json") or []
    if follow_ups:
        parts.append(f"{len(follow_ups)} follow-up{'s' if len(follow_ups) != 1 else ''}.")
    return " ".join(parts)
