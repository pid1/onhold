"""The call brief: everything the agent knows about one call, and nothing more.

A call never starts without a brief, written for a call the owner asked for
and approved. The brief is validated strictly here, before any request leaves
the Worker, and every problem is reported at once: ``parse_brief`` and
``check_brief`` raise one ``BriefError`` whose ``issues`` name each failing
field as a JSON path (``brief.may_share[2]``, ``brief.call_window.start``).

* numbers are E.164, in an allowed country code, and not premium-rate or
  N11 service numbers;
* every free-text field has a length cap, and lists have item caps;
* the call has a class (``call_class``, see ``onhold.classes``), which decides
  which categories of personal fact the agent may share when asked. The
  agent's never-share list is ``classes.ALWAYS_FORBIDDEN``, plus each category
  the class does not allow ("Any date of birth" on a customer_support call),
  plus the brief's own ``must_not_share``;
* forbidden in every field and every class, and never echoed back in an
  issue: a Social Security number (any 9-digit number, with or without
  separators, standing on its own), a payment card number (13-19 digits that
  pass the Luhn check) and a bank routing number (9 digits that pass the ABA
  checksum). Keypad entry values get the card check too;
* other free text (everything but ``may_share``) may not carry a long digit
  run (9 or more digits) or a street address;
* ``may_share`` may carry a street address when the class allows addresses
  (every v1 class does), and a long number only when the item is labelled as a
  numbered category the class allows: a callback or phone number, a customer
  account or order number (customer_support), an insurance member ID
  (healthcare), a licence plate or a case or permit number (government). The
  label is matched by keyword, e.g. "Member ID W123456789" or "Callback number
  512-555-0142";
* a ``may_share`` item that looks like a category outside the class (a date of
  birth on a customer_support call) is not refused but warned about
  (``class_warnings``), so the owner sees it in the draft preview;
* the other way a long number reaches the agent is ``keypad_entries``: numbers
  the owner approved for this call (an account number, the phone number on the
  account), each labelled, never an SSN-shaped value. The agent keys them into
  the callee's phone menu or reads them to the callee's representative, and
  they are shown in the preview like everything else.

The brief becomes the agent's dynamic variables (``to_dynamic_variables``).
ElevenLabs accepts only strings, numbers and booleans there, so lists are
rendered as numbered lines.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from onhold import classes
from onhold.classes import CallClass
from onhold.errors import OnholdError, issue

E164 = re.compile(r"^\+[1-9]\d{7,14}$")
# NANP numbers that must never be dialled by an agent: premium rate (900, 976),
# N11 service codes and invalid area codes or exchanges.
NANP_BLOCKED_AREA_CODES = {"900", "976"}
# +1 also covers Caribbean and Atlantic countries billed as international calls
# and common in toll fraud; only the US (with territories) and Canada are dialled.
NANP_NON_US_CA = {
    "242",
    "246",
    "264",
    "268",
    "284",
    "345",
    "441",
    "473",
    "649",
    "658",
    "664",
    "721",
    "758",
    "767",
    "784",
    "809",
    "829",
    "849",
    "868",
    "869",
    "876",
}
SSN = re.compile(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b")
LONG_DIGITS = re.compile(r"(?:\d[ -]?){9,}")
# A number followed by a dose or quantity unit ("1 mg weekly per Dr Lee") is not
# a house number.
STREET = re.compile(
    r"\b\d{1,6}\s+(?!(?:mg|mcg|ml|g|kg|units?|iu|tabs?|tablets?|pills?|doses?|times|x)\b)(?:[A-Za-z0-9.'-]+\s+){1,4}"
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|court|way|"
    r"pl|place|pkwy|parkway|hwy|highway|cir|circle|ter|terrace|trl|trail)\b\.?",
    re.IGNORECASE,
)

# The longest any call may run, everywhere: the voice agent's own cap
# (agent.json), the report's hold-timeout check and the calling hours below.
MAX_CALL_SECS = 3600
MAX_WINDOW_HOURS = 12  # 08:00-20:00: the last call that can still end by 21:00
MAX_SCHEDULE_DAYS = 14
MIN_LEAD = timedelta(seconds=60)
MIN_WINDOW_LEFT = timedelta(minutes=10)
# TCPA calling hours, in the callee's local time: 8:00 a.m. to 9:00 p.m.
# (https://elevenlabs.io/docs/eleven-agents/legal/tcpa). A call placed now must
# start by 20:00 so the one-hour cap still ends it by 21:00. A call_window
# must end by then too, since a scheduled call may dial at any point in it.
CALL_HOURS_START = time(8, 0)
CALL_HOURS_END = time(21, 0)
LATEST_START_NOW = time(21 - MAX_CALL_SECS // 3600, 0)

# Always forbidden, whatever the brief or the class says. Rendered into
# must_not_share ahead of the categories the class does not allow.
STANDING_DO_NOT_SHARE = (
    *classes.ALWAYS_FORBIDDEN,
    "Any driver's licence, passport or PIN number, and any other account, ID or policy "
    "number, except the keypad entries the owner approved for this call and what may_share "
    "lists",
)

CHARGE_ON_FILE_ALLOWED = (
    "Allowed only for the charge the goal is about; never read out or confirm card or "
    "account digits"
)
CHARGE_ON_FILE_NOT_ALLOWED = "Not allowed: never agree to a charge"

# A run of digits standing on its own (not part of a word such as W123456789),
# allowing one space, dot or hyphen between digits.
DIGIT_RUN = re.compile(r"(?<![A-Za-z0-9])\d(?:[ .-]?\d)*(?![A-Za-z0-9])")
NO_PAYMENT_OR_SSN = (
    "no Social Security, card, bank account or routing numbers; the owner handles anything "
    "that needs one on their own call"
)
SSN_PROBLEM = "looks like a Social Security number; the agent never says one, on any call"
CARD_PROBLEM = "looks like a payment card number; card and bank details never go in a brief"
ROUTING_PROBLEM = "looks like a bank routing number; card and bank details never go in a brief"

# Keywords that mark which category a may_share item is. Used to allow a long
# number in may_share and to warn about items outside the call's class.
CATEGORY_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in {
        "date_of_birth": r"\b(?:date of birth|birth ?date|birthday|d\.?o\.?b\b|born)",
        "callback_number": r"\b(?:call ?back|phone|cell|mobile|reach (?:me|the owner))\b",
        "address": r"\baddress\b",
        "prescriber": r"\b(?:prescriber|prescribed by|doctor|physician)\b",
        "pharmacy": r"\b(?:pharmacy|pharmacist)\b",
        "medication": r"\b(?:medications?|medicines?|prescriptions?|rx|refills?|doses?|"
        r"dosage|\d+(?:\.\d+)?\s?(?:mg|mcg|ml))\b",
        "insurance_member_id": r"\b(?:insurance|insurer|member (?:id|number|no\b|#)|"
        r"subscriber (?:id|number)|group (?:number|no\b|#)|policy (?:number|no\b|#))",
        "license_plate": r"\b(?:licen[cs]e plates?|plates? (?:number|no\b|#)|tag number|plate)\b",
        "case_number": r"\b(?:case|permit|docket|citation|application)\s*(?:number|no\b|#|id\b)",
        "account_number": r"\b(?:account|acct)\b",
        "order_number": r"\border\s*(?:number|no\b|#|id\b)",
    }.items()
}
# Categories whose value is itself a number, so a long digit run is expected.
NUMBERED_CATEGORIES = (
    "callback_number",
    "insurance_member_id",
    "license_plate",
    "case_number",
    "account_number",
    "order_number",
)

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Item = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class BriefError(OnholdError):
    """The brief was refused. The message names the field and the reason; ``issues`` lists each."""

    code = "invalid_brief"


def check_number(value: str, allowed_country_codes: tuple[str, ...] = ("1",)) -> str:
    value = value.strip()
    if not E164.match(value):
        raise BriefError(f"'{value}' is not an E.164 number like +12025550123")
    digits = value[1:]
    if not any(digits.startswith(cc) for cc in allowed_country_codes):
        raise BriefError(
            f"{value}: country code not allowed (allowed: {', '.join(allowed_country_codes)})"
        )
    if digits.startswith("1"):
        if len(digits) != 11:
            raise BriefError(f"{value}: North American numbers have 10 digits after +1")
        area, exchange = digits[1:4], digits[4:7]
        if area in NANP_BLOCKED_AREA_CODES or area[0] in "01" or area[1:] == "11":
            raise BriefError(f"{value}: area code {area} is not callable by the agent")
        if area in NANP_NON_US_CA:
            raise BriefError(f"{value}: area code {area} is outside the US and Canada")
        if exchange[0] in "01" or exchange[1:] == "11":
            raise BriefError(f"{value}: exchange {exchange} is not a valid subscriber number")
    return value


def _check_tz(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError, ValueError:
        raise BriefError(
            f"unknown time zone '{value}'; use an IANA zone such as America/Chicago"
        ) from None
    return value


def check_call_hours(now: datetime, tz: str) -> None:
    """Refuse a call placed now outside 8:00-20:00 in the callee's time zone."""
    local = now.astimezone(ZoneInfo(tz)).time()
    if not (CALL_HOURS_START <= local <= LATEST_START_NOW):
        problem = (
            f"it is {local:%H:%M} in {tz}; calls are placed only "
            f"{CALL_HOURS_START:%H:%M}-{LATEST_START_NOW:%H:%M} callee time (TCPA hours "
            "8 a.m.-9 p.m., less the one-hour call cap). Draft it with a call_window and use "
            "schedule_call."
        )
        raise BriefError(
            problem,
            [issue("brief.call_window", problem, expected=f"a call_window {WINDOW_RULES}")],
        )


def speakable_number(e164: str) -> str:
    """+15125550123 -> 512-555-0123 (other countries unchanged)."""
    if e164.startswith("+1") and len(e164) == 12:
        d = e164[2:]
        return f"{d[:3]}-{d[3:6]}-{d[6:]}"
    return e164


def luhn_valid(digits: str) -> bool:
    """The card-number checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def aba_valid(digits: str) -> bool:
    """The ABA routing-number checksum (weights 3, 7, 1)."""
    if len(digits) != 9:
        return False
    return sum(int(d) * w for d, w in zip(digits, (3, 7, 1) * 3, strict=True)) % 10 == 0


def forbidden_problems(text: str, ssn: bool = True) -> list[str]:
    """What ``text`` carries that no brief may: SSN, card or routing numbers.

    With ``ssn=False`` only card numbers are looked for (keypad entry values,
    where a 9-digit account number the owner approved is expected).
    """
    found: list[str] = []
    if ssn and SSN.search(text):
        found.append(SSN_PROBLEM)
    for m in DIGIT_RUN.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) == 9 and ssn:
            problem = (
                ROUTING_PROBLEM
                if aba_valid(digits) and not SSN.fullmatch(m.group())
                else SSN_PROBLEM
            )
        elif 13 <= len(digits) <= 19 and luhn_valid(digits):
            problem = CARD_PROBLEM
        else:
            continue
        if problem not in found:
            found.append(problem)
    return found


def _forbidden_issues(field: str, text: str, ssn: bool = True) -> list[dict[str, Any]]:
    # Never echo the value: say what it looked like instead.
    return [
        issue(field, p, expected=NO_PAYMENT_OR_SSN, received="(withheld)")
        for p in forbidden_problems(text, ssn=ssn)
    ]


def item_categories(text: str) -> list[str]:
    """The categories (``classes.CATEGORIES`` ids) a may_share item looks like."""
    found = [name for name, pattern in CATEGORY_PATTERNS.items() if pattern.search(text)]
    if "address" not in found and STREET.search(text):
        found.append("address")
    if "callback_number" not in found:
        for m in DIGIT_RUN.finditer(text):
            digits = re.sub(r"\D", "", m.group())
            if len(digits) == 10 or (len(digits) == 11 and digits[0] == "1"):
                found.append("callback_number")
                break
    return [c for c in classes.CATEGORIES if c in found]


def _text_issues(field: str, text: str) -> list[dict[str, Any]]:
    """Free text other than may_share: nothing forbidden, no long numbers, no addresses."""
    out = _forbidden_issues(field, text)
    if not out and LONG_DIGITS.search(text):
        out.append(
            issue(
                field,
                "contains a long number (card, account or licence numbers are never shared "
                "on a call)",
                expected="phone numbers in to_number or callback_number; an account number "
                "the owner approved in keypad_entries; a number the callee may be told in "
                "may_share, labelled with what it is",
            )
        )
    if STREET.search(text):
        out.append(
            issue(
                field,
                "looks like a street address; the agent shares an address only from may_share",
                expected="the address, if the owner said the agent may give it, as a "
                "may_share item",
            )
        )
    return out


def _may_share_issues(field: str, text: str, call_class: str) -> list[dict[str, Any]]:
    """A may_share item: nothing forbidden; addresses and labelled numbers the class allows."""
    out = _forbidden_issues(field, text)
    allowed = classes.CLASS_CATEGORIES[call_class]
    cats = item_categories(text)
    if not out and LONG_DIGITS.search(text):
        if not any(c in allowed and c in NUMBERED_CATEGORIES for c in cats):
            numbered = [classes.CATEGORIES[c] for c in allowed if c in NUMBERED_CATEGORIES]
            out.append(
                issue(
                    field,
                    f"contains a long number that isn't labelled as something a {call_class} "
                    "call may share",
                    expected=f"a number labelled as one of: {', '.join(numbered)} (e.g. "
                    "'Callback number 512-555-0100'); or an approved account number in "
                    "keypad_entries",
                )
            )
    if STREET.search(text) and "address" not in allowed:
        out.append(
            issue(
                field,
                f"looks like a street address, which a {call_class} call never shares",
                expected=f"what a {call_class} call may share: "
                f"{classes.shareable_text(call_class)}",
            )
        )
    return out


TEXT_FIELDS = ("callee_name", "topic", "goal", "give_up_when")
TEXT_LISTS = ("questions", "must_not_share")


def _class_of(data: dict) -> str:
    c = data.get("call_class")
    return c if c in classes.CALL_CLASSES else classes.DEFAULT_CLASS


def content_issues(data: dict) -> list[dict[str, Any]]:
    """Content checks over a brief as a dict, so they run even when the schema fails."""
    out: list[dict[str, Any]] = []
    call_class = _class_of(data)

    def items(name: str) -> list[tuple[int, Any]]:
        v = data.get(name)
        return list(enumerate(v)) if isinstance(v, list) else []

    for name in TEXT_FIELDS:
        if isinstance(data.get(name), str):
            out += _text_issues(f"brief.{name}", data[name])
    for name in TEXT_LISTS:
        for i, v in items(name):
            if isinstance(v, str):
                out += _text_issues(f"brief.{name}[{i}]", v)
    for i, v in items("may_share"):
        if isinstance(v, str):
            out += _may_share_issues(f"brief.may_share[{i}]", v, call_class)
    for i, step in items("menu_path"):
        if isinstance(step, dict) and isinstance(step.get("option"), str):
            out += _text_issues(f"brief.menu_path[{i}].option", step["option"])
    for i, entry in items("keypad_entries"):
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("label"), str):
            out += _text_issues(f"brief.keypad_entries[{i}].label", entry["label"])
        if isinstance(entry.get("value"), str):
            out += _forbidden_issues(f"brief.keypad_entries[{i}].value", entry["value"], ssn=False)
    return out


def _raise(issues: list[dict[str, Any]]) -> None:
    if not issues:
        return
    lines = [f"{i['field']}: {i['problem']}" for i in issues]
    if len(lines) == 1:
        raise BriefError(lines[0], issues)
    raise BriefError(f"{len(lines)} problems with the brief: " + "; ".join(lines), issues)


WINDOW_TIME_FORMAT = (
    "the callee's local wall time, YYYY-MM-DDTHH:MM with no offset, e.g. 2026-11-06T09:00"
)
WINDOW_RULES = (
    f"in the future, within {CALL_HOURS_START:%H:%M}-{LATEST_START_NOW:%H:%M} on one day in "
    f"the callee's time zone, at most {MAX_WINDOW_HOURS} hours long, starting within "
    f"{MAX_SCHEDULE_DAYS} days"
)


class CallWindow(BaseModel):
    """When the call may be placed, in the business's local time."""

    model_config = ConfigDict(extra="forbid")

    timezone: Annotated[
        str, Field(description="IANA time zone of the business, e.g. America/Chicago")
    ]
    start: Annotated[
        datetime,
        Field(
            description=f"Window start: {WINDOW_TIME_FORMAT}. The window must be {WINDOW_RULES}."
        ),
    ]
    end: Annotated[
        datetime,
        Field(
            description="Window end: the callee's local wall time in the same format, e.g. "
            f"2026-11-06T12:00, on the same day as start. The window must be {WINDOW_RULES}."
        ),
    ]

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _check_tz(v)

    def bounds_utc(self) -> tuple[datetime, datetime]:
        tz = ZoneInfo(self.timezone)
        start = self.start if self.start.tzinfo else self.start.replace(tzinfo=tz)
        end = self.end if self.end.tzinfo else self.end.replace(tzinfo=tz)
        return start.astimezone(UTC), end.astimezone(UTC)

    def issues(self, now: datetime, prefix: str = "brief.call_window") -> list[dict[str, Any]]:
        """Every problem with the window, each naming ``start`` or ``end``."""
        tz = ZoneInfo(self.timezone)
        start, end = self.bounds_utc()
        local_start, local_end = start.astimezone(tz), end.astimezone(tz)
        shown = {"start": f"{local_start:%Y-%m-%dT%H:%M}", "end": f"{local_end:%Y-%m-%dT%H:%M}"}

        def bad(name: str, problem: str, expected: str) -> dict[str, Any]:
            return issue(f"{prefix}.{name}", problem, expected=expected, received=shown[name])

        if end <= start:
            return [bad("end", "end must be after start", "a time later than start")]
        out = []
        if end - start > timedelta(hours=MAX_WINDOW_HOURS):
            out.append(
                bad("end", f"at most {MAX_WINDOW_HOURS} hours long", "an end closer to start")
            )
        tcpa = (
            f"must lie within {CALL_HOURS_START:%H:%M}-{LATEST_START_NOW:%H:%M} on one day in "
            "the callee's time zone (TCPA calling hours, less the one-hour call cap)"
        )
        if local_start.time() < CALL_HOURS_START:
            out.append(bad("start", tcpa, f"{CALL_HOURS_START:%H:%M} or later, local time"))
        if local_start.date() != local_end.date() or local_end.time() > LATEST_START_NOW:
            out.append(
                bad("end", tcpa, f"{LATEST_START_NOW:%H:%M} or earlier, the same day as start")
            )
        if start - now > timedelta(days=MAX_SCHEDULE_DAYS):
            out.append(bad("start", f"must start within {MAX_SCHEDULE_DAYS} days", "a nearer date"))
        if end - max(start, now + MIN_LEAD) < MIN_WINDOW_LEFT:
            out.append(
                bad(
                    "end",
                    "the window has ended or is about to end",
                    f"an end at least {MIN_WINDOW_LEFT.seconds // 60} minutes from now; it is "
                    f"{now.astimezone(tz):%Y-%m-%dT%H:%M} in {self.timezone}",
                )
            )
        return out

    def dial_time(self, now: datetime) -> datetime:
        """The UTC moment to dial: the window start, or shortly after now if inside it."""
        _raise(self.issues(now))
        start, _ = self.bounds_utc()
        return max(start, now + MIN_LEAD)

    def contains(self, now: datetime) -> bool:
        start, end = self.bounds_utc()
        return start <= now <= end - MIN_WINDOW_LEFT


class MenuStep(BaseModel):
    """One keypad press on the way through a phone menu."""

    model_config = ConfigDict(extra="forbid")

    press: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9*#]{1,4}$"),
        Field(description="Keypad digits for this menu, e.g. '2' (1-4 of 0-9, * and #)"),
    ]
    option: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=80),
        Field(description="What the press is for, e.g. 'billing'"),
    ] = ""


MAX_KEYPAD_ENTRIES = 3
KEYPAD_VALUE = re.compile(r"^\d(?:[ -]?\d){3,24}$")


class KeypadEntry(BaseModel):
    """A number the owner approved for this call: an account number, the phone
    number on an account, a customer id. The agent may key it in when the
    callee's phone menu asks for it, or read it to the callee's representative
    to find the account. It is the only way a long number reaches the agent."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=60),
        Field(description="What the number is, e.g. 'Frontier account number'"),
    ]
    value: Annotated[
        str,
        StringConstraints(strip_whitespace=True),
        Field(description="The digits, 4-25 of them; spaces and hyphens are allowed"),
    ]

    @field_validator("value")
    @classmethod
    def _value(cls, v: str) -> str:
        if not KEYPAD_VALUE.match(v):
            raise ValueError("must be 4-25 digits, optionally grouped with spaces or hyphens")
        if SSN.fullmatch(v):
            raise ValueError("looks like a Social Security number; the agent never says one")
        return v

    @property
    def digits(self) -> str:
        return re.sub(r"\D", "", self.value)


CALL_CLASS_DESCRIPTION = (
    "Who is being called, which decides what kinds of personal fact the agent may share when "
    "asked (only items that are also in may_share). One of: "
    + "; ".join(classes.describe(c) for c in classes.CALL_CLASSES)
    + f". Default {classes.DEFAULT_CLASS}. Never shared on any call: Social Security numbers, "
    "card or bank details, or changes to a payment method."
)


def must_not_share_list(brief: CallBrief) -> list[str]:
    """The standing list, then each category the class does not allow, then the brief's own."""
    allowed = classes.CLASS_CATEGORIES[brief.call_class]
    barred = [f"Any {classes.CATEGORIES[c]}" for c in classes.CATEGORIES if c not in allowed]
    return [*STANDING_DO_NOT_SHARE, *barred, *brief.must_not_share]


def class_warnings(brief: CallBrief) -> list[dict[str, Any]]:
    """may_share items that look like a category the call's class does not share.

    Not a refusal: the owner sees them in the draft preview. The agent treats
    that category as never-share, and the safety check fails a call that shares it.
    """
    allowed = classes.CLASS_CATEGORIES[brief.call_class]
    out = []
    for i, item in enumerate(brief.may_share):
        for c in item_categories(item):
            if c in allowed:
                continue
            out.append(
                issue(
                    f"brief.may_share[{i}]",
                    f"looks like a {classes.CATEGORIES[c]}, which a {brief.call_class} call "
                    "does not share; the agent will not say it, and the safety check fails "
                    "the call if it does",
                    expected=f"what a {brief.call_class} call may share: "
                    f"{classes.shareable_text(brief.call_class)}; or a call_class that allows "
                    f"a {classes.CATEGORIES[c]}",
                )
            )
    return out


class CallBrief(BaseModel):
    """What the owner's agent writes before any call. Every field is shown to the voice agent."""

    model_config = ConfigDict(extra="forbid")

    callee_name: Annotated[
        Short, Field(description="Who is being called, e.g. 'Travis County Tax Office'")
    ]
    to_number: Annotated[
        str, Field(description="Number to dial, E.164 (+ and country code), e.g. +15125550123")
    ]
    call_class: Annotated[
        CallClass,
        Field(description=CALL_CLASS_DESCRIPTION),
    ] = classes.DEFAULT_CLASS
    topic: Annotated[
        Short,
        Field(
            description="Plain-language topic for the opening line, e.g. 'a licence plate renewal'"
        ),
    ]
    goal: Annotated[Text, Field(description="What a successful call achieves")]
    questions: Annotated[
        list[Item], Field(max_length=10, description="Questions to ask, in order (max 10)")
    ] = []
    may_share: Annotated[
        list[Item],
        Field(
            max_length=10,
            description="Facts the agent may share if asked, each written as the agent would "
            "say it, e.g. 'Date of birth: March 3, 1980' (max 10). Only the owner's own words "
            "about this call, and only kinds of fact the call_class allows. A street address "
            "may go here; a long number only when labelled as a kind the class allows, e.g. "
            "'Callback number 512-555-0100' or 'Insurance member ID W123456789'. Never a "
            "Social Security, card, bank account or routing number.",
        ),
    ] = []
    must_not_share: Annotated[
        list[Item],
        Field(
            max_length=10,
            description="Extra things never to share; the standing list is always added",
        ),
    ] = []
    give_up_when: Annotated[
        Text,
        Field(
            description="When to stop and say the owner will follow up, e.g. 'they need the "
            "owner in person or need ID'"
        ),
    ]
    keypad_entries: Annotated[
        list[KeypadEntry],
        Field(
            max_length=MAX_KEYPAD_ENTRIES,
            description="Numbers the owner approved for this call, such as the account number "
            "or the phone number on the account. The agent keys them in when the phone menu "
            "asks, or reads them to the representative to find the account, and never shares "
            "them with anyone else. Only what the owner said may be used on this call (max 3).",
        ),
    ] = []
    menu_path: Annotated[
        list[MenuStep],
        Field(
            max_length=10,
            description="A phone-menu path that worked before for this number, from "
            "get_menu_paths, in order. The agent tries it first and falls back to "
            "listening if the menu has changed. Leave empty for a number with no saved path.",
        ),
    ] = []
    callback_number: Annotated[
        str | None,
        Field(
            description="Number the callee may call back on (E.164, e.g. +15125550100), only "
            "if the owner gave one; "
            "otherwise the configured callback number is offered"
        ),
    ] = None
    callee_timezone: Annotated[
        str | None,
        Field(
            description="IANA time zone of the business, e.g. America/Chicago. Required unless "
            "call_window is given (TCPA calling hours)."
        ),
    ] = None
    call_window: Annotated[
        CallWindow | None,
        Field(
            description="When a scheduled call may be placed (use schedule_call), e.g. "
            '{"timezone": "America/Chicago", "start": "2026-11-06T09:00", "end": '
            '"2026-11-06T12:00"}. start and end are the callee\'s local wall time, no offset; '
            f"the window must be {WINDOW_RULES}. Omit it to call now with place_call."
        ),
    ] = None
    allow_charge_on_file: Annotated[
        bool,
        Field(
            description="True only if the owner said the agent may agree to a charge to the "
            "card or account the callee already has on file, for the charge the goal is about "
            "(e.g. a refill copay or a fee the goal names). The agent never reads out, gives "
            "or confirms card or account digits, and never adds or changes a payment method. "
            "Default false: the agent never agrees to any charge."
        ),
    ] = False
    reference: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=120),
        Field(
            description="Free text for your own bookkeeping, e.g. 'todo:347'. Stored with the "
            "call and returned by get_call and list_calls; never shown to the voice agent."
        ),
    ] = None

    @field_validator("to_number", "callback_number")
    @classmethod
    def _numbers(cls, v: str | None) -> str | None:
        return None if v is None else check_number(v)

    @field_validator("callee_timezone")
    @classmethod
    def _callee_tz(cls, v: str | None) -> str | None:
        return None if v is None else _check_tz(v)

    @property
    def timezone(self) -> str | None:
        return self.call_window.timezone if self.call_window else self.callee_timezone

    def check_content(self) -> None:
        """Refuse forbidden or misplaced content (raises one BriefError with every issue)."""
        _raise(content_issues(self.model_dump(mode="json")))


def _numbered(items: list[str] | tuple[str, ...], empty: str) -> str:
    if not items:
        return empty
    return "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))


NO_MENU_PATH = "(none saved; listen to the menu)"
NO_KEYPAD_ENTRIES = "(none: key in or read out no account, ID or phone numbers)"


def keypad_entries_text(entries: list[KeypadEntry]) -> str:
    """1. Example account number: 1234 5678 (keypad: 12345678)"""
    return _numbered(
        [f"{e.label}: {e.value} (keypad: {e.digits})" for e in entries], NO_KEYPAD_ENTRIES
    )


def menu_path_text(steps: list[MenuStep]) -> str:
    """1. Press 2 (billing)"""
    return _numbered(
        [f"Press {s.press}" + (f" ({s.option})" if s.option else "") for s in steps],
        NO_MENU_PATH,
    )


def to_dynamic_variables(
    brief: CallBrief,
    owner_name: str,
    assistant_number: str,
    call_id: str,
    default_callback: str | None = None,
) -> dict[str, str]:
    """The voice agent's dynamic variables. ``call_id`` comes back in every webhook.

    The callback number offered is the brief's, else the deployment's default
    (``CALLBACK_NUMBER``), else the assistant's own number.
    """
    window = "none (call placed now)"
    if brief.call_window:
        w = brief.call_window
        window = f"{w.start:%A %Y-%m-%d %H:%M} to {w.end:%H:%M} ({w.timezone})"
    return {
        "owner_name": owner_name,
        "callee_name": brief.callee_name,
        "call_topic": brief.topic,
        "call_goal": brief.goal,
        "questions": _numbered(brief.questions, "(no specific questions)"),
        "may_share": _numbered(brief.may_share, "(nothing beyond the topic and goal)"),
        "must_not_share": _numbered(must_not_share_list(brief), "(standing list only)"),
        "give_up_when": brief.give_up_when,
        "menu_path": menu_path_text(brief.menu_path),
        "keypad_entries": keypad_entries_text(brief.keypad_entries),
        "callback_number": speakable_number(
            brief.callback_number or default_callback or assistant_number
        ),
        "assistant_number": speakable_number(assistant_number),
        "call_window": window,
        "onhold_call_id": call_id,
        "call_class": brief.call_class,
        "class_may_share": classes.shareable_text(brief.call_class),
        "charge_on_file": (
            CHARGE_ON_FILE_ALLOWED if brief.allow_charge_on_file else CHARGE_ON_FILE_NOT_ALLOWED
        ),
    }


_EXPECTED = {
    "missing": "a value",
    "extra_forbidden": "only the documented brief fields",
    "string_too_short": "non-empty text",
    "bool_type": "true or false",
    "bool_parsing": "true or false",
    "list_type": "a list",
    "string_type": "text",
    "model_type": "an object",
    "datetime_type": WINDOW_TIME_FORMAT,
    "datetime_parsing": WINDOW_TIME_FORMAT,
    "datetime_from_date_parsing": WINDOW_TIME_FORMAT,
}


# What a field takes, for errors whose type alone doesn't say (validators' ValueErrors).
_FIELD_EXPECTED = {
    "timezone": "an IANA zone such as America/Chicago",
    "callee_timezone": "an IANA zone such as America/Chicago",
    "to_number": "an E.164 number in the US or Canada, e.g. +15125550123",
    "callback_number": "an E.164 number in the US or Canada, e.g. +15125550123",
    "value": "4-25 digits, optionally grouped with spaces or hyphens; never an SSN",
    "press": "1-4 of 0-9, * and #",
}


def _path(loc: tuple) -> str:
    out = "brief"
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _safe_received(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, str) and len(value) <= 80 and not forbidden_problems(value):
        return value
    return None


def validation_issues(e: ValidationError) -> list[dict[str, Any]]:
    """One readable issue per failing field of a pydantic error."""
    out = []
    for err in e.errors():
        kind, ctx = err["type"], err.get("ctx") or {}
        last = err["loc"][-1] if err["loc"] else ""
        expected = _EXPECTED.get(kind) or _FIELD_EXPECTED.get(str(last))
        if kind == "string_too_long":
            expected = f"at most {ctx.get('max_length')} characters"
        elif kind == "too_long":
            expected = f"at most {ctx.get('max_length')} items"
        elif kind == "literal_error":
            expected = f"one of {ctx.get('expected')}"
        received = None if kind in ("missing", "extra_forbidden") else err.get("input")
        out.append(
            issue(
                _path(err["loc"]),
                err["msg"].removeprefix("Value error, "),
                expected=expected,
                received=_safe_received(received),
            )
        )
    return out


def parse_brief(data: dict, now: datetime | None = None) -> CallBrief:
    """Validate a brief dict, raising one BriefError that lists every issue found."""
    try:
        return check_brief(CallBrief.model_validate(data), now)
    except ValidationError as e:
        found = validation_issues(e)
    if isinstance(data, dict):
        found += content_issues(data)
        # The window's own rules, when the window itself parsed.
        if isinstance(data.get("call_window"), dict) and not any(
            i["field"].startswith("brief.call_window") for i in found
        ):
            try:
                found += CallWindow.model_validate(data["call_window"]).issues(
                    now or datetime.now(UTC)
                )
            except ValidationError:
                pass
    _raise(found)
    raise AssertionError("unreachable: a ValidationError always has an issue")


def check_brief(brief: CallBrief, now: datetime | None = None) -> CallBrief:
    """Checks beyond the schema: content rules, numbers, the window. Collects them all."""
    found = content_issues(brief.model_dump(mode="json"))
    if brief.callback_number and brief.callback_number == brief.to_number:
        found.append(
            issue("brief.callback_number", "must differ from to_number", expected="another number")
        )
    if brief.call_window:
        if brief.callee_timezone and brief.callee_timezone != brief.call_window.timezone:
            found.append(
                issue(
                    "brief.callee_timezone",
                    "must match call_window.timezone",
                    expected=brief.call_window.timezone,
                    received=brief.callee_timezone,
                )
            )
        found += brief.call_window.issues(now or datetime.now(UTC))
    elif not brief.callee_timezone:
        found.append(
            issue(
                "brief.callee_timezone",
                "required (IANA zone of the business) for TCPA hours",
                expected="an IANA zone such as America/Chicago, or a call_window",
            )
        )
    _raise(found)
    return brief
