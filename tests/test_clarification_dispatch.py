"""What happens AFTER a clarification button is tapped.

`_dispatch` is entered from two different places with two different objects:
an `Update` on the message path, and a `CallbackQuery` when a child/category
button re-enters it. They are not interchangeable - `.effective_user` exists
only on Update - so every branch of `_dispatch` has to be exercised from the
button side as well, which is what these do.

That difference is not theoretical: it is why "Delete piano appointment" ->
tap "Child_2" answered "Something went wrong on my end". The create branch was
written for both callers and survived; delete and amend were not.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

SATURDAY = datetime.date(2026, 8, 8)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main, "_note_friction", AsyncMock())


def _at(hour, minute=0, date=SATURDAY):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


PIANO = {
    "id": "ev1",
    "summary": "Child_2 10am Piano",
    "start": {"dateTime": _at(9).isoformat()},
    "end": {"dateTime": _at(10).isoformat()},
}


def _callback_query():
    """A stand-in for a real CallbackQuery: it has `.message`, and crucially
    it does NOT have `.effective_user`. Without the delete, MagicMock would
    invent that attribute and the test would pass against broken code."""
    query = MagicMock()
    query.message.reply_text = AsyncMock()
    query.edit_message_text = AsyncMock()
    del query.effective_user
    del query.effective_chat
    return query


def _dispatch(query, result):
    asyncio.run(main._dispatch(query, 1, ADMIN_ID, result, "delete piano appointment"))


# ----- the reported crash -----

def test_delete_survives_arriving_from_a_child_button(six_calendars, as_admin, monkeypatch):
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([("child_2_enrichment", PIANO)], False)))
    query = _callback_query()

    _dispatch(query, {"action": "delete", "status": "ok", "search_text": "piano",
                      "calendar_key": "child_2_enrichment"})

    text = query.message.reply_text.await_args.args[0]
    assert "piano" in text.lower() or "delete" in text.lower()
    buttons = query.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert any("Confirm Delete" in b[0].text for b in buttons)


def test_amend_survives_arriving_from_a_child_button(six_calendars, as_admin, monkeypatch):
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([("child_2_enrichment", PIANO)], False)))
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    query = _callback_query()

    _dispatch(query, {"action": "amend", "status": "ok", "search_text": "piano",
                      "calendar_key": "child_2_enrichment", "changes": {"start_time": "18:00"}})

    assert query.edit_message_text.await_count or query.message.reply_text.await_count


def test_a_no_match_delete_still_answers_from_a_button(six_calendars, as_admin, monkeypatch):
    """The not-found branch reads telegram_id too, for the friction log - so
    it crashed in the same way, just one line earlier."""
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([], False)))
    query = _callback_query()

    _dispatch(query, {"action": "delete", "status": "ok", "search_text": "kickboxing",
                      "calendar_key": "child_2_enrichment"})

    assert query.message.reply_text.await_count == 1


def test_a_query_survives_arriving_from_a_child_button(six_calendars, as_admin, monkeypatch):
    monkeypatch.setattr(ct, "list_events_range_all", lambda s, e, keys=None: [("Child_2 — Enrichment", PIANO)])
    query = _callback_query()

    _dispatch(query, {"action": "query", "status": "ok", "start": SATURDAY, "end": SATURDAY,
                      "calendar_key": ["child_2_enrichment"]})

    assert query.message.reply_text.await_count == 1


# ----- the message path must keep working too -----

def test_delete_still_works_from_a_plain_message(six_calendars, as_admin, monkeypatch):
    """Threading telegram_id through must not break the caller that always
    had an Update to read it from."""
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([("child_2_enrichment", PIANO)], False)))
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.message.reply_text = AsyncMock()

    asyncio.run(main._dispatch(update, 1, ADMIN_ID, {
        "action": "delete", "status": "ok", "search_text": "piano",
        "calendar_key": "child_2_enrichment"}, "delete piano"))

    assert update.message.reply_text.await_count == 1
