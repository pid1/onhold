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
* a call cut off at the duration cap while waiting is a hold timeout;
* a call the other side hung up on before the agent got past its opening line
  is a hang-up, not a conversation: not reached;
* a brief whose questions all went unanswered did not meet its goal.

Answers come from the ``qa`` data collection: every question the agent asked
and what the other person said, one ``Q: ... || A: ...`` pair per line. The
analyzer sees only the transcript, never the brief, so each pair is matched to
the brief's questions here (``match_answers``). Older conversations carry
``answer_1``...``answer_10``, which are still read when present.

When the outcome is corrected away from ``goal_met``, a ``goal_achieved``
evaluation of ``success`` is corrected with it, the way that criterion reads.

Every adjustment is recorded in ``details.checks`` so the report says why.

ElevenLabs' post-call analysis does not substitute the brief's dynamic
variables, so it cannot know the call's class or may_share. ``apply_brief_rules``
judges ``shared_only_permitted_info`` and the charge-on-file rule from the
stored brief instead (see its docstring).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

from onhold import classes
from onhold.brief import item_categories

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
GOAL = "goal_achieved"
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
# Deprecated evaluation criterion ids -> the criterion that replaced them. Responses
# carry the old id too, with the new one's result, for one release.
CRITERION_ALIASES = {"no_sensitive_info_shared": "shared_only_permitted_info"}
MAX_SHARED_ITEMS = 20
MAX_OTHER_QUESTIONS = 20
PERMITTED = "shared_only_permitted_info"
COMMITMENTS = "no_commitments_or_payments"
# Said on every call, whatever the brief: the opening line names the owner, and
# the agent offers the callback number when asked.
ALWAYS_SHAREABLE = ("name", "callback_number")
# One ``qa`` line: 'Q: <question> || A: <answer>', with any bullet or number before it.
QA_LINE = re.compile(
    r"^\s*(?:[-*\u2022]|\d+[.)])?\s*Q\s*[:.]\s*(?P<q>.*?)\s*\|\|\s*A\s*[:.]\s*(?P<a>.*?)\s*$",
    re.IGNORECASE,
)
# A question the agent asked matches a brief question when it has at least this
# share of the brief question's content words (see ``similarity``).
MATCH_THRESHOLD = 0.6
# A shorter word this long or longer matches a longer one it starts ("refill",
# "refills"; "pick", "pickup").
PREFIX_MIN = 4
STOPWORDS = frozenset(
    """a about after again all also am an and any anything are as ask at be been before
    being but by can could did do does doing for from get got had has have having he her
    here him his how i if in into is it its just know let like may me might more most much
    my need no not now of off on once one only or other our out over please really she
    should so some still such tell than thank thanks that the their them then there these
    they this those to too up us very was way we were what whats when where whether which
    while who whom why will with would yes you your yours""".split()
)
# The opening line's AI disclosure (agent/first_message.txt), which marks the
# agent's first words to a person.
DISCLOSURE = re.compile(r"\bAI\b")
# ElevenLabs' termination reason when the person called hung up.
REMOTE_HANGUP = re.compile(r"remote party", re.IGNORECASE)
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


def _opening_only(transcript: list[dict]) -> str | None:
    """The opening line, when the agent said nothing after it; else None.

    The opening line is the agent's first line with the AI disclosure, or its
    first line at all when none has it.
    """
    agent = [t["message"] for t in transcript if t["role"] == "agent"]
    if not agent:
        return None
    opening = next((i for i, m in enumerate(agent) if DISCLOSURE.search(m)), 0)
    return agent[opening] if opening == len(agent) - 1 else None


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
    reported = outcome

    answers, other_questions = match_answers(
        questions[:MAX_QUESTIONS], parse_qa(data.get("qa")), data
    )
    any_answered = any(a["answered"] for a in answers)
    shared = _lines(data.get("owner_facts_shared"))

    if outcome is None:
        outcome = "partial" if heard_them else "not_reached"
        checks.append("The agent did not report an outcome; it was worked out from the call.")
    if not heard_them and outcome != "not_reached":
        checks.append(f"Reported {outcome}, but nobody on the other end spoke: not reached.")
        outcome = "not_reached"
    opening = _opening_only(transcript)
    if (
        outcome != "not_reached"
        and heard_them
        and REMOTE_HANGUP.search(termination)
        and opening is not None
        and not any_answered
        # Anything the agent says it shared must have been in the opening line
        # (the owner's name, the topic).
        and all(fact.lower() in opening.lower() for fact in shared)
    ):
        checks.append(
            f"Reported {outcome}, but they hung up before the agent said more than its"
            " opening line: not reached."
        )
        outcome, reason = "not_reached", "hung_up"
    if outcome == "goal_met" and answers and not any_answered:
        checks.append(
            f"Reported goal_met, but none of the {len(answers)} questions was answered:"
            " marked partial."
        )
        outcome = "partial"
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

    evaluation = {
        k: (v or {}).get("result")
        for k, v in (analysis.get("evaluation_criteria_results") or {}).items()
    }
    if reported == "goal_met" and outcome != "goal_met" and evaluation.get(GOAL) == "success":
        # The criterion reads: unknown if no person was reached, failure if not met.
        evaluation[GOAL] = "unknown" if outcome == "not_reached" else "failure"
        checks.append(f"The {GOAL} evaluation said success; corrected to {evaluation[GOAL]}.")

    details = {k: data.get(k) for k in DETAIL_FIELDS if data.get(k) not in (None, "")}
    shared_items = [i for i in _lines(data.get("shared_items")) if i.lower() not in NOT_ANSWERED]
    if shared_items:
        details["shared_items"] = shared_items[:MAX_SHARED_ITEMS]
    if other_questions:
        details["other_questions"] = other_questions[:MAX_OTHER_QUESTIONS]
    charge = {k: str(data.get(f"charge_on_file_{k}") or "").strip() for k in ("amount", "purpose")}
    if any(v.lower() not in NOT_ANSWERED for v in charge.values()):
        details["charge_on_file"] = {
            k: (v if v.lower() not in NOT_ANSWERED else None) for k, v in charge.items()
        }
    details["ended_because"] = termination or None
    if checks:
        details["checks"] = checks

    return {
        "status": "ended",
        "outcome": outcome,
        "reason": reason,
        "summary": analysis.get("transcript_summary") or None,
        "answers_json": answers,
        "follow_ups_json": _lines(data.get("follow_ups")),
        "details_json": details,
        "shared_json": shared,
        "evaluation_json": evaluation,
        "conversation_id": conv.get("conversation_id"),
        "started_at": started,
        "ended_at": (started + duration) if started else None,
        "duration_secs": duration,
        "cost": meta.get("cost"),
        "transcript_json": transcript,
    }


def _answer(value: Any) -> str | None:
    """The answer text, or None when it says nothing was answered."""
    text = str(value).strip() if value is not None else ""
    return None if text.rstrip(".").strip().lower() in NOT_ANSWERED else text


def parse_qa(value: Any) -> list[dict]:
    """The ``qa`` data collection as ``[{"question", "answer"}]``, in the order asked.

    One pair per line, ``Q: <question> || A: <answer>``. ``answer`` is None when
    the line says it was not answered. Lines in any other shape are skipped.
    """
    out = []
    for line in str(value or "").splitlines():
        m = QA_LINE.match(line)
        if m and m.group("q"):
            out.append({"question": m.group("q"), "answer": _answer(m.group("a"))})
    return out


def _content_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"[-'\u2019]", "", text.lower()))
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS))


def _word_match(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    return len(short) >= PREFIX_MIN and long.startswith(short)


def _found(words: list[str], other: list[str]) -> int:
    return sum(any(_word_match(x, y) for y in other) for x in words)


def similarity(question: str, asked: str) -> float:
    """How much of a brief question the agent's question covers, 0 to 1.

    Both are lowercased and stripped of stopwords. The score is the share of
    the brief question's content words that the asked question has: the same
    word, or one starting with the other when the shorter has at least
    ``PREFIX_MIN`` letters ("renew" and "renewed"). Words the agent added, such
    as the owner's name, do not count against it.
    """
    wq, wa = _content_words(question), _content_words(asked)
    if not wq:
        # Only stopwords ("Who is it?"): the same words or nothing.
        same = re.findall(r"[a-z0-9]+", question.lower()) == re.findall(r"[a-z0-9]+", asked.lower())
        return 1.0 if same else 0.0
    return _found(wq, wa) / len(wq)


def _dice(a: str, b: str) -> float:
    """Shared content words over all of them, both ways: breaks ties in coverage."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return (_found(wa, wb) + _found(wb, wa)) / (len(wa) + len(wb))


def match_answers(
    questions: list[str], pairs: list[dict], data: dict | None = None
) -> tuple[list[dict], list[dict]]:
    """Each brief question's answer, and the asked pairs that match no question.

    A pair matches a question when ``similarity`` is at least ``MATCH_THRESHOLD``.
    Matching is one to one, answered pairs first, then by score (ties by the
    Dice score over both questions' content words), then in order:
    a question asked twice gets the try that was answered, and the other try is
    left over. An old conversation's
    ``answer_N`` (from ``data``), when present and non-empty, is used for
    question N unless it says not answered and a matched pair has an answer.
    """
    data = data or {}
    scored = sorted(
        (p["answer"] is None, -score, -_dice(q, p["question"]), qi, pi)
        for qi, q in enumerate(questions)
        for pi, p in enumerate(pairs)
        if (score := similarity(q, p["question"])) >= MATCH_THRESHOLD
    )
    matched: dict[int, int] = {}
    used: set[int] = set()
    for *_, qi, pi in scored:
        if qi not in matched and pi not in used:
            matched[qi] = pi
            used.add(pi)
    answers = []
    for qi, q in enumerate(questions):
        legacy = data.get(f"answer_{qi + 1}")
        text = _answer(legacy)
        if text is None and qi in matched:
            text = pairs[matched[qi]]["answer"]
        answers.append({"question": q, "answered": text is not None, "answer": text})
    other = [
        {"question": p["question"], "answer": p["answer"]}
        for pi, p in enumerate(pairs)
        if pi not in used
    ]
    return answers, other


def _option(reason: Any) -> str:
    text = " ".join(str(reason or "").split())[:MAX_OPTION_CHARS].strip()
    return "" if OPTION_DIGITS.search(text) else text


def menu_presses(conv: dict, entries: Iterable[str] = ()) -> list[dict] | None:
    """The keypad presses the agent made, in order: ``[{"press", "option"}]``.

    Read from the transcript's tool calls, the record of what was actually
    pressed, not from anything the agent or the callee said. ``option`` is the
    agent's own reason for the press. A press that failed is left out, and so is
    a press that keyed in one of ``entries`` (the digits of the brief's keypad
    entries), so an account number never becomes part of a saved path. Returns
    None when the presses cannot be a menu path (too many, or one too long to be
    a menu choice), so nothing is learned from that call.
    """
    entry_digits = {e for e in entries if e}
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
            if re.sub(r"[*#]", "", tones) in entry_digits:
                continue
            if not PRESS.match(tones):
                return None
            steps.append({"press": tones, "option": _option(params.get("reason"))})
    if len(steps) > MAX_MENU_STEPS:
        return None
    return steps


def shared_category(item: str) -> str | None:
    """The class-table id for one shared item ('date_of_birth'), else None."""
    text = " ".join(item.lower().replace("_", " ").split())
    for cid, label in classes.CATEGORIES.items():
        if text in (label.lower(), cid.replace("_", " ")) or text.startswith(f"{label.lower()}:"):
            return cid
    return None


def shared_kind(item: str) -> str:
    """The class-table name for one shared item ('date of birth'), else 'other'."""
    cid = shared_category(item)
    return classes.CATEGORIES[cid] if cid else "other"


# Stricter first. A missing result is as good as unknown.
_SEVERITY = {"failure": 2, "unknown": 1, None: 1, "success": 0}


def permitted_categories(b: dict) -> set[str]:
    """The categories a call may share: its class, narrowed to what the brief carries.

    Name and callback number always; otherwise a category the class allows that
    a may_share item, a keypad entry's label, the topic or the goal looks like
    (``brief.item_categories``). An old brief without a class is customer_support.
    """
    call_class = b.get("call_class") or classes.DEFAULT_CLASS
    in_class = set(classes.CLASS_CATEGORIES.get(call_class, ()))
    texts = [
        *(b.get("may_share") or []),
        *(str((e or {}).get("label") or "") for e in b.get("keypad_entries") or []),
        str(b.get("topic") or ""),
        str(b.get("goal") or ""),
    ]
    named = {c for t in texts for c in item_categories(t)}
    return set(ALWAYS_SHAREABLE) | (named & in_class)


def apply_brief_rules(fields: dict, b: dict) -> dict:
    """Judge the class and charge rules from the stored brief ``b``; returns ``fields``.

    ElevenLabs' evaluation cannot see the brief (it does not substitute dynamic
    variables), so onhold decides ``shared_only_permitted_info`` from the
    report's ``shared_items``:

    * a shared category the class does not allow, or that nothing in the brief
      carries (``permitted_categories``), is a failure;
    * when every item maps to the class table and all are permitted, success;
    * when anything shared is 'other' (unmapped), the stricter of onhold's
      result and ElevenLabs';
    * with nothing recorded as shared, ElevenLabs' result stands.

    A charge on file recorded when the brief did not allow one fails
    ``no_commitments_or_payments``; otherwise ElevenLabs' result stands. Each
    change is noted in ``details.checks``. The deprecated id, if the conversation
    carried it, is replaced by the new one, so ``with_aliases`` mirrors the final
    result.
    """
    evaluation = dict(fields.get("evaluation_json") or {})
    details = dict(fields.get("details_json") or {})
    notes: list[str] = []
    old_ids = [old for old, new in CRITERION_ALIASES.items() if new == PERMITTED]

    items = details.get("shared_items") or []
    if items:
        call_class = b.get("call_class") or classes.DEFAULT_CLASS
        in_class = set(classes.CLASS_CATEGORIES.get(call_class, ()))
        allowed = permitted_categories(b)
        cats = [shared_category(i) for i in items]
        problems = []
        for c in dict.fromkeys(c for c in cats if c and c not in allowed):
            why = (
                f"which a {call_class} call does not share"
                if c not in in_class
                else "which the brief's may_share did not include"
            )
            problems.append(f"a {classes.CATEGORIES[c]} was shared, {why}")
        ours = "failure" if problems else "success"
        theirs = evaluation.get(PERMITTED)
        if theirs is None:
            theirs = next((evaluation[o] for o in old_ids if o in evaluation), None)
        final = ours
        if None in cats and _SEVERITY.get(theirs, 1) > _SEVERITY[ours]:
            final = theirs if theirs is not None else "unknown"
        if final != theirs:
            if problems:
                why = "; ".join(problems)
            elif None in cats:
                why = "something shared is not in the class table, so it stays unconfirmed"
            else:
                why = f"everything shared is permitted on this {call_class} call"
            notes.append(
                f"The {PERMITTED} evaluation said {theirs or 'nothing'}; onhold's check of the"
                f" class and may_share: {why}. Marked {final}."
            )
        evaluation[PERMITTED] = final
        # An older agent's id goes; ``with_aliases`` returns it with the final result.
        for old in old_ids:
            evaluation.pop(old, None)

    if details.get("charge_on_file") and not b.get("allow_charge_on_file", False):
        if evaluation.get(COMMITMENTS) != "failure":
            notes.append(
                f"The {COMMITMENTS} evaluation said {evaluation.get(COMMITMENTS) or 'nothing'};"
                " a charge on file was agreed, but this call did not allow one. Marked failure."
            )
        evaluation[COMMITMENTS] = "failure"

    if notes:
        details["checks"] = [*(details.get("checks") or []), *notes]
    return fields | {"evaluation_json": evaluation, "details_json": details}


def evaluation_details(call: dict) -> dict:
    """What the safety evaluation judged against: the class, what was shared, any charge.

    Returned outside ``untrusted_external_content``, so it holds no free text:
    ``call_class`` and ``allowed`` come from the brief (an old brief has neither:
    customer_support, not allowed); ``shared_items`` are names from the fixed
    class table only, with anything else counted in ``shared_other``; a charge
    on file is only ``recorded``. The agent's own lines, and the charge's amount
    and purpose, stay in the report's details, which are untrusted.
    """
    b = call.get("brief_json") or {}
    details = call.get("details_json") or {}
    call_class = b.get("call_class") or classes.DEFAULT_CLASS
    kinds = [shared_kind(i) for i in details.get("shared_items") or []]
    out: dict[str, Any] = {
        "call_class": call_class,
        "class_may_share": classes.shareable(call_class)
        if call_class in classes.CLASS_CATEGORIES
        else [],
        "shared_items": list(dict.fromkeys(k for k in kinds if k != "other")),
        "shared_other": "other" in kinds,
    }
    allowed = bool(b.get("allow_charge_on_file", False))
    recorded = bool(details.get("charge_on_file"))
    if allowed or recorded:
        out["charge_on_file"] = {"allowed": allowed, "recorded": recorded}
    return out


def with_aliases(evaluation: dict) -> tuple[dict, dict]:
    """``evaluation`` plus each deprecated id that has a replacement result, and the aliases."""
    out = dict(evaluation)
    aliases = {}
    for old, new in CRITERION_ALIASES.items():
        if new in out and old not in out:
            out[old] = out[new]
            aliases[old] = f"deprecated: same as {new}; removed in the next release"
    return out, aliases


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
