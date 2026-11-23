"""The brief guide: the one place an agent learns to write a brief that drafts first time.

``get_brief_guide`` and the ``onhold://guide`` resource both return ``guide()``.
The checklist states the rules ``brief.parse_brief``/``check_brief`` enforce,
built from the same constants, and every example brief is run through
``parse_brief`` by the tests, so the guide and the validator cannot drift apart.

The examples use fictional 555-01xx numbers and made-up names.
"""

from __future__ import annotations

from typing import Any

from onhold import classes
from onhold.brief import (
    CALL_HOURS_START,
    LATEST_START_NOW,
    MAX_KEYPAD_ENTRIES,
    Item,
    Short,
    Text,
)

EXAMPLE_TZ = "America/Chicago"


def _cap(t: Any) -> int:
    return next(m.max_length for m in t.__metadata__ if getattr(m, "max_length", None))


def examples() -> dict[str, dict[str, Any]]:
    """One complete, valid brief per call class, ready to adapt."""
    return {
        "healthcare": {
            "call_class": "healthcare",
            "callee_name": "Example Family Clinic",
            "to_number": "+15125550131",
            "topic": "a prescription dose question",
            "goal": "Learn whether the doctor approved the dose change and when it is sent.",
            "questions": [
                "Has the doctor approved the dose change?",
                "When will the new prescription be sent?",
            ],
            "may_share": [
                "Patient name: Jordan Example",
                "Date of birth: March 4, 1980",
                "Taking Examplofen, 10 mg once a day",
            ],
            "give_up_when": "they need the patient on the line",
            "callback_number": "+15125550142",
            "callee_timezone": EXAMPLE_TZ,
        },
        "government": {
            "call_class": "government",
            "callee_name": "Example County Tax Office",
            "to_number": "+15125550132",
            "topic": "a licence plate renewal",
            "goal": "Learn whether the plate can be renewed by mail and the fee.",
            "questions": ["Can it be renewed by mail?", "What is the fee?"],
            "may_share": ["Name on the registration: Jordan Example", "Plate number: EXM 4821"],
            "give_up_when": "they need the owner in person",
            "callee_timezone": EXAMPLE_TZ,
        },
        "customer_support": {
            "call_class": "customer_support",
            "callee_name": "Example Internet Co",
            "to_number": "+15125550133",
            "topic": "a billing question",
            "goal": "Learn why the bill went up and whether the old rate can return.",
            "questions": ["Why did the bill go up?", "Can the old rate be restored?"],
            "may_share": ["Account holder: Jordan Example"],
            "keypad_entries": [
                {"label": "Example Internet account number", "value": "8001 2345 6789"}
            ],
            "give_up_when": "they need a PIN",
            "callee_timezone": EXAMPLE_TZ,
        },
    }


def checklist() -> list[str]:
    """The checks draft_call runs, in the words an agent needs to pass them."""
    start, latest = f"{CALL_HOURS_START:%H:%M}", f"{LATEST_START_NOW:%H:%M}"
    return [
        "Numbers: E.164 (+15125550123), US/Canada only, no 900/976/N11; callback_number "
        "differs from to_number.",
        f"callee_timezone (IANA, e.g. {EXAMPLE_TZ}) is required. Draft and place the call "
        f"only {start}-{latest} callee time; place_call dials right away.",
        f"Caps: callee_name, topic {_cap(Short)} chars; goal, give_up_when {_cap(Text)}; list "
        f"items {_cap(Item)}; lists 10 items; keypad_entries {MAX_KEYPAD_ENTRIES}.",
        "Anywhere: no SSN, card, bank or routing number. Outside may_share: no 9+ digit run "
        "or street address ('100 Main St'). In may_share, label a number with what it is "
        "('Callback number: 512-555-0142'), only for a category the class allows.",
        "Approved account numbers: keypad_entries (labelled, 4-25 digits).",
        f"call_class defaults to {classes.DEFAULT_CLASS}; a may_share item outside the class "
        "drafts with a warning.",
    ]


TIPS = [
    "A specific goal: what done looks like.",
    "Questions in the order to ask them.",
    "A clear give_up_when.",
    "may_share items as the agent would say them, only what the owner allowed.",
    "Reuse a working menu_path from get_menu_paths.",
]


def guide(call_class: str | None = None) -> dict[str, Any]:
    """The whole guide, or with ``call_class`` just that class's rule and example."""
    ex = examples()
    if call_class is not None:
        return {
            "call_class": call_class,
            "allows": classes.describe(call_class),
            "example": ex[call_class],
        }
    return {
        "classes": {c: classes.shareable_text(c) for c in classes.CALL_CLASSES},
        "checklist": checklist(),
        "tips": TIPS,
        "examples": ex,
    }
