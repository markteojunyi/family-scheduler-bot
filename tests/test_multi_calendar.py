"""Tests for ADR-003 (second calendar) and the Flow 2 trade-off ranking
scaffold: calendar_tools' registry/resolution/alternatives, event_parser's
child-clarification logic, and main.py's option-pick + calendar-key routing."""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import event_parser as ep
import main
import db

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])


# ----- test isolation from the real .env -----

def test_real_env_calendars_do_not_leak_into_tests():
    """conftest must neutralize every optional calendar var, not just the five
    it setdefaults. Adding real DAUGHTER_*_CALENDAR_ID values to .env once put
    six live calendars into the registry and broke seven tests in this file -
    all of which set up their own env but never thought to clear the rest."""
    assert list(ct._calendars()) == ["daughter 1"], (
        f"registry polluted by the real .env: {list(ct._calendars())}"
    )
    for n in (1, 2):
        for category in ("SCHOOL", "ENRICHMENT", "OTHERS"):
            assert not os.environ.get(f"DAUGHTER_{n}_{category}_CALENDAR_ID")


# ----- calendar_tools: registry and resolution -----

def test_legacy_single_calendar_backward_compat(monkeypatch):
    monkeypatch.setenv("CALENDAR_ID", "legacy-id")
    monkeypatch.delenv("DAUGHTER_1_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_CALENDAR_ID", raising=False)
    assert ct._calendar_id() == "legacy-id"
    assert ct.is_multi_calendar_configured() is False
    key, err = ct.resolve_calendar_key(None)
    assert err is None
    assert ct._calendar_id(key) == "legacy-id"


def test_two_calendars_registered_with_names(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")
    assert ct.is_multi_calendar_configured() is True
    assert set(ct.calendar_names()) == {"Emma", "Lily"}


def test_resolve_calendar_key_ambiguous_unknown_and_named(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")

    assert ct.resolve_calendar_key(None) == (None, "ambiguous")
    assert ct.resolve_calendar_key("Bobby") == (None, "unknown")
    key, err = ct.resolve_calendar_key("emma")  # case-insensitive
    assert err is None
    assert ct._calendar_id(key) == "cal-emma"


def test_calendar_id_fails_loudly_when_ambiguous(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    with pytest.raises(ValueError):
        ct._calendar_id(None)


def test_calendar_display_name(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    key, _ = ct.resolve_calendar_key("Emma")
    assert ct.calendar_display_name(key) == "Emma"


# ----- calendar_tools: suggest_alternatives -----

def test_suggest_alternatives_finds_free_slots(monkeypatch):
    monkeypatch.setattr(ct, "find_conflicts", lambda s, e, calendar_key=None: iter([]))
    start = datetime.datetime(2026, 7, 25, 16, 0, tzinfo=ct.TIMEZONE)
    end = datetime.datetime(2026, 7, 25, 17, 0, tzinfo=ct.TIMEZONE)
    alts = ct.suggest_alternatives(start, end, max_options=2)
    assert len(alts) == 2
    for s, e in alts:
        assert e - s == datetime.timedelta(hours=1)


def test_suggest_alternatives_skips_busy_and_unreasonable_hours(monkeypatch):
    # only the +2h offset (18:00) is free; -1h (15:00) is busy, -2h/+1h land
    # on reasonable hours too but we want to prove filtering picks the free one
    def fake_conflicts(s, e, calendar_key=None):
        if s.hour == 17:  # the +1h candidate is busy
            return iter([{"summary": "busy"}])
        return iter([])

    monkeypatch.setattr(ct, "find_conflicts", fake_conflicts)
    start = datetime.datetime(2026, 7, 25, 16, 0, tzinfo=ct.TIMEZONE)
    end = datetime.datetime(2026, 7, 25, 17, 0, tzinfo=ct.TIMEZONE)
    alts = ct.suggest_alternatives(start, end, max_options=1)
    assert len(alts) == 1
    assert alts[0][0].hour != 17  # did not return the busy slot


def test_suggest_alternatives_empty_when_nothing_free(monkeypatch):
    monkeypatch.setattr(ct, "find_conflicts", lambda s, e, calendar_key=None: iter([{"summary": "busy"}]))
    start = datetime.datetime(2026, 7, 25, 16, 0, tzinfo=ct.TIMEZONE)
    end = datetime.datetime(2026, 7, 25, 17, 0, tzinfo=ct.TIMEZONE)
    assert ct.suggest_alternatives(start, end) == []


# ----- calendar_tools: multi-calendar query aggregation -----

def test_list_events_range_all_tags_with_the_key_and_sorts(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")

    def fake_range(start_date, end_date, calendar_key=None):
        if calendar_key == "emma":
            return [{"summary": "Piano", "start": {"dateTime": "2026-07-25T17:00:00+08:00"}, "end": {"dateTime": "2026-07-25T18:00:00+08:00"}}]
        return [{"summary": "Swim", "start": {"dateTime": "2026-07-25T09:00:00+08:00"}, "end": {"dateTime": "2026-07-25T10:00:00+08:00"}}]

    monkeypatch.setattr(ct, "list_events_range", fake_range)
    combined = ct.list_events_range_all(datetime.date(2026, 7, 25), datetime.date(2026, 7, 25))
    # Calendar KEYS, not display names: the key is what a caller can compute
    # from (child, category, an icon), and the display name derives from it
    # while the reverse needs parsing a string built for humans.
    assert [key for key, _ in combined] == ["lily", "emma"]  # Swim (9am) before Piano (5pm)
    assert [ct.child_display_name(key) for key, _ in combined] == ["Lily", "Emma"]


# ----- event_parser: child resolution -----

def test_finalize_single_calendar_never_asks(monkeypatch):
    monkeypatch.setenv("CALENDAR_ID", "legacy-id")
    monkeypatch.delenv("DAUGHTER_1_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_CALENDAR_ID", raising=False)
    result = ep._finalize({"action": "create", "title": "Swim", "date": "2026-07-25"}, "src")
    assert result["status"] == "ok"
    assert result["calendar_key"] is not None


def test_finalize_create_ambiguous_asks_for_child(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")
    result = ep._finalize({"action": "create", "title": "Swim", "date": "2026-07-25"}, "src")
    assert result["status"] == "needs_clarification"
    assert "Emma" in result["question"] and "Lily" in result["question"]


def test_finalize_create_named_child_resolves(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")
    result = ep._finalize({"action": "create", "title": "Swim", "date": "2026-07-25", "child": "Emma"}, "src")
    assert result["status"] == "ok"
    assert result["calendar_key"] is not None


def test_finalize_query_defaults_to_both_no_clarification(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    result = ep._finalize({"action": "query", "range": "week"}, "src")
    assert result["status"] == "ok"
    assert result["calendar_key"] is None


def test_finalize_amend_unknown_child_names_it_in_question(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_2_CALENDAR_ID", "cal-lily")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Lily")
    result = ep._finalize({"action": "amend", "title": "piano", "new_start_time": "18:00", "child": "Bobby"}, "src")
    assert result["status"] == "needs_clarification"
    assert "Bobby" in result["question"]


# ----- main.py: trade-off ranking UI -----

def test_present_create_branches_to_options_on_conflict(monkeypatch):
    monkeypatch.setattr(
        main.calendar_tools, "find_conflicts", lambda s, e, calendar_key=None: iter([{"summary": "Existing"}])
    )
    monkeypatch.setattr(main.calendar_tools, "suggest_alternatives", lambda s, e, calendar_key=None: [
        (datetime.datetime(2026, 7, 25, 17, 0, tzinfo=main.calendar_tools.TIMEZONE),
         datetime.datetime(2026, 7, 25, 18, 0, tzinfo=main.calendar_tools.TIMEZONE)),
    ])
    saved = {}
    monkeypatch.setattr(main.db, "save_pending", lambda chat_id, kind, payload: saved.update(kind=kind, payload=payload))

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    result = {
        "action": "create", "status": "ok", "title": "Swim",
        "location": None, "notes": None, "all_day": False,
        "start": datetime.datetime(2026, 7, 25, 16, 0, tzinfo=main.calendar_tools.TIMEZONE),
        "end": datetime.datetime(2026, 7, 25, 17, 0, tzinfo=main.calendar_tools.TIMEZONE),
        "calendar_key": None,
    }
    asyncio.run(main._present_create(update, chat_id=1, result=result))

    assert saved["kind"] == "create_options"
    assert len(saved["payload"]["options"]) == 2  # original (conflicting) + 1 free alternative
    # buttons: 2 options + cancel
    sent_markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert len(sent_markup.inline_keyboard) == 3


def test_create_opt_callback_shows_confirm_for_chosen_slot(monkeypatch):
    payload = {
        "title": "Swim", "location": None, "notes": None, "calendar_key": None,
        "options": [
            {"start": datetime.datetime(2026, 7, 25, 16, 0, tzinfo=main.calendar_tools.TIMEZONE),
             "end": datetime.datetime(2026, 7, 25, 17, 0, tzinfo=main.calendar_tools.TIMEZONE)},
            {"start": datetime.datetime(2026, 7, 25, 17, 0, tzinfo=main.calendar_tools.TIMEZONE),
             "end": datetime.datetime(2026, 7, 25, 18, 0, tzinfo=main.calendar_tools.TIMEZONE)},
        ],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: payload if kind == "create_options" else None)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    # get_role is stubbed alongside can() because handle_callback now gates on
    # identity *before* capability - an unrecognised ID is dropped silently.
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "create_opt:1"  # pick the second (free) option
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    sent = query.edit_message_text.await_args.args[0]
    assert "17:00-18:00" in sent  # the chosen alternative slot, not the original
    # RETITLED for the slot that was picked. The stored title was built for
    # whichever option was listed first, so keeping it verbatim would write a
    # time onto the event that contradicts the time beside it.
    assert "Add: 6pm Swim" in sent


def test_delete_callback_data_carries_calendar_key(monkeypatch):
    """The del: callback_data must round-trip the calendar_key through the
    button, since Google event IDs are only meaningful within one calendar."""
    monkeypatch.setattr(main.calendar_tools, "find_events_by_title", lambda text, prefer_date, calendar_key=None: [
        {"id": "ev1", "summary": "Piano", "start": {"dateTime": "2026-07-25T16:00:00+08:00"}, "end": {"dateTime": "2026-07-25T17:00:00+08:00"}},
    ])
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    result = {"search_text": "piano", "prefer_date": None, "calendar_key": "emma"}
    asyncio.run(main._present_delete(update, ADMIN_ID, result))

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    callback_data = markup.inline_keyboard[0][0].callback_data
    assert callback_data == "del:emma|ev1"


def test_delete_callback_calls_calendar_tools_with_decoded_key(monkeypatch):
    calls = {}

    def fake_get_event(eid, calendar_key=None):
        calls["get"] = (eid, calendar_key)
        return {"summary": "Piano"}

    def fake_delete_event(eid, calendar_key=None):
        calls["delete"] = (eid, calendar_key)

    monkeypatch.setattr(main.calendar_tools, "get_event", fake_get_event)
    monkeypatch.setattr(main.calendar_tools, "delete_event", fake_delete_event)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    # get_role is stubbed alongside can() because handle_callback now gates on
    # identity *before* capability - an unrecognised ID is dropped silently.
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "del:emma|ev1"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert calls["get"] == ("ev1", "emma")
    assert calls["delete"] == ("ev1", "emma")


def test_amend_pick_carries_calendar_key_through_to_patch(monkeypatch):
    # keys_by_event, not one shared key: an amend search spans all of a
    # child's calendars (ADR-004), so each option carries its own.
    pick = {"changes": {"start_time": "18:00"}, "keys_by_event": {"ev1": "lily"}}
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: pick if kind == "amend_pick" else None)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    # get_role is stubbed alongside can() because handle_callback now gates on
    # identity *before* capability - an unrecognised ID is dropped silently.
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)

    calls = {}

    def fake_get_event(eid, calendar_key=None):
        calls["get_key"] = calendar_key
        return {
            "id": eid, "summary": "Piano",
            "start": {"dateTime": "2026-07-25T16:00:00+08:00"}, "end": {"dateTime": "2026-07-25T17:00:00+08:00"},
        }

    monkeypatch.setattr(main.calendar_tools, "get_event", fake_get_event)

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "amend:ev1"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert calls["get_key"] == "lily"
    # and the flow actually completed (not silently swallowed downstream by
    # safe_handler after some later TypeError) - a real confirm card was shown
    assert query.edit_message_text.await_count == 1
    assert "Update to:" in query.edit_message_text.await_args.args[0]


# ----- ADR-004: per-category calendars (school / enrichment / others) -----

@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_1_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def test_six_calendars_registered(six_calendars):
    cals = ct._calendars()
    assert len(cals) == 6
    assert cals["child_1_school"]["id"] == "cal-child_1-school"
    assert cals["child_2_others"]["child"] == "child_2"
    assert cals["child_2_others"]["category"] == "others"
    # the child axis is what a human picks between, so each name appears once
    assert ct.calendar_names() == ["Child_1", "Child_2"]
    assert ct.has_categorized_calendars() is True


def test_categorized_display_name(six_calendars):
    assert ct.calendar_display_name("child_1_school") == "Child_1 — School"


def test_resolve_child_and_category(six_calendars):
    assert ct.resolve_calendar_key("child_1", "enrichment") == ("child_1_enrichment", None)


def test_resolve_child_without_category_is_ambiguous_for_writes(six_calendars):
    assert ct.resolve_calendar_key("child_1") == (None, "ambiguous_category")


def test_resolve_child_without_category_spans_calendars_for_reads(six_calendars):
    keys, err = ct.resolve_calendar_key("child_1", require_child=False, require_category=False)
    assert err is None
    assert set(keys) == {"child_1_school", "child_1_enrichment", "child_1_others"}


def test_resolve_no_child_still_ambiguous_with_categories(six_calendars):
    assert ct.resolve_calendar_key(None) == (None, "ambiguous")


def test_resolve_unknown_category_for_child(six_calendars):
    assert ct.resolve_calendar_key("child_1", "medical") == (None, "unknown")


def test_partial_category_config(monkeypatch):
    """A child with only some categories configured still resolves."""
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_1_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_SCHOOL_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_ENRICHMENT_CALENDAR_ID", raising=False)
    monkeypatch.delenv("DAUGHTER_2_OTHERS_CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_1_SCHOOL_CALENDAR_ID", "cal-child_1-school")
    monkeypatch.setenv("DAUGHTER_1_ENRICHMENT_CALENDAR_ID", "cal-child_1-enrichment")
    assert set(ct._calendars()) == {"child_1_school", "child_1_enrichment"}
    assert ct.categories_for_child("child_1") == ["school", "enrichment"]


def test_flat_calendar_still_wins_when_no_categories(monkeypatch):
    """ADR-003 setups are untouched: no category vars means one flat key."""
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    for slot in (1, 2):
        for category in ct.CATEGORIES:
            monkeypatch.delenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Emma")
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "cal-emma")
    assert set(ct._calendars()) == {"emma"}
    assert ct._calendars()["emma"]["category"] is None
    assert ct.calendar_display_name("emma") == "Emma"
    assert ct.has_categorized_calendars() is False


def test_find_conflicts_for_child_spans_categories(six_calendars, monkeypatch):
    """A school event and an enrichment class live on different calendars but
    still compete for the same child's time."""
    def fake_events_on(date, calendar_key=None):
        if calendar_key == "child_1_enrichment":
            return [{
                "summary": "Piano",
                "start": {"dateTime": "2026-07-25T16:00:00+08:00"},
                "end": {"dateTime": "2026-07-25T17:00:00+08:00"},
            }]
        return []

    monkeypatch.setattr(ct, "list_events_on", fake_events_on)
    start = datetime.datetime(2026, 7, 25, 16, 30, tzinfo=ct.TIMEZONE)
    end = datetime.datetime(2026, 7, 25, 17, 30, tzinfo=ct.TIMEZONE)

    # writing to the school calendar still sees the enrichment clash
    found = list(ct.find_conflicts_for_child("child_1_school", start, end))
    assert [key for key, _ in found] == ["child_1_enrichment"]
    # and Child_2's calendars are not consulted at all
    assert not list(ct.find_conflicts_for_child("child_2_school", start, end))


def test_find_events_by_title_across_tags_each_match(six_calendars, monkeypatch):
    def fake_find(search_text, prefer_date=None, calendar_key=None):
        return [{"id": f"ev-{calendar_key}", "summary": "Dentist"}] if calendar_key == "child_1_others" else []

    monkeypatch.setattr(ct, "find_events_by_title", fake_find)
    matches = ct.find_events_by_title_across("Dentist", None, ct.calendar_keys_for_child("child_1"))
    assert matches == [("child_1_others", {"id": "ev-child_1_others", "summary": "Dentist"})]


# ----- event_parser: category clarification -----

def test_finalize_create_unclear_category_asks_and_never_guesses(six_calendars):
    result = ep._finalize({"action": "create", "title": "Thing", "date": "2026-07-25", "child": "Child_1"}, "src")
    assert result["status"] == "needs_clarification"
    q = result["question"]
    assert "Child_1" in q
    for category in ct.CATEGORIES:
        assert category in q
    # crucially it did NOT silently file this under "others"
    assert "calendar_key" not in result


def test_finalize_create_with_category_resolves(six_calendars):
    result = ep._finalize(
        {"action": "create", "title": "Piano", "date": "2026-07-25", "child": "Child_1", "category": "enrichment"},
        "src",
    )
    assert result["status"] == "ok"
    assert result["calendar_key"] == "child_1_enrichment"


def test_finalize_delete_does_not_ask_for_category(six_calendars):
    """Deleting an event the user can already name shouldn't require them to
    know which of the child's calendars it lives on."""
    result = ep._finalize({"action": "delete", "title": "Dentist", "child": "Child_1"}, "src")
    assert result["status"] == "ok"
    assert set(result["calendar_key"]) == {"child_1_school", "child_1_enrichment", "child_1_others"}


def test_finalize_delete_with_no_child_searches_everyone(six_calendars):
    """Reported live: "/upcoming Child_2" listed exactly one piano event, then
    "Delete piano appointment" asked "Is this for Child_1 or Child_2?". The search
    can answer that better than the user can - every match is offered as a
    labelled row, so one hit needs no question and two are resolved by the tap
    the delete already required."""
    result = ep._finalize({"action": "delete", "title": "piano appointment"}, "src")
    assert result["status"] == "ok"
    assert set(result["calendar_key"]) == {
        "child_1_school", "child_1_enrichment", "child_1_others",
        "child_2_school", "child_2_enrichment", "child_2_others",
    }


def test_finalize_amend_with_no_child_searches_everyone(six_calendars):
    result = ep._finalize({"action": "amend", "title": "piano", "new_end_time": "18:00"}, "src")
    assert result["status"] == "ok"
    assert len(result["calendar_key"]) == 6


def test_finalize_create_with_no_child_still_asks(six_calendars):
    """A create has to land on ONE calendar, so it cannot defer to a search -
    there is nothing to search. This is the line between the two."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "date": "2026-08-08", "end_time": "17:00"}, "src"
    )
    assert result["status"] == "needs_clarification"
    assert result["clarify"] == "child"


def test_finalize_delete_with_an_unrecognised_name_still_asks(six_calendars):
    """A name that matches nobody is usually a typo. Quietly searching
    everyone would bury it, and the user would think the event was gone."""
    result = ep._finalize({"action": "delete", "title": "piano", "child": "Bobby"}, "src")
    assert result["status"] == "needs_clarification"


def test_finalize_delete_with_a_named_child_still_narrows(six_calendars):
    """Searching everyone is the fallback, not the new default - naming a
    child must still cut the search down."""
    result = ep._finalize({"action": "delete", "title": "piano", "child": "Child_2"}, "src")
    assert set(result["calendar_key"]) == {"child_2_school", "child_2_enrichment", "child_2_others"}


def test_finalize_query_for_one_child_spans_their_calendars(six_calendars):
    result = ep._finalize({"action": "query", "child": "Child_2", "range": "week"}, "src")
    assert result["status"] == "ok"
    assert set(result["calendar_key"]) == {"child_2_school", "child_2_enrichment", "child_2_others"}


def test_finalize_query_no_child_still_means_everyone(six_calendars):
    result = ep._finalize({"action": "query", "range": "week"}, "src")
    assert result["calendar_key"] is None


# ----- main.py: routing with categorized calendars -----

def test_query_with_key_list_narrows_aggregation(six_calendars, monkeypatch):
    captured = {}

    def fake_range_all(start, end, keys=None):
        captured["keys"] = keys
        return []

    monkeypatch.setattr(main.calendar_tools, "list_events_range_all", fake_range_all)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._handle_query(update, {
        "start": datetime.date(2026, 7, 25), "end": datetime.date(2026, 7, 25),
        "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
    }))
    assert captured["keys"] == ["child_1_school", "child_1_enrichment", "child_1_others"]


def test_delete_buttons_carry_per_event_calendar_key(six_calendars, monkeypatch):
    monkeypatch.setattr(
        main.calendar_tools, "find_events_by_title_across",
        lambda text, date, keys: [
            ("child_1_school", {"id": "ev1", "summary": "Assembly", "start": {"date": "2026-07-25"}}),
            ("child_1_others", {"id": "ev2", "summary": "Dentist", "start": {"date": "2026-07-25"}}),
        ],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_delete(update, ADMIN_ID, {
        "search_text": "thing", "prefer_date": None,
        "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
    }))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    payloads = [row[0].callback_data for row in markup.inline_keyboard[:2]]
    assert payloads == ["del:child_1_school|ev1", "del:child_1_others|ev2"]


# ----- category clarification answered by button, not by typing -----

def test_ambiguous_category_offers_buttons_and_carries_the_parse(six_calendars):
    """The stalled parse travels with the question. Re-asking the model to
    read a bare "Others" was the original bug: it sometimes re-asked and
    sometimes fell through to the help text."""
    result = ep._finalize(
        {"action": "create", "title": "thing", "child": "Child_1", "day": "tuesday", "start_time": "15:00"},
        "Child_1 thing Tuesday 3pm",
    )
    assert result["status"] == "needs_clarification"
    assert result["clarify"] == "category"
    assert result["categories"] == ["school", "enrichment", "others"]
    assert result["raw"]["title"] == "thing"


def _explode(*a, **k):
    raise AssertionError("resuming must not call the model again")


def test_resume_with_category_needs_no_second_model_call(six_calendars, monkeypatch):
    monkeypatch.setattr(ep, "_call_model", _explode)
    resumed = ep.resume_with_category(
        {"action": "create", "title": "thing", "child": "Child_1", "day": "tuesday", "start_time": "15:00"},
        "Child_1 thing Tuesday 3pm",
        "others",
    )
    assert resumed["status"] == "ok"
    assert resumed["calendar_key"] == "child_1_others"


def test_category_button_routes_to_the_calendar_it_names(six_calendars, monkeypatch):
    """End-to-end for the reported bug: tapping "Others" must reach
    child_1_others, where typing "Others" previously reached nothing."""
    pending = {
        "raw": {"action": "create", "title": "thing", "child": "Child_1", "day": "tuesday", "start_time": "15:00"},
        "source_text": "Child_1 thing Tuesday 3pm",
        "categories": ["school", "enrichment", "others"],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: pending if kind == "category" else None)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    monkeypatch.setattr(main.calendar_tools, "find_conflicts_for_child", lambda key, s, e: [])

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "cat:2"  # "others"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert query.edit_message_text.await_count == 1
    card = query.edit_message_text.await_args.args[0]
    assert "Child_1 — Others" in card


@pytest.mark.parametrize("bad", ["cat:-1", "cat:99", "cat:notanint"])
def test_category_button_rejects_out_of_range_index(six_calendars, monkeypatch, bad):
    """callback_data is client-controlled; a wrapped index would file the
    event on a calendar the buttons never offered."""
    pending = {
        "raw": {"action": "create", "title": "thing", "child": "Child_1"},
        "source_text": "Child_1 thing Tuesday 3pm",
        "categories": ["school", "enrichment", "others"],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: pending if kind == "category" else None)
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    called = {"resume": False}
    monkeypatch.setattr(ep, "resume_with_category", lambda *a: called.update(resume=True))

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = bad
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert called["resume"] is False


# ----- "which child?" must not throw the parse away -----

def test_child_question_carries_the_parse_for_a_button(six_calendars):
    """The reported bug: an uncaptioned screenshot was parsed fine, the bot
    asked "Is this for Child_1 or Child_2?", and answering by typing "Child_2" re-parsed
    the bare word as a fresh request - so the extracted event vanished and the
    user got a list of Child_2's existing events instead."""
    result = ep._finalize(
        {"action": "create", "title": "assembly", "category": "school",
         "date": "2026-09-03", "end_time": "10:30"},
        "",
    )
    assert result["status"] == "needs_clarification"
    assert result["clarify"] == "child"
    assert result["children"] == ["Child_1", "Child_2"]
    assert result["raw"]["title"] == "assembly"


def test_resume_with_child_needs_no_second_model_call(six_calendars, monkeypatch):
    monkeypatch.setattr(ep, "_call_model", _explode)
    resumed = ep.resume_with_child(
        {"action": "create", "title": "assembly", "category": "school",
         "date": "2026-09-03", "end_time": "10:30"},
        "",
        "Child_2",
    )
    assert resumed["status"] == "ok"
    assert resumed["calendar_key"] == "child_2_school"
    assert resumed["title"] == "Child_2 10.30am Gate 2 Assembly"


def test_answering_the_child_can_chain_into_the_category_question(six_calendars, monkeypatch):
    """A screenshot that names neither axis stalls twice. The second question
    must still carry `raw`, or the fix only moves where the parse is lost."""
    monkeypatch.setattr(ep, "_call_model", _explode)
    resumed = ep.resume_with_child(
        {"action": "create", "title": "thing", "day": "tuesday", "start_time": "15:00"}, "", "Child_1",
    )
    assert resumed["clarify"] == "category"
    assert resumed["raw"]["title"] == "thing"


def test_child_button_keeps_the_screenshot_event(six_calendars, monkeypatch):
    """End to end: tapping "Child_2" reaches a confirm card for the extracted
    event, not a query about Child_2."""
    pending = {
        "raw": {"action": "create", "title": "assembly", "category": "school",
                "date": "2026-09-03", "end_time": "10:30"},
        "source_text": "assembly 2026-09-03 10:30",
        "children": ["Child_1", "Child_2"],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: pending if kind == "child" else None)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    monkeypatch.setattr(main.calendar_tools, "find_conflicts_for_child", lambda key, s, e: [])

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "kid:1"  # "Child_2"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    card = query.edit_message_text.await_args.args[0]
    assert "Assembly" in card
    assert "Child_2 — School" in card


def test_child_button_survives_the_real_database_round_trip(six_calendars, monkeypatch, tmp_path):
    """The same flow as above but through the REAL pending-state store.

    The stubbed version passes while the live bot fails, which is exactly what
    happened: pending state is JSON in SQLite, and the decoder coerced the
    "date" inside the model's raw output into a datetime.date. event_parser
    re-parses raw from scratch and expects a string, so the resume crashed with
    "something went wrong on my end". A dated screenshot hits this every time;
    a typed "Tuesday" never does, because it fills `day`, not `date`.

    Two independent fixes now stop that (the decoder leaves raw alone, and
    _resolve_when tolerates a date object), so either one alone keeps this
    green - test_raw_model_output_round_trips_untouched is what actually pins
    the decoder. What this test is for is the join: no stub anywhere between
    the tap and the card, so a future serialization change has to survive the
    whole chain rather than a mock of it."""
    import threading
    db.DB_PATH = str(tmp_path / "round_trip.db")
    db._local = threading.local()
    db.init_db()

    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    monkeypatch.setattr(main.calendar_tools, "find_conflicts_for_child", lambda key, s, e: [])

    raw = {"action": "create", "title": "Teachers' Day Celebration",
           "date": "2026-09-03", "end_time": "10:30", "category": "school"}
    db.save_pending(1, "child", {"raw": raw, "source_text": "", "children": ["Child_1", "Child_2"]})

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = "kid:1"  # "Child_2"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    card = query.edit_message_text.await_args.args[0]
    assert "Teachers' Day Celebration" in card
    assert "Child_2 — School" in card


@pytest.mark.parametrize("bad", ["kid:-1", "kid:99", "kid:notanint"])
def test_child_button_rejects_out_of_range_index(six_calendars, monkeypatch, bad):
    pending = {
        "raw": {"action": "create", "title": "thing"},
        "source_text": "thing",
        "children": ["Child_1", "Child_2"],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: pending if kind == "child" else None)
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    called = {"resume": False}
    monkeypatch.setattr(ep, "resume_with_child", lambda *a: called.update(resume=True))

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    query = update.callback_query
    query.data = bad
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert called["resume"] is False


def test_uncaptioned_photo_clarification_replays_the_extraction(six_calendars, monkeypatch):
    """Not every clarification is a button - a screenshot with no readable date
    still gets a typed follow-up. With no caption there is nothing to replay,
    so the model's own extraction stands in for one."""
    raw = {"action": "create", "title": "sports day", "location": "Field"}
    monkeypatch.setattr(ep, "_call_model", lambda content: raw)
    result = ep.interpret_image([{"type": "image_url", "image_url": {"url": "x"}}], "")
    assert result["status"] == "needs_clarification"
    assert "sports day" in result["source_text"]
    assert "Field" in result["source_text"]


# ----- event title format: "[Child] [Time] [Activity]" -----

def test_create_title_uses_child_end_time_and_activity(six_calendars):
    result = ep._finalize(
        {"action": "create", "title": "assembly", "child": "Child_1",
         "category": "school", "day": "thursday", "start_time": "09:00"},
        "Child_1 has assembly Thursday 9am",
    )
    # the lone 9am is the END time, so the title says 9am; a school event also
    # carries the pickup gate, defaulted since the message didn't name one
    assert result["title"] == "Child_1 9am Gate 2 Assembly"


def test_create_title_does_not_repeat_a_child_name_the_model_included(six_calendars):
    result = ep._finalize(
        {"action": "create", "title": "Child_1 thing", "child": "Child_1",
         "category": "others", "day": "tuesday", "start_time": "15:00"},
        "Child_1 thing Tuesday 3pm",
    )
    assert result["title"] == "Child_1 3pm Thing"


# ----- which end a lone time names is decided by the category -----

def test_a_lone_enrichment_time_is_the_start(six_calendars):
    """"piano at 5pm" means the class begins at 5 - the afternoon is planned
    around when it starts. Only a school event reads a lone time as a pickup."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2",
         "category": "enrichment", "day": "tuesday", "end_time": "17:00"},
        "Child_2 piano Tuesday 5pm",
    )
    assert (result["start"].hour, result["start"].minute) == (17, 0)
    assert result["end"] - result["start"] == datetime.timedelta(minutes=ep.EVENT_BLOCK_MINUTES)
    assert result["title"] == "Child_2 5pm Piano"


def test_a_lone_others_time_is_the_start(six_calendars):
    """A dentist "at 3pm" starts at 3, same as an enrichment class - the
    end-as-pickup reading is school-only."""
    result = ep._finalize(
        {"action": "create", "title": "dentist", "child": "Child_2",
         "category": "others", "day": "tuesday", "start_time": "15:00"},
        "Child_2 dentist Tuesday 3pm",
    )
    assert (result["start"].hour, result["start"].minute) == (15, 0)
    assert result["title"] == "Child_2 3pm Dentist"


def test_a_lone_school_time_stays_the_pickup_end(six_calendars):
    """Assembly "at 9am" is when she is collected - the block ends there and
    the title shows 9am, the same as before."""
    result = ep._finalize(
        {"action": "create", "title": "assembly", "child": "Child_1",
         "category": "school", "day": "tuesday", "end_time": "09:00"},
        "Child_1 assembly Tuesday 9am",
    )
    assert (result["end"].hour, result["end"].minute) == (9, 0)
    assert (result["start"].hour, result["start"].minute) == (8, 45)
    assert result["title"] == "Child_1 9am Gate 2 Assembly"


def test_time_is_start_anchors_the_block_at_the_stated_time(six_calendars):
    """"Drop off at 8" is 8:00-8:15, and the title must say 8am - showing the
    8.15am the block happens to end at would state a time nobody said."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2", "category": "enrichment",
         "day": "tuesday", "start_time": "08:00", "time_is_start": True},
        "Child_2 piano Tuesday from 8am",
    )
    assert (result["start"].hour, result["start"].minute) == (8, 0)
    assert (result["end"].hour, result["end"].minute) == (8, 15)
    assert result["title"] == "Child_2 8am Piano"


def test_the_category_rule_beats_the_family_wide_default(six_calendars, monkeypatch):
    """LONE_TIME_MEANS_START only governs an uncategorised calendar. Once a
    category is known it decides: enrichment starts at the stated time,
    school ends there, whatever the env flag says."""
    monkeypatch.setattr(ep, "LONE_TIME_MEANS_START", False)
    enrichment = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2",
         "category": "enrichment", "day": "tuesday", "end_time": "08:00"}, "src")
    assert enrichment["start"].hour == 8  # starts then, not collected then

    monkeypatch.setattr(ep, "LONE_TIME_MEANS_START", True)
    school = ep._finalize(
        {"action": "create", "title": "assembly", "child": "Child_1",
         "category": "school", "day": "tuesday", "end_time": "09:00"}, "src")
    assert school["end"].hour == 9 and school["start"].hour == 8  # still a pickup

    # Per-message wording still wins over the category.
    told_start = ep._finalize(
        {"action": "create", "title": "assembly", "child": "Child_1", "category": "school",
         "day": "tuesday", "start_time": "09:00", "time_is_start": True}, "src")
    assert told_start["start"].hour == 9


def test_a_stated_range_is_untouched_by_the_anchor(six_calendars):
    """time_is_start is about which end a LONE time names, so a stated range
    must ignore it entirely - the block is 4 to 5 whatever the flag says."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2", "category": "enrichment",
         "day": "tuesday", "start_time": "16:00", "end_time": "17:00", "time_is_start": True},
        "Child_2 piano Tuesday 4-5pm",
    )
    assert (result["start"].hour, result["end"].hour) == (16, 17)
    assert result["title"] == "Child_2 4-5pm Piano"


def test_an_enrichment_range_is_titled_with_both_ends(six_calendars):
    """A class runs 4 to 5 and the afternoon is planned around both ends, so
    the title states both - a lone time only ever shows the one number given."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2", "category": "enrichment",
         "day": "tuesday", "start_time": "16:00", "end_time": "17:00"},
        "Child_2 piano Tuesday 4 to 5pm",
    )
    assert result["title"] == "Child_2 4-5pm Piano"
    assert (result["start"].hour, result["end"].hour) == (16, 17)


def test_a_school_range_still_shows_only_the_end(six_calendars):
    """The rule is everything-but-school. School titles are read for their
    pickup time and already carry a gate; widening them buys nothing."""
    result = ep._finalize(
        {"action": "create", "title": "assembly", "child": "Child_1", "category": "school",
         "day": "tuesday", "start_time": "09:00", "end_time": "10:00"},
        "Child_1 assembly Tuesday 9 to 10am",
    )
    assert "9-10am" not in result["title"]
    assert "10am" in result["title"]


def test_an_others_range_is_titled_with_both_ends(six_calendars):
    """An appointment holds an afternoon the same way a class does, so it is
    read the same way: both ends when the message gave both."""
    result = ep._finalize(
        {"action": "create", "title": "dentist", "child": "Child_2", "category": "others",
         "day": "tuesday", "start_time": "14:00", "end_time": "15:00"},
        "Child_2 dentist Tuesday 2 to 3pm",
    )
    assert result["title"] == "Child_2 2-3pm Dentist"
    assert (result["start"].hour, result["end"].hour) == (14, 15)


def test_a_lone_others_time_shows_the_start_and_invents_no_end(six_calendars):
    """"Dentist at 3" is when the thing happens, so the block begins there and
    the title shows that one number - never a range the message didn't give."""
    result = ep._finalize(
        {"action": "create", "title": "dentist", "child": "Child_2", "category": "others",
         "day": "tuesday", "end_time": "15:00"},
        "Child_2 dentist Tuesday 3pm",
    )
    assert result["title"] == "Child_2 3pm Dentist"
    assert result["start"].hour == 15
    assert result["end"] - result["start"] == datetime.timedelta(minutes=ep.EVENT_BLOCK_MINUTES)


def test_a_lone_enrichment_time_never_invents_an_end(six_calendars):
    """The start moved to the stated time, but no end time is conjured - the
    block stays the fixed 15-minute stand-in for a duration nobody gave."""
    result = ep._finalize(
        {"action": "create", "title": "piano", "child": "Child_2", "category": "enrichment",
         "day": "tuesday", "end_time": "17:00"},
        "Child_2 piano Tuesday 5pm",
    )
    assert result["title"] == "Child_2 5pm Piano"
    assert result["start"].hour == 17
    assert result["end"] - result["start"] == datetime.timedelta(minutes=ep.EVENT_BLOCK_MINUTES)


def test_editing_only_the_location_keeps_a_range_title(six_calendars):
    """The same recovery-from-the-title rule the start-anchored case relies
    on. Without it a location edit silently retitles "4-5pm" to a bare "5pm"."""
    original = {
        "id": "ev1", "summary": "Child_2 4-5pm Piano",
        "start": {"dateTime": "2026-08-06T16:00:00+08:00"},
        "end": {"dateTime": "2026-08-06T17:00:00+08:00"},
    }
    final = main._apply_changes(original, {"location": "Hall B"}, "child_2_enrichment")
    assert final["title"] == "Child_2 4-5pm Piano"


def test_a_range_title_is_not_double_stamped_on_an_amend(six_calendars):
    """The clock has to be strippable by split_title, or the old one is left
    on the front of the activity and a second is added: "4-5pm 4-5pm Piano"."""
    original = {
        "id": "ev1", "summary": "Child_2 4-5pm Piano",
        "start": {"dateTime": "2026-08-06T16:00:00+08:00"},
        "end": {"dateTime": "2026-08-06T17:00:00+08:00"},
    }
    final = main._apply_changes(original, {"location": "Hall B"}, "child_2_enrichment")
    # Asserted through split_title rather than on the string: "Child_2 5pm 4-5pm
    # Piano" contains "4-5pm" exactly once too, so counting proves nothing.
    # What matters is that the clock came back OFF the activity.
    assert ep.split_title(final["title"], "Child_2") == ("Piano", None)


def test_a_new_full_range_upgrades_an_enrichment_title(six_calendars):
    """"move piano to 4 to 5.30" on an event titled "5pm Piano" must show the
    range it was just given, not keep the end-only title it happened to have."""
    result = ep._finalize(
        {"action": "amend", "title": "piano", "child": "Child_2",
         "new_start_time": "16:00", "new_end_time": "17:30"},
        "move Child_2 piano to 4 to 5.30pm",
    )
    original = {
        "id": "ev1", "summary": "Child_2 5pm Piano",
        "start": {"dateTime": "2026-08-06T16:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T17:00:00+08:00"},
    }
    final = main._apply_changes(original, result["changes"], "child_2_enrichment")
    assert final["title"] == "Child_2 4-5.30pm Piano"


def test_a_new_full_range_on_a_school_event_does_not_upgrade(six_calendars):
    """The same amend against a school event keeps the end-only title."""
    result = ep._finalize(
        {"action": "amend", "title": "assembly", "child": "Child_1",
         "new_start_time": "09:00", "new_end_time": "10:00"},
        "move Child_1 assembly to 9 to 10am",
    )
    original = {
        "id": "ev1", "summary": "Child_1 10am Gate 2 Assembly",
        "start": {"dateTime": "2026-08-06T09:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T10:00:00+08:00"},
    }
    final = main._apply_changes(original, result["changes"], "child_1_school")
    assert final["title"] == "Child_1 10am Gate 2 Assembly"


def test_a_lone_new_enrichment_time_starts_the_class(six_calendars):
    """"move piano to 6pm" - piano now BEGINS at 6, the same reading a create
    gives it. Decided in _anchor_for, since the amend only learns the event is
    on the enrichment calendar once it is found."""
    result = ep._finalize(
        {"action": "amend", "title": "piano", "child": "Child_2", "new_start_time": "18:00"},
        "move Child_2 piano to 6pm",
    )
    assert "time_anchor" not in result["changes"]
    original = {
        "id": "ev1", "summary": "Child_2 5pm Piano",
        "start": {"dateTime": "2026-08-06T16:00:00+08:00"},
        "end": {"dateTime": "2026-08-06T17:00:00+08:00"},
    }
    final = main._apply_changes(original, result["changes"], "child_2_enrichment")
    assert final["start"].hour == 18 and final["end"].hour == 19  # duration kept
    assert final["title"] == "Child_2 6pm Piano"


def test_a_lone_new_school_time_stays_the_pickup(six_calendars):
    """The same amend against a school event keeps the pickup-time reading -
    the block ends at 10am and the title is unchanged."""
    result = ep._finalize(
        {"action": "amend", "title": "assembly", "child": "Child_1", "new_start_time": "10:00"},
        "move Child_1 assembly to 10am",
    )
    original = {
        "id": "ev1", "summary": "Child_1 9am Gate 2 Assembly",
        "start": {"dateTime": "2026-08-06T08:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T09:00:00+08:00"},
    }
    final = main._apply_changes(original, result["changes"], "child_1_school")
    assert final["end"].hour == 10
    assert final["title"] == "Child_1 10am Gate 2 Assembly"


def test_the_parser_does_not_decide_the_range_on_an_amend(six_calendars):
    """An amend searches all three of the child's calendars, so at parse time
    there is no category to consult - the decision belongs to _anchor_for,
    after the event has been found. Pinned so it is not "helpfully" moved back
    into the parser, where it would silently do nothing."""
    result = ep._finalize(
        {"action": "amend", "title": "piano", "child": "Child_2",
         "new_start_time": "16:00", "new_end_time": "17:30"},
        "move Child_2 piano to 4 to 5.30pm",
    )
    assert result["calendar_key"] == ["child_2_school", "child_2_enrichment", "child_2_others"]
    assert "time_anchor" not in result["changes"]
    assert ep.wants_range_title(result["calendar_key"]) is False
    assert ep.wants_range_title(None) is False
    assert ep.wants_range_title("child_2_enrichment") is True
    assert ep.wants_range_title("child_2_others") is True
    assert ep.wants_range_title("child_2_school") is False


def test_amend_can_move_a_start_anchored_event(six_calendars):
    original = {
        "id": "ev1", "summary": "Child_2 8am Piano",
        "start": {"dateTime": "2026-08-06T08:00:00+08:00"},
        "end": {"dateTime": "2026-08-06T08:15:00+08:00"},
    }
    final = main._apply_changes(
        original, {"start_time": "09:00", "time_anchor": "start"}, "child_2_enrichment"
    )
    assert final["start"].hour == 9
    assert final["title"] == "Child_2 9am Piano"


def test_editing_only_the_location_keeps_a_start_anchored_title(six_calendars):
    """Nothing on the calendar records which end the title was built around,
    so it is recovered from the title itself - otherwise a location-only edit
    silently retitles "Child_2 8am Piano" to 8.15am."""
    original = {
        "id": "ev1", "summary": "Child_2 8am Piano",
        "start": {"dateTime": "2026-08-06T08:00:00+08:00"},
        "end": {"dateTime": "2026-08-06T08:15:00+08:00"},
    }
    final = main._apply_changes(original, {"location": "Hall B"}, "child_2_enrichment")
    assert final["title"] == "Child_2 8am Piano"


def test_editing_only_the_location_keeps_an_end_anchored_title(six_calendars):
    original = {
        "id": "ev1", "summary": "Child_2 5pm Piano",
        "start": {"dateTime": "2026-08-06T16:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T17:00:00+08:00"},
    }
    final = main._apply_changes(original, {"location": "Hall B"}, "child_2_enrichment")
    assert final["title"] == "Child_2 5pm Piano"


def test_all_day_title_omits_the_time_segment(six_calendars):
    result = ep._finalize(
        {"action": "create", "title": "mom visiting", "child": "Child_2",
         "category": "others", "day": "sunday"},
        "Child_2 mom visiting Sunday",
    )
    assert result["all_day"] is True
    assert result["title"] == "Child_2 Mom visiting"


# ----- school pickup gate in the title -----

def _school_create(six_calendars_unused=None, **extra):
    raw = {"action": "create", "title": "assembly", "child": "Child_1",
           "category": "school", "day": "thursday", "start_time": "09:00"}
    raw.update(extra)
    return ep._finalize(raw, "src")


def test_school_event_defaults_to_gate_2(six_calendars):
    assert _school_create()["title"] == "Child_1 9am Gate 2 Assembly"


def test_stated_gate_overrides_the_default(six_calendars):
    assert _school_create(gate="5")["title"] == "Child_1 9am Gate 5 Assembly"


@pytest.mark.parametrize("stated", ["3", "gate 3", "Gate3", 3])
def test_gate_is_normalized_however_the_model_reports_it(six_calendars, stated):
    assert _school_create(gate=stated)["title"] == "Child_1 9am Gate 3 Assembly"


def test_gate_left_in_the_activity_is_not_written_twice(six_calendars):
    """The model sometimes keeps "gate 4" in the title it extracted."""
    result = _school_create(title="gate 4 assembly")
    assert result["title"] == "Child_1 9am Gate 4 Assembly"


def test_non_school_events_get_no_gate(six_calendars):
    """A gate on a dentist appointment would be nonsense."""
    for category in ("enrichment", "others"):
        result = ep._finalize(
            {"action": "create", "title": "piano", "child": "Child_2",
             "category": category, "day": "tuesday", "start_time": "16:00"},
            "src",
        )
        assert "Gate" not in result["title"], category


def test_amend_keeps_the_gate_it_already_had(six_calendars):
    """Moving a school event must not strand a parent at an unknown gate."""
    original = {
        "id": "ev1", "summary": "Child_1 9am Gate 5 Assembly",
        "start": {"dateTime": "2026-08-06T08:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T09:00:00+08:00"},
    }
    final = main._apply_changes(original, {"start_time": "10:00"}, "child_1_school")
    assert final["title"] == "Child_1 10am Gate 5 Assembly"


def test_amend_can_change_the_gate(six_calendars):
    original = {
        "id": "ev1", "summary": "Child_1 9am Gate 5 Assembly",
        "start": {"dateTime": "2026-08-06T08:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T09:00:00+08:00"},
    }
    final = main._apply_changes(original, {"gate": "1"}, "child_1_school")
    assert final["title"] == "Child_1 9am Gate 1 Assembly"


def test_gate_segment_does_not_stack_across_repeated_amends(six_calendars):
    original = {
        "id": "ev1", "summary": "Child_1 9am Gate 2 Assembly",
        "start": {"dateTime": "2026-08-06T08:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T09:00:00+08:00"},
    }
    once = main._apply_changes(original, {"start_time": "10:00"}, "child_1_school")
    twice = main._apply_changes(
        {**original, "summary": once["title"]}, {"start_time": "11:00"}, "child_1_school"
    )
    assert twice["title"] == "Child_1 11am Gate 2 Assembly"


# ----- no gate on a day nobody is collected -----

@pytest.mark.parametrize("activity", [
    "No school", "no classes", "School holiday", "Public holiday",
    "Term break", "School closed", "classes cancelled", "Teachers' day off",
])
def test_no_pickup_days_get_no_gate(six_calendars, activity):
    """A gate is somewhere to turn up. Printing one on a day the child isn't
    there reads as an instruction to go and collect her."""
    result = _school_create(title=activity)
    assert "Gate" not in result["title"], activity


@pytest.mark.parametrize("activity", [
    "Holiday concert", "Sports day", "Assembly", "Book fair",
])
def test_ordinary_school_events_keep_their_gate(six_calendars, activity):
    """The matching is deliberately narrow: a missing gate is the failure that
    strands a parent, so anything ambiguous keeps one. "Holiday concert" is a
    real event with a real pickup."""
    assert "Gate 2" in _school_create(title=activity)["title"], activity


def test_model_flag_suppresses_the_gate_for_wording_the_regex_misses(six_calendars):
    """'PH' means public holiday here, and no word list will ever cover every
    way the family writes that - the model's own reading is the other half."""
    result = _school_create(title="PH", no_pickup=True)
    assert "Gate" not in result["title"]


def test_a_stated_gate_still_wins_over_no_pickup(six_calendars):
    """no_pickup is an inference; a gate a human typed is not."""
    result = _school_create(title="No school (office open)", gate="4")
    assert "Gate 4" in result["title"]


def test_amend_into_a_no_school_day_drops_the_gate(six_calendars):
    original = {
        "id": "ev1", "summary": "Child_1 9am Gate 5 Assembly",
        "start": {"dateTime": "2026-08-06T08:45:00+08:00"},
        "end": {"dateTime": "2026-08-06T09:00:00+08:00"},
    }
    final = main._apply_changes(original, {"title": "no school"}, "child_1_school")
    assert "Gate" not in final["title"]


def test_event_actually_named_gate_keeps_its_name(six_calendars):
    """"Gate 2" alone is the activity, not a gate segment with no activity."""
    activity, gate = ep.split_title("Gate 2", "Child_1")
    assert (activity, gate) == ("Gate 2", None)


def test_category_calendars_win_over_legacy_vars(six_calendars, monkeypatch):
    """Per child, a set category variable makes _calendars() skip the flat and
    legacy fallbacks entirely - so a leftover CALENDAR_ID in .env cannot
    quietly demote a categorized family back to one calendar."""
    monkeypatch.setenv("CALENDAR_ID", "legacy-should-be-ignored")
    monkeypatch.setenv("DAUGHTER_1_CALENDAR_ID", "flat-should-be-ignored")

    cals = ct._calendars()
    assert sorted(cals) == sorted([
        "child_1_school", "child_1_enrichment", "child_1_others",
        "child_2_school", "child_2_enrichment", "child_2_others",
    ])
    assert not [v["id"] for v in cals.values() if "ignored" in v["id"]]


# ----- intent-based matching: a title miss must not dead-end -----

def _ev(eid, summary, date="2026-08-03"):
    return {"id": eid, "summary": summary,
            "start": {"dateTime": f"{date}T15:00:00+08:00"},
            "end": {"dateTime": f"{date}T16:00:00+08:00"}}


def test_generic_reference_falls_back_to_that_days_events(six_calendars, monkeypatch):
    """The reported bug: "cancel Child_1's appointment today" searched literally
    for "appointment", found nothing, and gave up - while Child_1 plainly had
    something on that day."""
    monkeypatch.setattr(
        main.calendar_tools, "list_events_range_across",
        lambda start, end, keys: [("child_1_school", _ev("ev1", "Child_1 3pm Gate 2 Assembly"))],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_delete(update, ADMIN_ID, {
        "search_text": "appointment", "prefer_date": datetime.date(2026, 8, 3),
        "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
    }))

    text = update.message.reply_text.await_args.args[0]
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert "couldn't match" in text          # says so rather than pretending
    assert markup.inline_keyboard[0][0].callback_data == "del:child_1_school|ev1"


def test_a_title_that_matches_still_narrows(six_calendars, monkeypatch):
    """The fallback must not swallow real filtering - naming an event should
    still pick it out of a busy day."""
    monkeypatch.setattr(
        main.calendar_tools, "list_events_range_across",
        lambda start, end, keys: [
            ("child_1_school", _ev("ev1", "Child_1 3pm Gate 2 Assembly")),
            ("child_1_enrichment", _ev("ev2", "Child_1 5pm Piano")),
        ],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_delete(update, ADMIN_ID, {
        "search_text": "piano", "prefer_date": datetime.date(2026, 8, 3),
        "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
    }))

    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    payloads = [row[0].callback_data for row in markup.inline_keyboard[:-1]]
    assert payloads == ["del:child_1_enrichment|ev2"]


def test_no_title_at_all_lists_the_whole_day(six_calendars, monkeypatch):
    monkeypatch.setattr(
        main.calendar_tools, "list_events_range_across",
        lambda start, end, keys: [
            ("child_1_school", _ev("ev1", "Child_1 3pm Gate 2 Assembly")),
            ("child_1_enrichment", _ev("ev2", "Child_1 5pm Piano")),
        ],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_delete(update, ADMIN_ID, {
        "search_text": "", "prefer_date": datetime.date(2026, 8, 3),
        "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
    }))
    markup = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert len(markup.inline_keyboard) == 3  # two events + Cancel


def test_empty_day_says_so_rather_than_quoting_a_missing_title(six_calendars, monkeypatch):
    monkeypatch.setattr(main.calendar_tools, "list_events_range_across", lambda start, end, keys: [])
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_delete(update, ADMIN_ID, {
        "search_text": "", "prefer_date": datetime.date(2026, 8, 3),
        "calendar_key": ["child_1_school"],
    }))
    text = update.message.reply_text.await_args.args[0]
    assert "don't see anything on" in text
    assert '""' not in text  # never quote an empty search string back at the user


def test_amend_fallback_shows_a_picker_instead_of_a_confirm_card(six_calendars, monkeypatch):
    """A lone fallback match must be picked deliberately - showing a confirm
    card for an event the user never named would be putting words in their
    mouth one tap away from a write."""
    monkeypatch.setattr(
        main.calendar_tools, "list_events_range_across",
        lambda start, end, keys: [("child_1_school", _ev("ev1", "Child_1 3pm Gate 2 Assembly"))],
    )
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._present_amend(update, 1, ADMIN_ID, {
        "search_text": "appointment", "prefer_date": datetime.date(2026, 8, 3),
        "calendar_key": ["child_1_school"], "changes": {"end_time": "18:00"},
    }))
    text = update.message.reply_text.await_args.args[0]
    assert "couldn't match" in text
    assert "Update to:" not in text


def test_delete_ignores_the_models_category_guess(six_calendars):
    """The model guesses a category from wording ("appointment" -> others),
    but the event lives wherever it was actually filed. Honouring the guess
    would search one calendar and miss an event on another - found live:
    "cancel Child_1's appointment today" resolved to child_1_others alone."""
    result = ep._finalize(
        {"action": "delete", "child": "Child_1", "day": "today", "category": "others"}, "src"
    )
    assert result["status"] == "ok"
    assert sorted(result["calendar_key"]) == sorted(
        ["child_1_school", "child_1_enrichment", "child_1_others"]
    )


def test_create_still_honours_the_category(six_calendars):
    """The discard must apply to delete/amend only - a create has to land on
    exactly one calendar."""
    result = ep._finalize(
        {"action": "create", "title": "dentist", "child": "Child_1",
         "day": "today", "end_time": "15:00", "category": "others"},
        "src",
    )
    assert result["calendar_key"] == "child_1_others"
