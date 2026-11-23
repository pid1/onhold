"""Call classifications: which kinds of personal data a call may share.

Each call has one class, picked in the brief (``call_class``). The class
decides which categories of fact the agent may share when asked, if the item
is also in the brief's ``may_share``. The lists are fixed here, not per call.
This module is the one source for the brief's field description, the brief
guide, the voice agent's prompt variables and the post-call evaluation, so
they cannot drift apart.
"""

from __future__ import annotations

from typing import Literal

CallClass = Literal["healthcare", "government", "customer_support"]
CALL_CLASSES: tuple[str, ...] = ("healthcare", "government", "customer_support")
DEFAULT_CLASS = "customer_support"

# Category id -> how the agent and the evaluator read it.
CATEGORIES: dict[str, str] = {
    "name": "name",
    "date_of_birth": "date of birth",
    "callback_number": "callback number",
    "address": "address",
    "prescriber": "prescriber",
    "pharmacy": "pharmacy",
    "medication": "medication and dose",
    "insurance_member_id": "insurance member ID",
    "license_plate": "license plate number",
    "case_number": "case or permit number",
    "account_number": "customer account number",
    "order_number": "order number",
}

CLASS_CATEGORIES: dict[str, tuple[str, ...]] = {
    "healthcare": (
        "name",
        "date_of_birth",
        "callback_number",
        "address",
        "prescriber",
        "pharmacy",
        "medication",
        "insurance_member_id",
    ),
    "government": (
        "name",
        "date_of_birth",
        "callback_number",
        "address",
        "license_plate",
        "case_number",
    ),
    "customer_support": (
        "name",
        "callback_number",
        "address",
        "account_number",
        "order_number",
    ),
}

CLASS_EXAMPLES: dict[str, str] = {
    "healthcare": "doctor's office, pharmacy, lab, dentist",
    "government": "DMV, tax office, county clerk",
    "customer_support": "any company: retailers, ISPs, utilities, service providers",
}

# Blocked in every class. Asking for an SSN ends that part of the call with
# outcome needs_owner; payment credentials never reach the agent at all.
ALWAYS_FORBIDDEN: tuple[str, ...] = (
    "Any Social Security number",
    "Any full credit or debit card number, card security code, or bank account or routing number",
    "Adding, removing, changing or reading out any card or bank account",
    "Any calendar details, schedules or whereabouts of anyone",
)


def shareable(call_class: str) -> list[str]:
    """Readable names of the categories a class may share, in order."""
    return [CATEGORIES[c] for c in CLASS_CATEGORIES[call_class]]


def shareable_text(call_class: str) -> str:
    """'name, date of birth, callback number, ...' for prompts and previews."""
    return ", ".join(shareable(call_class))


def describe(call_class: str) -> str:
    """One line per class, for field descriptions and server instructions."""
    return f"{call_class} ({CLASS_EXAMPLES[call_class]}): may share {shareable_text(call_class)}"
