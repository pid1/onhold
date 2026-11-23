"""Answers: the ``qa`` data collection, matched to the brief's questions."""

from __future__ import annotations

from conftest import conversation

from onhold import report

QUESTIONS = [
    "Is the Zentrovil refill ready for pickup?",
    "What is the copay for the refill?",
    "What are your hours on Saturday?",
]


def collected(**values) -> dict:
    """A conversation whose data collection has ``values`` and no ``answer_N``."""
    conv = conversation("c_0123456789abcdef")
    results = conv["analysis"]["data_collection_results"]
    for k in [k for k in results if k.startswith("answer_")]:
        del results[k]
    results.update({k: {"value": v} for k, v in values.items()})
    return conv


def test_parse_qa_reads_each_pair_and_skips_other_lines():
    text = (
        "Q: Is the refill ready? || A: Yes, since this morning.\n"
        "Here is what was asked:\n"
        "- Q: How much is it? || A: not answered\n"
        "2. q: Open Saturday? ||A: Not answered.\n"
    )
    assert report.parse_qa(text) == [
        {"question": "Is the refill ready?", "answer": "Yes, since this morning."},
        {"question": "How much is it?", "answer": None},
        {"question": "Open Saturday?", "answer": None},
    ]
    assert report.parse_qa("") == [] and report.parse_qa(None) == []


def test_answers_are_matched_to_reworded_questions():
    qa = (
        "Q: Could you tell me if Test Owner's Zentrovil prescription refills are ready to pick"
        " up? || A: Yes, it's ready at the counter.\n"
        "Q: How much will the co-pay be for that refill? || A: $15.00\n"
        "Q: And what are your Saturday hours? || A: not answered\n"
    )
    fields = report.from_conversation(collected(qa=qa), QUESTIONS, 3600)
    assert fields["answers_json"] == [
        {"question": QUESTIONS[0], "answered": True, "answer": "Yes, it's ready at the counter."},
        {"question": QUESTIONS[1], "answered": True, "answer": "$15.00"},
        {"question": QUESTIONS[2], "answered": False, "answer": None},
    ]
    assert "other_questions" not in fields["details_json"]
    assert fields["outcome"] == "goal_met"
    assert "goal met. 2 of 3 answered." in report.headline(fields | {"callee_name": "X"})


def test_unmatched_pairs_go_to_other_questions():
    qa = (
        "Q: What's the copay on the refill? || A: Fifteen dollars.\n"
        "Q: Do you need the date of birth on the account? || A: No, the name is enough.\n"
        "Q: Can I get a reference number for this? || A: not answered\n"
    )
    fields = report.from_conversation(collected(qa=qa), QUESTIONS, 3600)
    answered = [a["answered"] for a in fields["answers_json"]]
    assert answered == [False, True, False]
    assert fields["details_json"]["other_questions"] == [
        {
            "question": "Do you need the date of birth on the account?",
            "answer": "No, the name is enough.",
        },
        {"question": "Can I get a reference number for this?", "answer": None},
    ]


def test_each_pair_matches_one_question_and_a_repeat_keeps_the_answer():
    qa = (
        "Q: What's the copay for the refill? || A: not answered\n"
        "Q: Sorry, what was the copay for the refill? || A: $15.00\n"
    )
    answers, other = report.match_answers(QUESTIONS[1:2], report.parse_qa(qa))
    assert answers == [{"question": QUESTIONS[1], "answered": True, "answer": "$15.00"}]
    assert other == [{"question": "What's the copay for the refill?", "answer": None}]


def test_unrelated_questions_do_not_match():
    assert report.similarity("What are your hours on Saturday?", "Is the refill ready?") == 0
    # One shared word of two is not enough to call them the same question.
    assert report.similarity("Is my refill ready?", "How much does the refill cost?") < (
        report.MATCH_THRESHOLD
    )
    assert report.similarity("Can it be renewed by mail?", "Can I renew it by mail?") >= (
        report.MATCH_THRESHOLD
    )


def test_an_old_answer_n_payload_still_works():
    conv = conversation("c_0123456789abcdef")  # answer_1 and answer_2, no qa
    fields = report.from_conversation(
        conv, ["Can it be renewed by mail?", "What is the fee?"], 3600
    )
    assert fields["answers_json"] == [
        {"question": "Can it be renewed by mail?", "answered": True, "answer": "Yes, by mail."},
        {"question": "What is the fee?", "answered": True, "answer": "$51.75"},
    ]


def test_qa_fills_an_answer_the_old_field_left_empty():
    conv = collected(
        answer_1="not answered", qa="Q: Is my Zentrovil refill ready to pick up? || A: Yes."
    )
    fields = report.from_conversation(conv, QUESTIONS[:1], 3600)
    assert fields["answers_json"][0]["answer"] == "Yes."


def test_empty_qa_with_questions_is_not_goal_met():
    fields = report.from_conversation(collected(qa=""), QUESTIONS, 3600)
    assert fields["outcome"] == "partial"
    assert "none of the 3 questions was answered" in fields["details_json"]["checks"][0]
