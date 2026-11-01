"""How /upcoming and query results are laid out.

The flat version restated the child, the calendar tag and the date on every
row, and printed "14:45-15:00" beside a title that already said "3pm". Seven
events filled a phone screen and none of it could be skimmed - reported
2026-08-11.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import main


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _timed(summary, date, start, end):
    return {"summary": summary,
            "start": {"dateTime": f"{date}T{start}:00+08:00"},
            "end": {"dateTime": f"{date}T{end}:00+08:00"}}


def _all_day(summary, date):
    return {"summary": summary, "start": {"date": date}, "end": {"date": date}}


SCHOOL_END = ("child_1_school", _timed("Child_1 3pm Gate 2 End school", "2026-08-17", "14:45", "15:00"))
CLOSURE = ("child_1_school", _all_day("Child_1 School closure", "2026-08-12"))
PIANO = ("child_2_enrichment", _timed("Child_2 5pm Piano", "2026-08-17", "16:00", "17:00"))


def test_each_day_is_a_heading_not_a_repeated_column(six_calendars):
    lines = main._grouped_event_lines([CLOSURE, SCHOOL_END])
    headings = [l for l in lines if l.strip().startswith("*") and "Aug" in l]
    assert len(headings) == 2
    # The date must not also be repeated on the event rows underneath it.
    assert not any("Aug" in l for l in lines if l not in headings)


def test_the_child_is_dropped_when_the_list_is_one_child(six_calendars):
    """It is in the header, and repeating it seven times is what made the
    old list unreadable."""
    lines = main._grouped_event_lines([CLOSURE, SCHOOL_END])
    assert not any("Child_1" in l for l in lines)


def test_the_child_is_kept_when_the_list_is_mixed(six_calendars):
    lines = main._grouped_event_lines([SCHOOL_END, PIANO])
    body = "\n".join(lines)
    assert "Child\\_1" in body and "Child\\_2" in body


def test_the_category_becomes_a_glyph(six_calendars):
    """"[Child_1 — School]" was the longest thing on the row and the least
    informative."""
    lines = main._grouped_event_lines([SCHOOL_END, PIANO])
    body = "\n".join(lines)
    assert main._CATEGORY_ICON["school"] in body
    assert main._CATEGORY_ICON["enrichment"] in body
    assert "[Child_1 — School]" not in body


def test_the_glyphs_are_all_different(six_calendars):
    """The first set read as three shades of red on a dark phone and the
    enrichment note was nearly invisible - reported 2026-08-11. Sameness is
    the failure being guarded against, so this asserts distinctness."""
    icons = list(main._CATEGORY_ICON.values())
    assert len(set(icons)) == len(icons)
    assert not {"🎒", "🎵", "📌"} & set(icons)


def test_the_time_is_not_printed_twice(six_calendars):
    """The title already says 3pm; "14:45-15:00" beside it was noise."""
    line = [l for l in main._grouped_event_lines([SCHOOL_END]) if "End school" in l][0]
    assert "3pm" in line
    assert "14:45" not in line and "15:00" not in line


def test_the_clock_shown_is_the_one_in_the_title(six_calendars):
    """Read from the title, not recomputed, so a row can never state a
    different time from the title beside it."""
    event = ("child_1_school", _timed("Child_1 8am Assembly", "2026-08-17", "08:00", "08:15"))
    line = [l for l in main._grouped_event_lines([event]) if "Assembly" in l][0]
    assert "8am" in line and "8.15am" not in line


def test_a_gate_is_labelled_not_left_as_a_bare_number(six_calendars):
    line = [l for l in main._grouped_event_lines([SCHOOL_END]) if "End school" in l][0]
    assert "Gate 2" in line


def test_an_all_day_event_has_no_clock(six_calendars):
    line = [l for l in main._grouped_event_lines([CLOSURE]) if "closure" in l][0]
    assert "*" not in line  # no bolded time, and no pickup either


# ----- who is collecting -----

def test_a_timed_event_names_who_is_collecting(six_calendars):
    """Otherwise the list raises the question and /pickups has to answer it."""
    line = [l for l in main._grouped_event_lines([SCHOOL_END]) if "End school" in l][0]
    assert "→" in line and "Helper\\_1" in line  # weekday roster, Child\\_1


def test_the_pickup_shown_is_the_one_on_the_event(six_calendars):
    """A `Pickup:` line beats the roster here exactly as it does in
    /pickups - the two must never name different people."""
    key, event = SCHOOL_END
    named = (key, dict(event, description="Pickup: Grandparent_1\nType: School"))
    line = [l for l in main._grouped_event_lines([named]) if "End school" in l][0]
    assert "Grandparent\\_1" in line and "Helper\\_1" not in line


def test_an_all_day_event_names_nobody(six_calendars):
    """There is no moment to turn up at, so claiming a carer would be
    inventing one."""
    line = [l for l in main._grouped_event_lines([CLOSURE]) if "closure" in l][0]
    assert "→" not in line


def test_an_unrostered_pickup_is_flagged_not_left_blank(six_calendars, monkeypatch):
    monkeypatch.setattr(main.roster, "PICKUP_ROSTER", {"weekday": {}, "saturday": {}, "sunday": {}})
    line = [l for l in main._grouped_event_lines([SCHOOL_END]) if "End school" in l][0]
    assert main._NOBODY_ROSTERED.strip("⚠️ ") in line or "⚠️" in line


def test_two_events_on_one_day_share_a_heading(six_calendars):
    lines = main._grouped_event_lines([SCHOOL_END, PIANO])
    assert len([l for l in lines if "17 Aug" in l]) == 1


# ----- the handler -----

def test_an_empty_list_says_so(six_calendars):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._reply_event_list(update, [], "Nothing coming up."))
    assert update.message.reply_text.await_args.args[0] == "Nothing coming up."


def test_a_header_is_shown_when_given(six_calendars):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._reply_event_list(update, [SCHOOL_END], "none", header="Coming up — Child_1"))
    assert "Coming up — Child\\_1" in update.message.reply_text.await_args.args[0]


def test_a_single_calendar_setup_keeps_the_flat_rendering(monkeypatch):
    """One calendar means no key to group or label by."""
    monkeypatch.setenv("CALENDAR_ID", "one-calendar")
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._reply_event_list(update, [SCHOOL_END[1]], "none"))
    assert update.message.reply_text.await_count == 1


# ----- the header on a bare /upcoming -----

def _run_upcoming(monkeypatch, events, args=None):
    monkeypatch.setattr(main.calendar_tools, "list_upcoming_events_all",
                        lambda n_per_calendar=10, keys=None: events)
    update = MagicMock()
    update.effective_user.id = int(os.environ["ADMIN_TELEGRAM_ID"])
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    asyncio.run(main.upcoming(update, context))
    return update.message.reply_text.await_args.args[0]


def test_a_bare_upcoming_names_the_child_when_only_one_appears(six_calendars, monkeypatch):
    """No child was asked for, so nothing filled the header - but "Coming up"
    over a list that is entirely Child_1's is a header that says nothing."""
    body = _run_upcoming(monkeypatch, [CLOSURE, SCHOOL_END])
    assert "Coming up — Child\\_1" in body


def test_naming_one_child_also_says_who_has_nothing(six_calendars, monkeypatch):
    """Otherwise "Coming up - Child_1" reads as a filter and Child_2 looks
    excluded when she is simply free."""
    body = _run_upcoming(monkeypatch, [CLOSURE, SCHOOL_END])
    assert "Nothing coming up for Child\\_2." in body


def test_a_mixed_list_keeps_the_plain_header(six_calendars, monkeypatch):
    body = _run_upcoming(monkeypatch, [SCHOOL_END, PIANO])
    assert "Coming up" in body and "Coming up —" not in body
    assert "Nothing coming up for" not in body


def test_an_explicit_child_is_not_second_guessed(six_calendars, monkeypatch):
    """/upcoming child_1 IS a filter, so it must not apologise for Child_2."""
    body = _run_upcoming(monkeypatch, [SCHOOL_END], args=["child_1"])
    assert "Coming up — Child\\_1" in body
    assert "Child_2" not in body
