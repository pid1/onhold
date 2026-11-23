"""The call classes are one table, shared by the brief, the guide, the prompt and the evaluation."""

from typing import get_args

import pytest

from onhold import classes
from onhold.brief import CATEGORY_PATTERNS, CallBrief, item_categories


def test_the_class_literal_matches_the_table():
    assert get_args(classes.CallClass) == classes.CALL_CLASSES
    assert set(classes.CLASS_CATEGORIES) == set(classes.CALL_CLASSES)
    assert set(classes.CLASS_EXAMPLES) == set(classes.CALL_CLASSES)
    assert classes.DEFAULT_CLASS == "customer_support"


def test_every_class_uses_known_categories():
    for cats in classes.CLASS_CATEGORIES.values():
        assert set(cats) <= set(classes.CATEGORIES)
        # Every v1 class may give the owner's name, a callback number and an address.
        assert {"name", "callback_number", "address"} <= set(cats)


def test_the_v1_table():
    assert "date_of_birth" in classes.CLASS_CATEGORIES["healthcare"]
    assert "date_of_birth" in classes.CLASS_CATEGORIES["government"]
    assert "date_of_birth" not in classes.CLASS_CATEGORIES["customer_support"]
    assert "license_plate" in classes.CLASS_CATEGORIES["government"]
    assert "medication" in classes.CLASS_CATEGORIES["healthcare"]


def test_describe_and_shareable_text():
    assert classes.shareable_text("customer_support") == (
        "name, callback number, address, customer account number, order number"
    )
    assert classes.describe("government").startswith("government (DMV")
    assert classes.shareable_text("government") in classes.describe("government")


def test_social_security_and_payment_details_are_always_forbidden():
    text = " ".join(classes.ALWAYS_FORBIDDEN)
    assert "Social Security" in text
    assert "card" in text and "routing" in text


def test_the_brief_field_is_described_from_the_table():
    description = CallBrief.model_fields["call_class"].description
    for c in classes.CALL_CLASSES:
        assert classes.describe(c) in description


def test_every_category_but_name_can_be_recognised():
    assert set(CATEGORY_PATTERNS) == set(classes.CATEGORIES) - {"name"}


@pytest.mark.parametrize(
    ("item", "category"),
    [
        ("Date of birth: March 3, 1980", "date_of_birth"),
        ("DOB 1980-03-03", "date_of_birth"),
        ("Callback number 512-555-0142", "callback_number"),
        ("512-555-0142", "callback_number"),
        ("Mailing address: 100 Example Street", "address"),
        ("100 Example Street, Austin", "address"),
        ("Prescriber: Dr. Example", "prescriber"),
        ("Pharmacy: Example Drug on Main", "pharmacy"),
        ("Takes example-pril 10 mg daily", "medication"),
        ("Insurance member ID W123456789", "insurance_member_id"),
        ("Plate number ABC 1234", "license_plate"),
        ("Permit number 2026-0012345", "case_number"),
        ("Account number 1234 5678", "account_number"),
        ("Order #112-1234567", "order_number"),
    ],
)
def test_item_categories(item, category):
    assert category in item_categories(item)


def test_plain_items_have_no_category():
    assert item_categories("The car is a passenger vehicle registered in the county.") == []
    assert item_categories("in order to renew") == []
