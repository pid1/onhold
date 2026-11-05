"""Phone-tree memory: learning the keypad path that reached a person, and offering it."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import brief, conversation

from onhold import report
from onhold.brief import NO_MENU_PATH, BriefError
from onhold.calls import UNTRUSTED_KEY, CallError
from onhold.store import MIGRATIONS, SqliteStore

pytestmark = pytest.mark.anyio

OUTBOUND = "/v1/convai/twilio/outbound-call"
NUMBER = "+15125550123"
PATH = [
    {"press": "2", "option": "Navigating to vehicle registration"},
    {"press": "0", "option": "Asking for a person"},
]


def press(tones: str, reason: str = "", request_id: str = "r1", called: bool = True) -> dict:
    return {
        "request_id": request_id,
        "tool_name": "play_keypad_touch_tone",
        "params_as_json": json.dumps({"dtmf_tones": tones, "reason": reason}),
        "tool_has_been_called": called,
        "type": "system",
    }


def menu_transcript(*calls: dict, results: list[dict] | None = None) -> list[dict]:
    return [
        {"role": "agent", "message": "Hi, I'm an AI assistant...", "time_in_call_secs": 0},
        {"role": "user", "message": "Press 2 for registration.", "time_in_call_secs": 3},
        {
            "role": "agent",
            "message": "",
            "time_in_call_secs": 5,
            "tool_calls": list(calls),
            "tool_results": results or [],
        },
        {"role": "user", "message": "Tax office, how can I help?", "time_in_call_secs": 30},
    ]


def through_menu(call_id: str, path=PATH, outcome="goal_met", reason="none", **over) -> dict:
    conv = conversation(
        call_id,
        transcript=menu_transcript(
            *(press(s["press"], s["option"], f"r{i}") for i, s in enumerate(path))
        ),
        **over,
    )
    conv["analysis"]["data_collection_results"]["outcome"] = {"value": outcome}
    conv["analysis"]["data_collection_results"]["not_reached_reason"] = {"value": reason}
    return conv


async def call_through(service, api, conv_builder, conv_id="conv_1", **b) -> str:
    d = await service.draft_call(brief(**b))
    api.route("POST", OUTBOUND, {"success": True, "conversation_id": conv_id, "callSid": "CA1"})
    call_id = (await service.place_call(d["draft_id"], True))["call_id"]
    await service.ingest_webhook(
        {"type": "post_call_transcription", "data": conv_builder(call_id, conv_id=conv_id)}
    )
    return call_id


# Reading the presses ---------------------------------------------------------


def test_presses_come_from_the_tool_calls_in_order():
    conv = {"transcript": menu_transcript(press("2", "billing", "a"), press("1w#", "", "b"))}
    assert report.menu_presses(conv) == [
        {"press": "2", "option": "billing"},
        {"press": "1#", "option": ""},
    ]


def test_a_failed_or_uncalled_press_is_left_out():
    conv = {
        "transcript": menu_transcript(
            press("2", "billing", "a"),
            press("3", "", "b"),
            press("4", "", "c", called=False),
            results=[{"request_id": "b", "tool_name": "play_keypad_touch_tone", "is_error": True}],
        )
    }
    assert [s["press"] for s in report.menu_presses(conv)] == ["2"]


def test_no_presses_is_an_empty_path():
    assert report.menu_presses(conversation("c_x")) == []


@pytest.mark.parametrize("tones", ["12345", "123456789", "", "2a"])
def test_a_press_that_cannot_be_a_menu_choice_learns_nothing(tones):
    # Five or more digits could be an extension, an ID or an account number.
    conv = {"transcript": menu_transcript(press("2", "", "a"), press(tones, "", "b"))}
    assert report.menu_presses(conv) is None


def test_too_many_presses_learn_nothing():
    conv = {"transcript": menu_transcript(*(press("1", "", f"r{i}") for i in range(11)))}
    assert report.menu_presses(conv) is None


def test_an_option_note_never_keeps_a_long_number():
    conv = {"transcript": menu_transcript(press("2", "Entering account 5125550199"))}
    assert report.menu_presses(conv) == [{"press": "2", "option": ""}]


# Learning --------------------------------------------------------------------


async def test_a_path_that_reached_a_person_is_saved(service, api, store):
    call_id = await call_through(service, api, through_menu)
    [row] = await store.menu_paths(NUMBER)
    assert row["digits"] == "2,0" and row["steps_json"] == PATH
    assert row["worked_count"] == 1 and row["last_call_id"] == call_id
    assert row["topic"] == "a licence plate renewal"
    await call_through(service, api, through_menu, conv_id="conv_2")
    [row] = await store.menu_paths(NUMBER)
    assert row["worked_count"] == 2


async def test_nothing_is_saved_when_nobody_was_reached(service, api, store):
    def unreached(call_id, conv_id):
        conv = through_menu(call_id, outcome="not_reached", reason="hold_timeout", conv_id=conv_id)
        conv["transcript"] = conv["transcript"][:3]  # nobody spoke after the menu
        return conv

    await call_through(service, api, unreached)
    assert await store.menu_paths(NUMBER) == []


async def test_a_call_with_no_menu_saves_nothing(service, api, store):
    await call_through(service, api, lambda c, conv_id: conversation(c, conv_id))
    assert await store.menu_paths(NUMBER) == []


# Offering it in the brief ----------------------------------------------------


async def test_a_draft_points_out_a_saved_path(service, api):
    await call_through(service, api, through_menu)
    d = await service.draft_call(brief())
    assert "menu_path_hint" in d
    [saved] = d[UNTRUSTED_KEY]["saved_menu_paths"]
    assert saved["status"] == "works" and saved["menu_path"] == PATH
    assert d["preview"]["dynamic_variables"]["menu_path"] == NO_MENU_PATH


async def test_a_brief_with_a_menu_path_gives_it_to_the_agent(service, api):
    d = await service.draft_call(brief(menu_path=PATH))
    assert d["preview"]["dynamic_variables"]["menu_path"] == (
        "1. Press 2 (Navigating to vehicle registration)\n2. Press 0 (Asking for a person)"
    )
    assert "menu_path_hint" not in d and UNTRUSTED_KEY not in d


async def test_a_number_with_no_saved_path_has_no_hint(service):
    d = await service.draft_call(brief())
    assert "menu_path_hint" not in d


@pytest.mark.parametrize(
    "step",
    [{"press": "12345"}, {"press": "2", "option": "account 5125550199"}, {"press": "2", "x": 1}],
)
async def test_a_bad_menu_step_is_refused(service, step):
    with pytest.raises(BriefError):
        await service.draft_call(brief(menu_path=[step]))


# Going stale -----------------------------------------------------------------


async def test_a_saved_path_that_dead_ends_goes_stale(service, api, store):
    await call_through(service, api, through_menu)

    def dead_end(call_id, conv_id):
        conv = through_menu(
            call_id, outcome="not_reached", reason="phone_tree_dead_end", conv_id=conv_id
        )
        conv["transcript"] = conv["transcript"][:3]
        return conv

    await call_through(service, api, dead_end, conv_id="conv_2", menu_path=PATH)
    [row] = (await service.get_menu_paths(NUMBER))[UNTRUSTED_KEY]["menu_paths"]
    assert row["status"] == "stale" and row["failed"] == 1
    # A stale path is not offered on a draft.
    assert "menu_path_hint" not in await service.draft_call(brief())


async def test_a_changed_menu_saves_the_new_path_and_marks_the_old_one(service, api, store):
    await call_through(service, api, through_menu)
    new = [{"press": "3", "option": "registration"}]
    await call_through(
        service,
        api,
        lambda c, conv_id: through_menu(c, path=new, conv_id=conv_id),
        conv_id="conv_2",
        menu_path=PATH,
    )
    rows = {
        r["presses"]: r for r in (await service.get_menu_paths(NUMBER))[UNTRUSTED_KEY]["menu_paths"]
    }
    assert rows["3"]["status"] == "works"
    assert rows["2,0"]["status"] == "stale"


async def test_a_path_that_worked_again_is_fresh(service, api, store):
    await call_through(service, api, through_menu, menu_path=PATH)
    await store.menu_path_failed(NUMBER, "2,0", 1)  # an old failure
    [row] = (await service.get_menu_paths(NUMBER))[UNTRUSTED_KEY]["menu_paths"]
    assert row["status"] == "works"


async def test_a_learning_error_still_records_the_report(service, api, store, monkeypatch):
    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "menu_path_worked", broken)
    call_id = await call_through(service, api, through_menu)
    assert (await store.get_call(call_id))["outcome"] == "goal_met"


# Reading and deleting --------------------------------------------------------


async def test_get_menu_paths_checks_the_number(service):
    with pytest.raises(BriefError):
        await service.get_menu_paths("5125550123")


async def test_delete_needs_confirm_and_deletes_one_or_all(service, api, store):
    await call_through(service, api, through_menu)
    await store.menu_path_worked(NUMBER, [{"press": "9", "option": ""}], "x", "c_y", 2)
    with pytest.raises(CallError):
        await service.delete_menu_paths(NUMBER, "2,0", confirm=False)
    assert (await service.delete_menu_paths(NUMBER, "2,0", confirm=True))["deleted"] == 1
    assert [r["digits"] for r in await store.menu_paths(NUMBER)] == ["9"]
    assert (await service.delete_menu_paths(NUMBER, None, confirm=True))["deleted"] == 1
    assert await store.menu_paths(NUMBER) == []


async def test_deleting_a_call_keeps_what_it_learned(service, api, store):
    call_id = await call_through(service, api, through_menu)
    await service.delete_call(call_id, confirm=True)
    assert len(await store.menu_paths(NUMBER)) == 1


# Migrations ------------------------------------------------------------------


def test_an_existing_database_gets_only_the_new_migration(tmp_path):
    path = tmp_path / "onhold.db"
    conn = sqlite3.connect(path)
    conn.executescript((MIGRATIONS / "0001_init.sql").read_text())
    conn.execute("INSERT INTO drafts VALUES ('d_1', 1, 2, '{}', '{}', NULL, NULL)")
    conn.commit()
    conn.close()
    s = SqliteStore(str(path))
    s.migrate()
    s.migrate()  # running again changes nothing
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"calls", "drafts", "menu_paths"} <= tables
    assert s.conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
