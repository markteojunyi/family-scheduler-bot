"""Saturday pickups now have two possible adults (Parent_2, Parent_1), and who
collects varies week to week. The confirm card asks rather than assuming, and
the answer feeds the clash check - it can only tell "one adult does both
children" from "one each" once a real name is on the event.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import main
import roster

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

FRIDAY = datetime.date(2026, 9, 11)
SATURDAY = datetime.date(2026, 9, 12)
SUNDAY = datetime.date(2026, 9, 13)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _at(date, h, m=0):
    return datetime.datetime.combine(date, datetime.time(h, m), tzinfo=ct.TIMEZONE)


def _event(summary, date, sh, eh, em=0, description=None, event_id="ev"):
    e = {"id": event_id, "summary": summary,
         "start": {"dateTime": _at(date, sh).isoformat()},
         "end": {"dateTime": _at(date, eh, em).isoformat()}}
    if description is not None:
        e["description"] = description
    return e


def _create_result(title, date, sh, eh, pickup=None, key="child_2_enrichment"):
    occ = {"all_day": False, "start": _at(date, sh), "end": _at(date, eh), "date": None,
           "time_anchor": "end"}
    return {"action": "create", "status": "ok", "title": title, "location": None,
            "notes": None, "pickup": pickup, "calendar_key": key,
            "occurrences": [occ], **occ}


# ----- roster.carer_choice_needed -----

def test_saturday_with_nobody_named_needs_a_choice(six_calendars):
    assert roster.carer_choice_needed(_at(SATURDAY, 9)) is True


def test_a_named_carer_answers_it_already(six_calendars):
    assert roster.carer_choice_needed(_at(SATURDAY, 9), override="Grandparent_1") is False


@pytest.mark.parametrize("date", [FRIDAY, SUNDAY])
def test_other_days_keep_the_roster(six_calendars, date):
    assert roster.carer_choice_needed(_at(date, 9)) is False


# ----- the card asks -----

def _card(monkeypatch, result, existing=()):
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "find_conflicts_for_child", lambda *a, **k: [])
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: list(existing))
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    asyncio.run(main._present_create(update, 1, result))
    call = update.message.reply_text.await_args
    keyboard = call.kwargs["reply_markup"]
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    return call.args[0], buttons


def test_a_saturday_create_asks_who_collects(six_calendars, monkeypatch):
    text, buttons = _card(monkeypatch, _create_result("Child_2 9am Ballet", SATURDAY, 8, 9))
    assert "Who's collecting on Saturday?" in text
    assert "pk:0" in buttons and "pk:1" in buttons


def test_a_saturday_create_with_a_named_carer_does_not_ask(six_calendars, monkeypatch):
    text, buttons = _card(monkeypatch, _create_result("Child_2 9am Ballet", SATURDAY, 8, 9, pickup="Grandparent_1"))
    assert "collecting on Saturday" not in text
    assert not any(b.startswith("pk:") for b in buttons)


def test_a_weekday_create_does_not_ask(six_calendars, monkeypatch):
    text, buttons = _card(monkeypatch, _create_result("Child_2 9am Ballet", FRIDAY, 8, 9))
    assert "collecting on Saturday" not in text
    assert not any(b.startswith("pk:") for b in buttons)


def test_the_speculative_clash_is_held_until_the_carer_is_known(six_calendars, monkeypatch):
    """Under the roster default both children's Saturday pickups are Parent_2, so
    the check would cry clash - but that is speculative until someone says who
    is actually collecting. Hold it; the re-render shows the real picture."""
    child_1_swim = ("child_1_enrichment", _event("Child_1 9am Swim", SATURDAY, 8, 9, event_id="s"))
    text, buttons = _card(
        monkeypatch, _create_result("Child_2 9am Ballet", SATURDAY, 8, 9), [child_1_swim]
    )
    assert "needed twice" not in text
    assert "Who's collecting on Saturday?" in text


# ----- answering feeds the clash check -----

def _rerender_with_pickup(monkeypatch, result, person, existing=()):
    saved = {}
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.update(payload))
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: list(existing))
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    result = {**result, "pickup": person}
    asyncio.run(main._show_create_confirm(query, 1, result))
    return query.edit_message_text.await_args.args[0], saved


def test_choosing_mark_clears_the_wife_double_booking(six_calendars, monkeypatch):
    child_1_swim = ("child_1_enrichment", _event("Child_1 9am Swim", SATURDAY, 8, 9, event_id="s"))
    result = _create_result("Child_2 9am Ballet", SATURDAY, 8, 9)

    wife_text, _ = _rerender_with_pickup(monkeypatch, result, "Parent_2", [child_1_swim])
    assert "needed twice" in wife_text  # both default-and-chosen to Parent_2 -> clash

    mark_text, saved = _rerender_with_pickup(monkeypatch, result, "Parent_1", [child_1_swim])
    assert "needed twice" not in mark_text  # two adults, no clash
    assert saved["pickup"] == "Parent_1"
    assert "collecting on Saturday" not in mark_text


def test_the_pk_callback_sets_the_carer_and_rerenders(six_calendars, monkeypatch):
    pending = {"kind": "create", "source_text": "child_2 ballet saturday 9am",
               **_create_result("Child_2 9am Ballet", SATURDAY, 8, 9)}
    monkeypatch.setattr(main.db, "peek_pending", lambda cid, kind: dict(pending))
    saved = {}
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.update(payload))
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)

    query = MagicMock()
    query.data = "pk:1"  # Parent_1
    query.from_user.id = ADMIN_ID
    query.message.chat.id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = 1

    asyncio.run(main.handle_callback(update, MagicMock()))

    text = query.edit_message_text.await_args.args[0]
    assert "Pickup: Parent\\_1" in text
    assert "collecting on Saturday" not in text
    assert saved["pickup"] == "Parent_1"
