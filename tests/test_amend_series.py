"""Applying one change to a whole run of events - "change Child_2's ballet pickup
to Parent_1 for the next 4 months".

The chain: the parser recognises "for the next N ..." and turns the phrase into
a date window; _present_amend_series finds every matching event inside it and
lists them on one card; _amend_many patches them all on one ✅.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import event_parser as ep
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
TODAY = datetime.date(2026, 9, 8)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _event(summary, date, sh=9, eh=10, event_id="ev"):
    at = lambda h: datetime.datetime.combine(date, datetime.time(h), tzinfo=ct.TIMEZONE)
    return {"id": event_id, "summary": summary,
            "start": {"dateTime": at(sh).isoformat()}, "end": {"dateTime": at(eh).isoformat()}}


# ----- _series_window -----

def test_a_run_of_weeks_becomes_a_date():
    end, err = ep._series_window("6 weeks", TODAY)
    assert err is None and end == TODAY + datetime.timedelta(weeks=6)


def test_a_run_of_months_becomes_a_date():
    end, err = ep._series_window("4 months", TODAY)
    assert err is None and end == datetime.date(2027, 1, 8)


def test_an_until_date_is_read():
    end, err = ep._series_window("until 20 Dec", TODAY)
    assert err is None and end == datetime.date(2026, 12, 20)


def test_a_far_horizon_is_clamped():
    end, _ = ep._series_window("5 years", TODAY)
    assert end == TODAY + datetime.timedelta(days=ep.MAX_SERIES_DAYS)


@pytest.mark.parametrize("horizon", ["", "for a while", "going forward", "a bit"])
def test_a_vague_horizon_is_flagged(horizon):
    assert ep._series_window(horizon, TODAY) == (None, "vague")


# ----- the parser -----

def test_a_series_amend_carries_a_window(six_calendars):
    result = ep._finalize_amend(
        {"action": "amend", "title": "ballet", "child": "Child_2", "pickup": "Parent_1",
         "apply_to_series": True, "series_horizon": "4 months"},
        TODAY, "src",
    )
    assert result["status"] == "ok"
    assert result["changes"] == {"pickup": "Parent_1"}
    assert result["series_until"] == datetime.date(2027, 1, 8)


def test_a_series_amend_with_no_horizon_asks(six_calendars):
    result = ep._finalize_amend(
        {"action": "amend", "title": "ballet", "child": "Child_2", "pickup": "Parent_1",
         "apply_to_series": True},
        TODAY, "src",
    )
    assert result["status"] == "needs_clarification"
    assert "how far ahead" in result["question"].lower()


def test_a_series_amend_with_no_named_event_asks(six_calendars):
    result = ep._finalize_amend(
        {"action": "amend", "day": "monday", "pickup": "Parent_1",
         "apply_to_series": True, "series_horizon": "4 months"},
        TODAY, "src",
    )
    assert result["status"] == "needs_clarification"
    assert "recurring" in result["question"].lower()


def test_a_one_off_amend_is_untouched(six_calendars):
    result = ep._finalize_amend(
        {"action": "amend", "title": "ballet", "child": "Child_2", "pickup": "Parent_1"},
        TODAY, "src",
    )
    assert result["status"] == "ok"
    assert result["series_until"] is None


# ----- the card -----

def _run_present_series(monkeypatch, result, existing):
    saved = {}
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.update(kind=kind, payload=payload))
    monkeypatch.setattr(main, "_note_friction", AsyncMock())
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: list(existing))
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_amend_series(update, 1, ADMIN_ID, result, "src"))
    return update.message.reply_text.await_args.args[0], saved


def _series_result(**over):
    base = {"action": "amend", "status": "ok", "search_text": "ballet",
            "prefer_date": None, "changes": {"pickup": "Other_carer"},
            "calendar_key": ["child_2_school", "child_2_enrichment", "child_2_others"],
            "slot_options": [], "series_until": TODAY + datetime.timedelta(weeks=8)}
    base.update(over)
    return base


def test_the_card_lists_every_matching_event(six_calendars, monkeypatch):
    existing = [
        ("child_2_enrichment", _event("Child_2 9am Ballet", TODAY + datetime.timedelta(days=d), event_id=f"b{d}"))
        for d in (2, 9, 16)
    ] + [("child_2_school", _event("Child_2 3pm Assembly", TODAY + datetime.timedelta(days=3), event_id="x"))]

    text, saved = _run_present_series(monkeypatch, _series_result(), existing)

    assert "Apply to all 3?" in text
    assert text.count("•") == 3
    assert "Assembly" not in text
    assert saved["kind"] == "confirm"
    assert saved["payload"]["kind"] == "amend_series"
    assert [e["id"] for e in saved["payload"]["events"]] == ["b2", "b9", "b16"]
    assert saved["payload"]["changes"] == {"pickup": "Other_carer"}


def test_the_card_summarises_the_change(six_calendars, monkeypatch):
    existing = [("child_2_enrichment", _event("Child_2 9am Ballet", TODAY, event_id="b"))]
    text, _ = _run_present_series(monkeypatch, _series_result(), existing)
    assert "pickup → Other\\_carer" in text


def test_no_matches_explains_the_window(six_calendars, monkeypatch):
    text, saved = _run_present_series(monkeypatch, _series_result(), [])
    assert "don't see any" in text.lower()
    assert not saved  # nothing pending to confirm


# ----- the write -----

def test_amend_many_patches_every_event(six_calendars, monkeypatch):
    patched, audited = [], []
    monkeypatch.setattr(ct, "get_event",
                        lambda eid, key=None: _event("Child_2 9am Ballet", TODAY, event_id=eid))
    monkeypatch.setattr(ct, "patch_event",
                        lambda eid, **kw: patched.append((eid, kw)) or {"id": eid})
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: audited.append(a))

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = 1
    pending = {
        "kind": "amend_series", "changes": {"pickup": "Other_carer"},
        "title": "ballet", "change_summary": "pickup → Other_carer",
        "events": [{"id": "b1", "calendar_key": "child_2_enrichment"},
                   {"id": "b2", "calendar_key": "child_2_enrichment"}],
    }
    asyncio.run(main._amend_many(query, ADMIN_ID, "admin", pending, {}))

    assert [eid for eid, _ in patched] == ["b1", "b2"]
    assert "Updated all 2" in query.edit_message_text.await_args.args[0]
    # One reviewed decision: only the first row counts toward the rate limit.
    assert [a[2] for a in audited] == ["amend", "amend_series"]


def test_amend_many_reports_a_partial_failure(six_calendars, monkeypatch):
    def flaky_patch(eid, **kw):
        if eid == "b2":
            raise RuntimeError("boom")
        return {"id": eid}

    monkeypatch.setattr(ct, "get_event",
                        lambda eid, key=None: _event("Child_2 9am Ballet", TODAY, event_id=eid))
    monkeypatch.setattr(ct, "patch_event", flaky_patch)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = 1
    pending = {
        "kind": "amend_series", "changes": {"pickup": "Other_carer"}, "title": "ballet",
        "events": [{"id": "b1", "calendar_key": "child_2_enrichment"},
                   {"id": "b2", "calendar_key": "child_2_enrichment"},
                   {"id": "b3", "calendar_key": "child_2_enrichment"}],
    }
    asyncio.run(main._amend_many(query, ADMIN_ID, "admin", pending, {}))

    assert "Updated 2 of 3" in query.edit_message_text.await_args.args[0]
