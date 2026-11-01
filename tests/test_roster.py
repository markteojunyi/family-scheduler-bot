"""Tests for roster.py: who collects whom, and when that becomes impossible.

No model, no network, no database - this is arithmetic about whether a person
can be in two places, and it is tested as such.
"""
import datetime

import pytest

import calendar_tools as ct
import roster


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


# Mon 07 Sep 2026 is a Monday; Sat 12 / Sun 13 Sep follow in the same week.
MONDAY = datetime.date(2026, 9, 7)
SATURDAY = datetime.date(2026, 9, 12)
SUNDAY = datetime.date(2026, 9, 13)


def _at(date, hour, minute=0):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


def _event(summary, date, start_h, end_h, start_m=0, end_m=0, description=None, event_id="ev"):
    event = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": _at(date, start_h, start_m).isoformat()},
        "end": {"dateTime": _at(date, end_h, end_m).isoformat()},
    }
    if description:
        event["description"] = description
    return event


# ----- day types and the roster table -----

@pytest.mark.parametrize("date,expected", [
    (MONDAY, "weekday"),
    (datetime.date(2026, 9, 11), "weekday"),   # Friday
    (SATURDAY, "saturday"),
    (SUNDAY, "sunday"),
])
def test_day_type(date, expected):
    assert roster.day_type(date) == expected


def test_weekday_splits_the_children_between_two_adults(six_calendars):
    assert roster.pickup_for("Child_2", _at(MONDAY, 15)) == "Parent_1"
    assert roster.pickup_for("Child_1", _at(MONDAY, 15)) == "Helper_1"


def test_weekend_wildcard_covers_every_child(six_calendars):
    """Saturday has one adult, so both children resolve to her - which is what
    makes two Saturday pickups collide under the ordinary same-person rule."""
    assert roster.pickup_for("Child_1", _at(SATURDAY, 10)) == "Parent_2"
    assert roster.pickup_for("Child_2", _at(SATURDAY, 10)) == "Parent_2"


def test_capacity_is_derived_from_the_table_not_configured(six_calendars):
    """Two adults on a weekday, one at the weekend. Nothing declares this - it
    is just how many distinct people the roster names, so it cannot fall out of
    step with the roster itself."""
    assert len(roster.carers_on(MONDAY)) == 2
    assert len(roster.carers_on(SATURDAY)) == 1
    assert len(roster.carers_on(SUNDAY)) == 1


def test_an_event_can_override_the_roster(six_calendars):
    """"Grandparent_1 is getting Child_1 on Friday" - a person a human named beats a
    default, the same precedence the pickup gate uses."""
    assert roster.pickup_for("Child_1", _at(MONDAY, 15), override="Grandparent_1") == "Grandparent_1"


def test_unknown_roster_children_are_reported(six_calendars, monkeypatch):
    """Renaming a child in .env silently unhooks the roster: every pickup
    becomes unassigned, which reads as "the feature does nothing" rather than
    as an error. Startup logs this instead."""
    monkeypatch.setattr(roster, "PICKUP_ROSTER", {"weekday": {"bobby": "Parent_1"}})
    assert roster.unknown_roster_children() == ["bobby"]


def test_no_false_alarm_when_the_roster_matches(six_calendars):
    assert roster.unknown_roster_children() == []


# ----- reading pickups off events -----

def test_pickup_is_the_end_of_the_event(six_calendars):
    """The end time is when a child is collected - the same reading that makes
    "Child_2 piano 5pm" mean picked up at 5."""
    pickups = roster.pickups_from_events([
        ("child_2_enrichment", _event("Child_2 5pm Piano", MONDAY, 16, 17, start_m=45)),
    ])
    assert len(pickups) == 1
    assert pickups[0].when == _at(MONDAY, 17)
    assert pickups[0].person == "Parent_1"
    assert pickups[0].child == "Child_2"


def test_all_day_events_create_no_pickup(six_calendars):
    """There is no moment to turn up at."""
    event = {"id": "e", "summary": "Child_2 Mom visiting",
             "start": {"date": MONDAY.isoformat()}, "end": {"date": MONDAY.isoformat()}}
    assert roster.pickups_from_events([("child_2_others", event)]) == []


def test_no_school_days_create_no_pickup(six_calendars):
    """Reuses the same test that decides whether a title shows a gate, so the
    two features can never disagree about whether a child is there."""
    pickups = roster.pickups_from_events([
        ("child_1_school", _event("Child_1 No school", MONDAY, 9, 10)),
        ("child_1_school", _event("Child_1 10am Gate 2 Public holiday", MONDAY, 9, 10)),
    ])
    assert pickups == []


def test_a_real_school_event_still_creates_one(six_calendars):
    """The narrow matching in is_no_pickup matters here too - a holiday concert
    is a real event that someone has to collect a child from."""
    pickups = roster.pickups_from_events([
        ("child_1_school", _event("Child_1 10am Gate 2 Holiday concert", MONDAY, 9, 10)),
    ])
    assert len(pickups) == 1


def test_pickup_override_is_read_from_the_description(six_calendars):
    pickups = roster.pickups_from_events([
        ("child_1_school", _event("Child_1 3pm Gate 2 Assembly", MONDAY, 14, 15,
                               description="Pickup: Grandparent_1\nType: School")),
    ])
    assert pickups[0].person == "Grandparent_1"


def test_unedited_schema_placeholder_is_not_a_person(six_calendars):
    """CLAUDE.md's schema shows "Pickup: [Parent_1 / Parent_2 / ...]". If that template
    is ever pasted in unedited it must fall through to the roster, not roster
    a person literally called "[Parent_1 / Parent_2 / ...]"."""
    pickups = roster.pickups_from_events([
        ("child_1_school", _event("Child_1 3pm Gate 2 Assembly", MONDAY, 14, 15,
                               description="Pickup: [Parent_1 / Parent_2 / Grandparent_1]")),
    ])
    assert pickups[0].person == "Helper_1"


# ----- the detector -----

def _pickups(*events):
    return roster.pickups_from_events(list(events))


def test_two_children_at_once_on_a_weekday_is_fine(six_calendars):
    """Parent_1 takes Child_2, the helper takes Child_1. Two adults, no problem - and
    this is the case that must NOT warn, or the family stops reading warnings."""
    pickups = _pickups(
        ("child_2_school", _event("Child_2 4pm End school", MONDAY, 15, 16, event_id="a")),
        ("child_1_enrichment", _event("Child_1 4pm Swimming", MONDAY, 15, 16, event_id="b")),
    )
    assert roster.find_clashes(pickups) == []


def test_two_children_at_once_on_a_saturday_clashes(six_calendars):
    """One adult. This is the case the whole file exists for."""
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
        ("child_1_enrichment", _event("Child_1 10am Swimming", SATURDAY, 9, 10, event_id="b")),
    )
    clashes = roster.find_clashes(pickups)
    assert [c.kind for c in clashes] == [roster.PICKUP_CLASH]
    assert clashes[0].person == "Parent_2"


def test_saturday_pickups_inside_the_buffer_clash(six_calendars):
    """15 minutes apart at different places is one person in two places."""
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
        ("child_1_enrichment", _event("Child_1 10.15am Swimming", SATURDAY, 9, 10, end_m=15, event_id="b")),
    )
    assert [c.kind for c in roster.find_clashes(pickups)] == [roster.PICKUP_CLASH]


def test_saturday_pickups_beyond_the_buffer_are_fine(six_calendars):
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
        ("child_1_enrichment", _event("Child_1 2pm Swimming", SATURDAY, 13, 14, event_id="b")),
    )
    assert roster.find_clashes(pickups) == []


def test_same_child_back_to_back_is_not_a_clash(six_calendars):
    """School then enrichment is the normal shape of a weekday - one adult
    drops her off and comes back. Flagging it would be noise."""
    pickups = _pickups(
        ("child_2_school", _event("Child_2 4pm End school", MONDAY, 15, 16, event_id="a")),
        ("child_2_enrichment", _event("Child_2 4.15pm Piano", MONDAY, 16, 16, end_m=15, event_id="b")),
    )
    assert roster.find_clashes(pickups) == []


def test_same_child_in_two_places_at_once_is_a_hard_conflict(six_calendars):
    """Impossible regardless of how many adults are free, so it is a distinct
    kind - somebody has to move an event, not just coordinate."""
    pickups = _pickups(
        ("child_2_school", _event("Child_2 4pm End school", MONDAY, 15, 16, event_id="a")),
        ("child_2_enrichment", _event("Child_2 4.30pm Piano", MONDAY, 15, 16, end_m=30, event_id="b")),
    )
    assert [c.kind for c in roster.find_clashes(pickups)] == [roster.CHILD_DOUBLE_BOOKED]


def test_a_long_event_overlapping_from_earlier_is_still_caught(six_calendars):
    """Sorting by END time does not bound the search: this event starts before
    the other pickup and ends hours after it. An early exit from the pair loop
    silently missed exactly this shape."""
    pickups = _pickups(
        ("child_2_school", _event("Child_2 10am End school", SATURDAY, 9, 10, event_id="a")),
        ("child_2_others", _event("Child_2 6pm Birthday party", SATURDAY, 9, 18, event_id="b")),
    )
    assert [c.kind for c in roster.find_clashes(pickups)] == [roster.CHILD_DOUBLE_BOOKED]


def test_an_override_can_resolve_a_saturday_clash(six_calendars):
    """The escape hatch: naming someone else on one event un-clashes the day,
    which is exactly the coordination the warning is asking for."""
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
        ("child_1_enrichment", _event("Child_1 10am Swimming", SATURDAY, 9, 10,
                                   description="Pickup: Grandparent_1", event_id="b")),
    )
    assert roster.find_clashes(pickups) == []


def test_buffer_is_configurable_per_call(six_calendars):
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
        ("child_1_enrichment", _event("Child_1 10.45am Swimming", SATURDAY, 9, 10, end_m=45, event_id="b")),
    )
    assert roster.find_clashes(pickups, buffer_minutes=30) == []
    assert len(roster.find_clashes(pickups, buffer_minutes=60)) == 1


def test_unassigned_pickups_are_flagged(six_calendars, monkeypatch):
    monkeypatch.setattr(roster, "PICKUP_ROSTER", {"weekday": {"child_2": "Parent_1"}})
    pickups = _pickups(
        ("child_1_enrichment", _event("Child_1 4pm Swimming", MONDAY, 15, 16, event_id="b")),
    )
    assert [c.kind for c in roster.find_clashes(pickups)] == [roster.UNASSIGNED]


def test_clashes_come_back_in_time_order(six_calendars):
    pickups = _pickups(
        ("child_2_enrichment", _event("Child_2 4pm Piano", SUNDAY, 15, 16, event_id="a")),
        ("child_1_enrichment", _event("Child_1 4pm Swimming", SUNDAY, 15, 16, event_id="b")),
        ("child_2_others", _event("Child_2 10am Party", SUNDAY, 9, 10, event_id="c")),
        ("child_1_others", _event("Child_1 10am Playdate", SUNDAY, 9, 10, event_id="d")),
    )
    clashes = roster.find_clashes(pickups)
    assert len(clashes) == 2
    assert clashes[0].when < clashes[1].when


# ----- the event description block -----

def test_describe_event_emits_only_what_it_has():
    assert roster.describe_event(pickup="Parent_1", event_type="School") == "Pickup: Parent_1\nType: School"


def test_describe_event_is_empty_when_it_knows_nothing():
    """Better than a block full of "Notes: [none]"."""
    assert roster.describe_event() == ""


def test_describe_event_round_trips_through_pickup_override():
    """What we write must be what we can read back, or the override silently
    stops working the first time an event is amended."""
    description = roster.describe_event(pickup="Grandparent_1", event_type="School", centre="Big School")
    assert roster.pickup_override({"description": description}) == "Grandparent_1"
