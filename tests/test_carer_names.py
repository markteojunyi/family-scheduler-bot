"""Folding a carer's name onto its canonical spelling.

Carers are free text, and _same_person compares them exactly - so "paernt_1" and
"Parent_1" read as two different adults and a real pickup clash goes unwarned. The
fix canonicalises a known name (and an obvious typo of one) while leaving a
genuinely new carer alone.
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


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


# ----- roster.canonical_carer -----

@pytest.mark.parametrize("written,expected", [
    ("Parent_1", "Parent_1"),
    ("parent_1", "Parent_1"),
    ("PARENT_1", "Parent_1"),
    (" grandparent_1 ", "Grandparent_1"),
    ("parnt_1", "Parent_1"),         # one substitution
    ("grandparet_1", "Grandparent_1"),  # one deletion
    ("helpr_1", "Helper_1"),      # one deletion
    ("Other_carer", "Other_carer"),     # a real new carer - untouched
    ("Bobby", "Bobby"),       # nowhere near any carer - never folded
    ("paernt_1", "paernt_1"),         # a transposition is two edits - not folded (conservative)
    ("", ""),
    (None, None),
])
def test_canonical_carer(written, expected):
    assert roster.canonical_carer(written) == expected


def test_is_known_carer():
    assert roster.is_known_carer("parent_1") is True
    assert roster.is_known_carer("parnt_1") is True          # folds first
    assert roster.is_known_carer("Other_carer") is False
    assert roster.is_known_carer("") is False


def test_a_folded_typo_is_the_same_person_to_the_detector():
    assert roster._same_person(roster.canonical_carer("parnt_1"),
                               roster.canonical_carer("Parent_1"))


# ----- _dispatch canonicalises before routing -----

def _dispatch(monkeypatch, result):
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main, "_present_create", AsyncMock())
    monkeypatch.setattr(main, "_present_amend", AsyncMock())
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._dispatch(update, 1, ADMIN_ID, result, "src"))
    return result


def test_dispatch_folds_a_create_pickup(six_calendars, monkeypatch):
    r = _dispatch(monkeypatch, {"action": "create", "status": "ok", "pickup": "grandparent_2"})
    assert r["pickup"] == "Grandparent_2"


def test_dispatch_folds_an_amend_pickup(six_calendars, monkeypatch):
    r = _dispatch(monkeypatch, {"action": "amend", "status": "ok",
                                "changes": {"pickup": "parnt_1"}})
    assert r["changes"]["pickup"] == "Parent_1"


def test_dispatch_leaves_a_new_carer_alone(six_calendars, monkeypatch):
    r = _dispatch(monkeypatch, {"action": "create", "status": "ok", "pickup": "Other_carer"})
    assert r["pickup"] == "Other_carer"


# ----- the card flags an unrecognised name -----

def _create_card(monkeypatch, pickup):
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "find_conflicts_for_child", lambda *a, **k: [])
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    occ = {"all_day": False,
           "start": datetime.datetime.combine(FRIDAY, datetime.time(15), tzinfo=ct.TIMEZONE),
           "end": datetime.datetime.combine(FRIDAY, datetime.time(16), tzinfo=ct.TIMEZONE),
           "date": None, "time_anchor": "end"}
    result = {"action": "create", "status": "ok", "title": "Child_2 4pm Ballet",
              "location": None, "notes": None, "pickup": pickup,
              "calendar_key": "child_2_enrichment", "occurrences": [occ], **occ}
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    asyncio.run(main._present_create(update, 1, result))
    return update.message.reply_text.await_args.args[0]


def test_a_new_carer_name_is_flagged_on_the_card(six_calendars, monkeypatch):
    assert "new name" in _create_card(monkeypatch, "Other_carer")


def test_a_known_carer_name_is_not_flagged(six_calendars, monkeypatch):
    assert "new name" not in _create_card(monkeypatch, "Grandparent_1")


# ----- action=unknown gets a targeted reply, not the whole help dump -----

def test_unknown_gets_the_short_reply_and_is_logged(six_calendars, monkeypatch):
    noted = []
    monkeypatch.setattr(main, "_note_friction",
                        AsyncMock(side_effect=lambda tid, kind, detail: noted.append(kind)))
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    update = MagicMock()
    update.message.reply_text = AsyncMock()

    asyncio.run(main._dispatch(update, 1, ADMIN_ID, {"action": "unknown", "status": "ok"}, "wat"))

    reply = update.message.reply_text.await_args.args[0]
    assert "didn't catch" in reply
    assert "/help" in reply
    assert noted == ["not_understood"]


# ----- names one keystroke apart -----

def test_a_typo_between_two_near_identical_carers_is_not_guessed(monkeypatch):
    """"carer1" and "carer2" are one edit apart, so "carer3" is one edit from
    BOTH. Folding it onto whichever came first would read one adult as the
    other and hide a real pickup clash."""
    monkeypatch.setattr(roster, "KNOWN_CARERS", ("Carer1", "Carer2"))
    assert roster.canonical_carer("carer3") == "carer3"
    assert roster.canonical_carer("carer1") == "Carer1"      # exact still folds
    assert roster.canonical_carer("carr1") == "Carer1"       # one candidate only
