"""The post-call safety evaluation: call classes, shared items, a charge on file, aliases."""

from __future__ import annotations

from conftest import conversation

from onhold import report
from onhold.calls import UNTRUSTED_KEY, view


def row(**over) -> dict:
    """A finished call row, as the store returns it."""
    data = {
        "id": "c_0123456789abcdef",
        "callee_name": "Example Family Pharmacy",
        "topic": "a prescription refill",
        "reference": None,
        "created_at": 1_800_000_000,
        "status": "ended",
        "outcome": "goal_met",
        "reason": None,
        "mode": "now",
        "to_number": "+15125550123",
        "scheduled_for": None,
        "started_at": 1_800_000_060,
        "duration_secs": 240,
        "cost": 1200,
        "brief_json": {"callee_timezone": "America/Chicago"},
        "details_json": {},
        "evaluation_json": {},
    }
    data.update(over)
    return data


def collected(**values) -> dict:
    """A conversation whose data collection carries ``values``."""
    conv = conversation("c_0123456789abcdef")
    results = conv["analysis"]["data_collection_results"]
    results.update({k: {"value": v} for k, v in values.items()})
    return conv


# The report ------------------------------------------------------------------


def test_the_report_records_a_charge_on_file_and_what_was_shared():
    conv = collected(
        shared_items="name\ndate of birth\ncallback number",
        charge_on_file_amount="$15.00",
        charge_on_file_purpose="refill copay",
    )
    details = report.from_conversation(conv, ["Is it ready?"], 3600)["details_json"]
    assert details["shared_items"] == ["name", "date of birth", "callback number"]
    assert details["charge_on_file"] == {"amount": "$15.00", "purpose": "refill copay"}


def test_no_charge_and_nothing_shared_records_neither():
    conv = collected(shared_items="", charge_on_file_amount="none", charge_on_file_purpose="")
    details = report.from_conversation(conv, [], 3600)["details_json"]
    assert "shared_items" not in details and "charge_on_file" not in details


def test_shared_items_are_named_by_the_class_table():
    assert report.shared_kind("Date of birth") == "date of birth"
    assert report.shared_kind("callback number: 512-555-0142") == "callback number"
    assert report.shared_kind("insurance_member_id") == "insurance member ID"
    assert report.shared_kind("other: Social Security number") == "other"
    assert report.shared_kind("the colour of the car") == "other"


# evaluation_details ----------------------------------------------------------


def test_a_healthcare_call_reports_its_class_and_what_was_shared():
    call = row(
        brief_json={"call_class": "healthcare", "allow_charge_on_file": True},
        details_json={
            "shared_items": ["name", "Date of birth", "callback number", "date of birth"],
            "charge_on_file": {"amount": "$15.00", "purpose": "refill copay"},
        },
        evaluation_json={"shared_only_permitted_info": "success"},
    )
    d = view(call)["evaluation_details"]
    assert d["call_class"] == "healthcare"
    assert "date of birth" in d["class_may_share"]
    assert d["shared_items"] == ["name", "date of birth", "callback number"]
    assert d["charge_on_file"] == {"allowed": True, "amount": "$15.00", "purpose": "refill copay"}
    assert "attention" not in view(call)


def test_an_old_row_without_a_class_is_customer_support():
    d = view(row(brief_json={"callee_timezone": "America/Chicago"}))["evaluation_details"]
    assert d["call_class"] == "customer_support"
    assert "date of birth" not in d["class_may_share"]
    assert d["shared_items"] == []
    # No charge allowed and none agreed: nothing to say about one.
    assert "charge_on_file" not in d


def test_an_allowed_charge_with_none_agreed_says_so():
    d = view(row(brief_json={"allow_charge_on_file": True}))["evaluation_details"]
    assert d["charge_on_file"] == {"allowed": True, "amount": None, "purpose": None}


def test_a_charge_agreed_without_the_flag_needs_attention():
    call = row(
        details_json={"charge_on_file": {"amount": "$40", "purpose": "membership fee"}},
        evaluation_json={"no_commitments_or_payments": "failure"},
    )
    v = view(call)
    assert v["evaluation_details"]["charge_on_file"]["allowed"] is False
    assert "Safety check failed: no_commitments_or_payments." in v["attention"]
    assert "did not allow one" in v["attention"]


def test_long_digit_runs_never_reach_the_top_level():
    call = row(
        brief_json={"allow_charge_on_file": True},
        details_json={"charge_on_file": {"amount": "$15", "purpose": "card 4111 1111 1111 1111"}},
    )
    assert view(call)["evaluation_details"]["charge_on_file"]["purpose"] is None


def test_a_running_call_has_no_evaluation_details():
    assert "evaluation_details" not in view(row(status="dialing", outcome=None))


# The deprecated criterion id ---------------------------------------------------


def test_the_old_criterion_id_is_kept_as_an_alias():
    v = view(row(evaluation_json={"shared_only_permitted_info": "failure"}))
    assert v["evaluation"]["shared_only_permitted_info"] == "failure"
    assert v["evaluation"]["no_sensitive_info_shared"] == "failure"
    assert "shared_only_permitted_info" in v["deprecated_aliases"]["no_sensitive_info_shared"]
    # One failure, named once.
    assert v["attention"] == "Safety check failed: shared_only_permitted_info. Read the transcript."


def test_an_old_row_with_only_the_old_criterion_still_shows():
    v = view(row(evaluation_json={"no_sensitive_info_shared": "failure"}))
    assert v["evaluation"] == {"no_sensitive_info_shared": "failure"}
    assert "deprecated_aliases" not in v
    assert v["attention"] == "Safety check failed: no_sensitive_info_shared. Read the transcript."


def test_shared_items_stay_untrusted_in_the_details():
    v = view(row(details_json={"shared_items": ["other: something they said"]}))
    assert v[UNTRUSTED_KEY]["details"]["shared_items"] == ["other: something they said"]
    assert v["evaluation_details"]["shared_items"] == ["other"]
