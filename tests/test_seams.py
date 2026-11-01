"""The joins between tested pieces, which is where every live bug has been.

Five bugs reached the family in a row, none of them caught by 361 passing
tests, and they share one shape: each component worked, each branch was
touched at least once, and the failure was in a COMBINATION nothing exercised.

  - a child tap then a DELETE crashed; a child tap then a create was fine
  - a screenshot then a child question lost the event; typed text did not
  - ✏️ then a reply mis-parsed; ✏️ on its own was covered ten times over
  - pending state round-tripped through SQLite wrong for one key only
  - the clash lookup hit the real API; the caller swallowed the error

Per-branch tests cannot find these, because each branch passes. What is needed
is the product: every button against every action it can lead to, every
pending kind through a real encode/decode, every generated button against the
handler that must exist for it. That is what this file is - deliberately
shallow assertions (did it crash, did it answer) over a wide grid, which is
the opposite trade-off from the rest of the suite and the reason it catches
what the rest misses.
"""
import asyncio
import datetime
import inspect
import logging
import os
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import db
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
MONDAY = datetime.date(2026, 9, 7)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _at(hour, minute=0, date=MONDAY):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


EVENT = {
    "id": "ev1",
    "summary": "Child_2 10am Piano",
    "start": {"dateTime": _at(9).isoformat()},
    "end": {"dateTime": _at(10).isoformat()},
}


# --------------------------------------------------------------------------
# 1. Every button, against every action it can lead to.
#
# kid: and cat: taps re-enter _dispatch, so the object arriving there is not
# an Update. Each (button, action) pair below was reachable in production;
# only the create ones had tests, and delete was broken for both.
# --------------------------------------------------------------------------

def _callback_query():
    """Shaped like the real thing: a CallbackQuery has .from_user and
    .message, and NO .effective_user. Deleting it matters - MagicMock would
    otherwise invent the attribute and the test would pass against the exact
    bug this is here to catch."""
    query = MagicMock()
    query.data = "kid:0"
    query.from_user.id = ADMIN_ID
    query.message.chat.id = 1
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    del query.effective_user
    del query.effective_chat
    return query


RESULTS = {
    "create": {"action": "create", "status": "ok", "title": "Child_2 10am Piano",
               "all_day": False, "location": None, "notes": None,
               "calendar_key": "child_2_enrichment",
               "start": _at(9), "end": _at(10), "date": None,
               "occurrences": [{"all_day": False, "start": _at(9), "end": _at(10), "date": None}]},
    "delete": {"action": "delete", "status": "ok", "search_text": "piano",
               "prefer_date": None, "calendar_key": ["child_2_enrichment"]},
    "amend": {"action": "amend", "status": "ok", "search_text": "piano",
              "prefer_date": None, "calendar_key": ["child_2_enrichment"],
              "changes": {"end_time": "18:00"}},
    "query": {"action": "query", "status": "ok", "start": MONDAY, "end": MONDAY,
              "calendar_key": ["child_2_enrichment"]},
    "unknown": {"action": "unknown", "status": "ok"},
    "clarify_category": {"action": "create", "status": "needs_clarification",
                         "question": "Which calendar?", "clarify": "category",
                         "categories": ["school", "enrichment", "others"],
                         "source_text": "piano", "raw": {"title": "piano"}},
    # Clarifications chain: a screenshot asks which child, and the answer to
    # that can still leave the category open. So a question can be asked from
    # a button tap, not only from a typed message.
    "clarify_child": {"action": "create", "status": "needs_clarification",
                      "question": "Child_1 or Child_2?", "clarify": "child",
                      "children": ["Child_1", "Child_2"],
                      "source_text": "piano", "raw": {"title": "piano"}},
    "clarify_plain": {"action": "create", "status": "needs_clarification",
                      "question": "When should that be?", "source_text": "piano"},
}


@pytest.fixture
def wired(monkeypatch):
    """Everything external stubbed; the routing itself stays real."""
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main, "_note_friction", AsyncMock())
    monkeypatch.setattr(ct, "find_conflicts_for_child", lambda *a, **k: [])
    monkeypatch.setattr(ct, "find_conflicts_for_child_over", lambda *a, **k: {})
    monkeypatch.setattr(ct, "list_events_range_across", lambda *a, **k: [])
    monkeypatch.setattr(ct, "list_events_range_all", lambda *a, **k: [("Child_2 — Enrichment", EVENT)])
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([("child_2_enrichment", EVENT)], False)))


@pytest.mark.parametrize("action", sorted(RESULTS))
def test_every_action_survives_arriving_from_a_button(six_calendars, wired, action):
    """The product that was missing. Shallow on purpose: it asserts the bot
    ANSWERED rather than what it said, because the failure mode being hunted
    is an unhandled exception, not a wording change."""
    query = _callback_query()
    update = main._QueryAsUpdate(query)

    asyncio.run(main._dispatch(update, 1, ADMIN_ID, dict(RESULTS[action]), "piano"))

    assert query.message.reply_text.await_count or query.edit_message_text.await_count, (
        f"{action} arriving from a button produced no reply at all"
    )


@pytest.mark.parametrize("action", sorted(RESULTS))
def test_every_action_still_works_from_a_message(six_calendars, wired, action):
    """The other half of the product - the path that always worked must keep
    working, since the fix for the button path touched shared code."""
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text

    asyncio.run(main._dispatch(update, 1, ADMIN_ID, dict(RESULTS[action]), "piano"))

    assert update.message.reply_text.await_count


def test_the_wrapper_supplies_what_a_bare_query_lacks():
    """Names the actual defect: a CallbackQuery has no .effective_user, and
    half of _dispatch's branches read it."""
    query = _callback_query()
    with pytest.raises(AttributeError):
        query.effective_user

    wrapped = main._QueryAsUpdate(query)
    assert wrapped.effective_user.id == ADMIN_ID
    assert wrapped.effective_chat.id == 1
    assert wrapped.message is query.message
    # Kept on purpose: the confirm cards check for it to decide whether to
    # rewrite the tapped card in place instead of sending a new one.
    assert wrapped.edit_message_text is query.edit_message_text


@pytest.mark.parametrize("data,kind,payload", [
    ("kid:0", "child", {"raw": {"title": "piano"}, "source_text": "piano", "children": ["Child_1", "Child_2"]}),
    ("cat:0", "category", {"raw": {"title": "piano"}, "source_text": "piano",
                           "categories": ["school", "enrichment", "others"]}),
])
def test_the_button_handlers_wrap_before_dispatching(six_calendars, monkeypatch, data, kind, payload):
    """The guard on the wiring, not the wrapper. The tests above build a
    _QueryAsUpdate by hand, so they would all still pass if handle_callback
    went back to handing _dispatch a bare CallbackQuery - which is precisely
    the bug. This pins the call sites."""
    seen = {}
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.db, "pop_pending", lambda cid, k: dict(payload))
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.event_parser, "resume_with_child", lambda *a: dict(RESULTS["create"]))
    monkeypatch.setattr(main.event_parser, "resume_with_category", lambda *a: dict(RESULTS["create"]),
                        raising=False)

    async def spy(update, *args):
        seen["type"] = type(update)

    monkeypatch.setattr(main, "_dispatch", spy)

    query = _callback_query()
    query.data = data
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 1
    update.effective_user.id = ADMIN_ID

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert seen.get("type") is main._QueryAsUpdate, (
        f"{data} handed _dispatch a {seen.get('type')}, not the wrapper"
    )


# --------------------------------------------------------------------------
# 2. Every button the bot can emit has a handler.
# --------------------------------------------------------------------------

def test_every_button_produced_has_somewhere_to_land():
    """A card offering a button nothing handles is a dead end that no unit
    test would notice - the button renders fine and the tap does nothing."""
    source = inspect.getsource(main)
    produced = {
        m.split(":")[0].strip('"')
        for m in re.findall(r'callback_data=f?"([^"]*)"', source)
    }
    handled = set(re.findall(r'data == "([a-z_]+)"', source))
    handled |= set(re.findall(r'data\.startswith\("([a-z_]+):"\)', source))

    assert produced <= handled, f"buttons with no handler: {sorted(produced - handled)}"


# --------------------------------------------------------------------------
# 3. Every pending kind survives a real SQLite round-trip.
#
# §23: db._decode turned raw["date"] from a string into a date, and the parser
# then called .strip() on it. One key, one kind, and the tap died.
# --------------------------------------------------------------------------

PENDING = {
    "clarification": {"text": "piano at 5pm", "kind": "edit", "card": "create"},
    "child": {"raw": {"title": "piano", "date": "2026-09-07", "start_time": "17:00"},
              "source_text": "", "children": ["Child_1", "Child_2"]},
    "category": {"raw": {"title": "piano", "date": "2026-09-07"},
                 "source_text": "piano", "categories": ["school", "enrichment", "others"]},
    "confirm": {"kind": "create", "title": "Child_2 10am Piano", "calendar_key": "child_2_enrichment",
                "start": _at(9), "end": _at(10), "date": None, "all_day": False,
                "location": None, "notes": None, "pickup": None, "pickup_clash": None,
                "source_text": "piano", "occurrences": [{"start": _at(9), "end": _at(10),
                                                         "date": None, "all_day": False}]},
    "create_options": {"options": [{"start": _at(9), "end": _at(10)}], "title": "Piano"},
    "amend_pick": {"changes": {"end_time": "18:00"}, "source_text": "piano",
                   "keys_by_event": {"ev1": "child_2_enrichment"}},
}


@pytest.mark.parametrize("kind", sorted(PENDING))
def test_pending_state_round_trips_unchanged(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    payload = PENDING[kind]

    db.save_pending(1, kind, payload)
    restored = db.pop_pending(1, kind)

    assert restored == payload, f"{kind} did not survive SQLite intact"


def test_a_models_raw_output_is_never_reinterpreted(tmp_path, monkeypatch):
    """The §23 bug specifically. `raw` is the model's own words and the parser
    re-reads them as strings; helpfully converting a date here crashed the
    child tap. Pinned separately because the round-trip above would pass even
    if raw were re-encoded consistently in both directions."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    raw = {"title": "piano", "date": "2026-09-07", "start_time": "17:00", "end_time": None}

    db.save_pending(1, "child", {"raw": raw, "source_text": "", "children": ["Child_1"]})
    restored = db.pop_pending(1, "child")

    assert restored["raw"] == raw
    assert isinstance(restored["raw"]["date"], str)


# --------------------------------------------------------------------------
# 4. A name is never silently read as the wrong child.
#
# Substring matching in both directions read "Bchild_2" as Child_2, because "child_2"
# is inside "bchild_2" - so a message about a child who does not exist was filed
# against one who does, without a question.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("typed,expected", [
    ("Child_2", "child_2"),
    ("child_2", "child_2"),
    ("Child_2's", "child_2"),          # the model usually strips this, but not always
    ("Child_2 Tan", "child_2"),        # a word of a longer name
    ("Child_1", "child_1"),
])
def test_a_name_that_means_this_child_resolves(six_calendars, typed, expected):
    assert ct.resolve_child_name(typed) == expected


@pytest.mark.parametrize("typed", ["Bchild_2", "Bobby", "Cinderchild_2", "Child_1a Smith", "x"])
def test_a_name_that_is_not_this_child_is_refused(six_calendars, typed):
    """Refused, not guessed. "Cinderchild_2" ends in "child_2" and "Child_1a Smith"
    starts with "child_1"; both used to resolve, and the bot would then act on
    the wrong child's calendar without asking."""
    assert ct.resolve_child_name(typed) is None


def test_an_unrecognised_name_asks_rather_than_searching_everyone(six_calendars):
    """The deterministic half of the fix. The prompt now also tells the model
    to pass unknown names through instead of dropping them - which it did
    inconsistently, so "delete Bobby's piano" asked on two runs out of three
    and silently searched all six calendars on the third."""
    import event_parser as ep

    result = ep._finalize({"action": "delete", "title": "piano", "child": "Bobby"}, "src")
    assert result["status"] == "needs_clarification"
    assert "Bobby" in result["question"]


# --------------------------------------------------------------------------
# 5. No test may reach the network.
#
# §27: adding the clash lookup made the suite authenticate with the live token
# and 404 on every create test, and it still went green, because the caller
# swallows errors on purpose. Runtime doubling was the only symptom.
# --------------------------------------------------------------------------

def test_the_network_guard_is_actually_armed():
    """conftest's autouse fixture is the only thing standing between the suite
    and the family's real calendar. If it silently stopped applying, tests
    would start passing for the wrong reason again."""
    with pytest.raises(AssertionError, match="real Google Calendar API"):
        ct._service()


# --------------------------------------------------------------------------
# 6. A full disk says so.
#
# 2026-08-09: the VM's disk filled, SQLite refused to write, and since every
# message starts by logging a usage row, the family got "Something went wrong
# on my end - please try again" for everything. Retrying could never work.
# --------------------------------------------------------------------------

import sqlite3 as _sqlite3


def test_a_full_disk_gets_its_own_message():
    reply = main._error_reply_for(_sqlite3.OperationalError("database or disk is full"))
    assert "disk space" in reply
    assert "won't help" in reply


def test_an_unrecognised_failure_says_so_and_carries_a_reference():
    """Still honest when the cause is unknown - but with a reference, so a
    report from the family can be matched to the traceback in the log.
    (errors.py covers every recognised kind; tests/test_errors.py.)"""
    for exc in (ValueError("boom"), _sqlite3.OperationalError("no such table: x")):
        reply = main._error_reply_for(exc, ref="K3F9Q")
        assert "don't recognise" in reply
        assert "Ref: K3F9Q" in reply


def test_polling_chatter_is_not_logged_at_info():
    """An idle bot long-polls every ~10s. At INFO, httpx wrote a line per
    poll - about 8,600 a day saying nothing happened, which is what filled
    the disk that then broke every message."""
    for noisy in ("httpx", "httpcore"):
        assert logging.getLogger(noisy).level >= logging.WARNING, f"{noisy} still logs at INFO"


def test_a_name_containing_an_underscore_stays_one_word(six_calendars, monkeypatch):
    """Splitting on '_' turned "kid_1's" into "kid" and "1" and stopped it
    resolving. Names with an underscore are rare, but they must still match."""
    monkeypatch.setattr(ct, "_calendars",
                        lambda: {"kid_1_school": {"child": "kid_1", "category": "school"}})
    assert ct.resolve_child_name("kid_1's") == "kid_1"
    assert ct.resolve_child_name("Kid_1 Tan") == "kid_1"
