"""Changing WHO collects a child.

The roster is a default and the Pickup: line is the exception to it, but until
now nothing could set that line: the amend schema had no field for a person, so
"change the pickup to Other_carer" put Other_carer in `location` and produced an event
held "at Other_carer".

The thread these tests follow is one value - a named person - from the parsed
message, onto the card, into the description, and back out through the detector
that reads it. A break anywhere in that chain looks like the feature working.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import event_parser as ep
import main
import roster

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

TODAY = datetime.date(2026, 8, 5)
FRIDAY = datetime.date(2026, 8, 7)
SATURDAY = datetime.date(2026, 8, 8)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _at(date, hour, minute=0):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


def _event(summary, date, start_h, end_h, end_m=0, description=None, event_id="ev"):
    event = {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": _at(date, start_h).isoformat()},
        "end": {"dateTime": _at(date, end_h, end_m).isoformat()},
    }
    if description is not None:
        event["description"] = description
    return event


# ----- the parser -----

def test_a_named_person_becomes_a_pickup_change_not_a_location():
    """The actual bug. "at Other_carer" on the card was the visible symptom of the
    model having nowhere else to put a name."""
    result = ep._finalize_amend(
        {"action": "amend", "title": "HOSPITAL_1 appointment", "pickup": "Other_carer"}, TODAY, "src"
    )
    assert result["changes"] == {"pickup": "Other_carer"}
    assert "location" not in result["changes"]


def test_a_pickup_alone_is_enough_to_amend():
    """Without this the amend falls through to "what would you like to
    change?" - which is the dead end the user hit."""
    result = ep._finalize_amend(
        {"action": "amend", "title": "piano", "pickup": "Grandparent_1"}, TODAY, "src"
    )
    assert result["status"] == "ok"


def test_a_pickup_survives_alongside_a_real_move():
    result = ep._finalize_amend(
        {"action": "amend", "title": "piano", "new_start_time": "18:00", "pickup": "Other_carer"},
        TODAY, "src",
    )
    assert result["changes"]["pickup"] == "Other_carer"
    assert result["changes"]["start_time"] == "18:00"


# ----- rewriting the description -----

def test_setting_a_pickup_keeps_the_rest_of_the_description():
    """The description is shared with the family and may hold a Centre, a
    Notes line, or a sentence explaining an arrangement. Changing the carer
    must not silently delete the reason why."""
    before = "Pickup: Parent_1\nType: Others\nCentre: HOSPITAL_1\nNotes: bring the referral letter"
    after = roster.set_pickup(before, "Other_carer")
    assert "Pickup: Other_carer" in after
    assert "Pickup: Parent_1" not in after
    for kept in ("Type: Others", "Centre: HOSPITAL_1", "Notes: bring the referral letter"):
        assert kept in after


def test_a_pickup_is_added_when_the_event_never_had_one():
    """Most events on the calendar predate the stamping entirely."""
    after = roster.set_pickup("Notes: made by hand", "Other_carer")
    assert after.startswith("Pickup: Other_carer")
    assert "Notes: made by hand" in after


def test_an_empty_description_becomes_just_the_pickup():
    assert roster.set_pickup(None, "Other_carer") == "Pickup: Other_carer"


def test_only_the_first_pickup_line_is_rewritten():
    """A description with two Pickup: lines is already broken; rewriting both
    would silently agree with whichever was wrong."""
    after = roster.set_pickup("Pickup: Parent_1\nNotes: x\nPickup: Helper_1", "Other_carer")
    assert after.count("Pickup: Other_carer") == 1
    assert "Pickup: Helper_1" in after


def test_a_backslash_in_a_name_is_not_read_as_a_group_reference():
    """Names are user input and go through re.sub, where \\1 means something."""
    assert roster.set_pickup("Pickup: Parent_1", r"D\1ebbie") == r"Pickup: D\1ebbie"


def test_no_person_leaves_the_description_alone():
    assert roster.set_pickup("Pickup: Parent_1", "") == "Pickup: Parent_1"


# ----- the same person, typed differently -----

def test_the_same_name_in_different_case_is_one_person(six_calendars):
    """Carers are free text now, so "parent_1" and "Parent_1" both reach the detector.
    Matching exactly would read them as two adults and find no clash - a false
    negative in precisely the case this module exists for."""
    saturday_10 = ("child_2_enrichment",
                   _event("Child_2 10am Piano", SATURDAY, 9, 10,
                          description="Pickup: other_carer", event_id="a"))
    saturday_1015 = ("child_1_enrichment",
                     _event("Child_1 10.15am Swim", SATURDAY, 9, 10, end_m=15,
                            description="Pickup: Other_carer", event_id="b"))
    clashes = roster.find_clashes(roster.pickups_from_events([saturday_10, saturday_1015]))
    assert [c.kind for c in clashes] == [roster.PICKUP_CLASH]


def test_a_name_is_displayed_exactly_as_it_was_typed(six_calendars):
    """Loose matching is for the detector only - the family sees what they
    wrote, not a normalised version of it."""
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15)
    final = main._apply_changes(original, {"pickup": "grandparent_1"}, "child_2_others")
    assert "Pickup: grandparent_1" in final["description"]


# ----- what the amend actually does -----

def test_the_amended_event_carries_the_new_carer(six_calendars):
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15,
                      description="Pickup: Parent_1\nType: Others")
    final = main._apply_changes(original, {"pickup": "Other_carer"}, "child_2_others")
    assert "Pickup: Other_carer" in final["description"]
    assert "Type: Others" in final["description"]


def test_an_unrelated_amend_does_not_touch_the_description(six_calendars):
    """patch_event only writes a description when given one. A time change
    must not rebuild the block from the roster - that would wipe a
    "Pickup: Grandparent_1" a human typed into Google Calendar directly."""
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15,
                      description="Pickup: Grandparent_1\nType: Others")
    final = main._apply_changes(original, {"start_time": "11:00"}, "child_2_others")
    assert "description" not in final


def test_a_pickup_change_does_not_move_the_event(six_calendars):
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15)
    final = main._apply_changes(original, {"pickup": "Other_carer"}, "child_2_others")
    assert final["start"] == _at(FRIDAY, 10)
    assert final["end"] == _at(FRIDAY, 10, 15)
    assert final["title"] == "Child_2 10.15am HOSPITAL_1 appointment"


def test_what_is_written_reads_back_as_an_override(six_calendars):
    """The stamped line is the same field the detector reads. A round-trip
    failure here would unhook the change from everything downstream while
    still looking correct on the card."""
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15)
    final = main._apply_changes(original, {"pickup": "Other_carer"}, "child_2_others")
    assert roster.pickup_override({"description": final["description"]}) == "Other_carer"


# ----- the card -----

def _amend_card(monkeypatch, original, changes, calendar_key, existing=()):
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: list(existing))
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    asyncio.run(main._show_amend_confirm(update, 1, original, changes, calendar_key))
    return update.message.reply_text.await_args.args[0]


def test_the_card_shows_the_pickup_changing(six_calendars, monkeypatch):
    """The pickup lives in the description, not the title, so without this the
    card would look identical to one that changed nothing - and a
    confirmation you cannot read is not a confirmation."""
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15,
                      description="Pickup: Parent_1\nType: Others")
    text = _amend_card(monkeypatch, original, {"pickup": "Other_carer"}, "child_2_others")
    assert "Pickup: Parent\\_1 → Other\\_carer" in text


def test_the_card_falls_back_to_the_roster_for_the_old_carer(six_calendars, monkeypatch):
    """An event with no Pickup: line still has someone who would turn up.
    Showing "nobody → Other_carer" would misrepresent the change."""
    original = _event("Child_2 10.15am HOSPITAL_1 appointment", FRIDAY, 10, 10, end_m=15)
    text = _amend_card(monkeypatch, original, {"pickup": "Other_carer"}, "child_2_others")
    assert "Pickup: Parent\\_1 → Other\\_carer" in text


def test_naming_someone_else_clears_the_clash(six_calendars, monkeypatch):
    """Naming another adult is the usual way to FIX a clash. A card that kept
    warning about the problem the user had just solved would be worse than one
    that never warned."""
    child_2_piano = ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="x"))
    original = _event("Child_1 10.15am Swimming", SATURDAY, 9, 10, end_m=15, event_id="y")

    unchanged = _amend_card(monkeypatch, original, {"title": "Child_1 10.15am Swim"},
                            "child_1_enrichment", [child_2_piano])
    assert "Parent\\_2 is needed twice" in unchanged

    reassigned = _amend_card(monkeypatch, original, {"pickup": "Other_carer"},
                             "child_1_enrichment", [child_2_piano])
    assert "⚠️" not in reassigned


def _create_result(child_key, title, date, start_h, end_h):
    occurrence = {"all_day": False, "start": _at(date, start_h), "end": _at(date, end_h), "date": None}
    return {"action": "create", "status": "ok", "title": title, "location": None,
            "notes": None, "calendar_key": child_key, "occurrences": [occurrence], **occurrence}


# ----- the nudge (2026-08-07: discussed instead of a new button) -----

def test_a_clash_on_the_create_card_points_at_the_existing_fix(six_calendars, monkeypatch):
    """Phase C was scoped down to this on purpose - "suggest a new time" and
    "one-tap reassign" were both weighed against just pointing at the change
    flow that already exists, and neither pulled its weight.

    On a Sunday (Parent_1 collects everyone) rather than a Saturday, so the clash
    shows straight away - a Saturday card asks who is collecting first."""
    sunday = datetime.date(2026, 8, 16)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "find_conflicts_for_child", lambda *a, **k: [])
    monkeypatch.setattr(
        ct, "list_events_range_across",
        lambda s, e, keys=None: [("child_2_enrichment", _event("Child_2 10am Piano", sunday, 9, 10, event_id="x"))],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    result = _create_result("child_1_enrichment", "Child_1 10.15am Swim", sunday, 9, 10)
    result["end"] = result["occurrences"][0]["end"] = _at(sunday, 10, 15)

    asyncio.run(main._present_create(update, 1, result))

    text = update.message.reply_text.await_args.args[0]
    assert "✏️ Change something" in text
    assert "change the pickup to" in text.lower()


def test_a_clean_card_carries_no_nudge(six_calendars, monkeypatch):
    """The hint is conditional on a clash existing - a card with nothing to
    fix should not tell the user how to fix something."""
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "find_conflicts_for_child", lambda *a, **k: [])
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    del update.edit_message_text

    asyncio.run(main._present_create(update, 1, _create_result("child_1_enrichment", "Child_1 4pm Swim", SATURDAY, 15, 16)))

    text = update.message.reply_text.await_args.args[0]
    assert "Change something" not in text


def test_pickups_points_at_the_message_based_fix(six_calendars, monkeypatch):
    """/pickups has no buttons at all, unlike a confirm card - so the hint
    has to name the actual mechanism (message the bot) rather than a button
    that does not exist on this screen."""
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(
        ct, "list_events_range_across",
        lambda start, end, keys=None: [
            ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
            ("child_1_enrichment", _event("Child_1 10.15am Swim", SATURDAY, 9, 10, end_m=15, event_id="b")),
        ],
    )
    update = MagicMock()
    update.message.reply_text = AsyncMock()

    asyncio.run(main.pickups(update, MagicMock()))

    text = update.message.reply_text.await_args.args[0]
    assert "message" in text.lower()
    assert "pickup" in text.lower()


# ----- create -----

def test_a_stated_carer_beats_the_roster_on_a_new_event(six_calendars):
    pending = {"calendar_key": "child_2_school", "title": "Child_2 4pm End school", "notes": None,
               "pickup": "Other_carer", "start": _at(FRIDAY, 15), "end": _at(FRIDAY, 16)}
    assert "Pickup: Other_carer" in main._event_description(pending)


def test_the_roster_still_applies_when_nobody_is_named(six_calendars):
    pending = {"calendar_key": "child_2_school", "title": "Child_2 4pm End school", "notes": None,
               "start": _at(FRIDAY, 15), "end": _at(FRIDAY, 16)}
    assert "Pickup: Parent_1" in main._event_description(pending)


def test_a_stated_carer_applies_to_every_date(six_calendars):
    """"Other_carer is getting her" is a statement about the arrangement, not about
    one Tuesday - so unlike the roster it does not vary by day."""
    pending = {"calendar_key": "child_2_enrichment", "title": "Child_2 10am Piano",
               "notes": None, "pickup": "Other_carer"}
    saturday = {"start": _at(SATURDAY, 9), "end": _at(SATURDAY, 10)}
    friday = {"start": _at(FRIDAY, 9), "end": _at(FRIDAY, 10)}
    assert "Pickup: Other_carer" in main._event_description(pending, saturday)
    assert "Pickup: Other_carer" in main._event_description(pending, friday)


# ----- "who is picking Child_1 up?" (UAT 8) -----

SUNDAY = datetime.date(2026, 8, 16)


def _ask(monkeypatch, result, events):
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: list(events))
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._handle_query(update, result))
    return update.message.reply_text.await_args.args[0]


def test_a_who_question_is_answered_with_a_name(six_calendars, monkeypatch):
    """Reported from UAT: "who is picking child_1 up on 16 aug" returned the
    event, not the person. CLAUDE.md Flow 4 has listed this as a query the
    bot answers since the first commit; every query returned the same list."""
    text = _ask(
        monkeypatch,
        {"action": "query", "status": "ok", "start": SUNDAY, "end": SUNDAY,
         "calendar_key": ["child_1_enrichment"], "asks_pickup": True},
        [("child_1_enrichment", _event("Child_1 11am Replacement", SUNDAY, 10, 11, event_id="a"))],
    )
    assert "Parent\\_1" in text          # Sunday -> Parent\\_1, per the roster
    assert "Replacement" in text   # still says which event, so it is checkable


def test_a_what_question_still_lists_events(six_calendars, monkeypatch):
    """The flag must not swallow ordinary queries."""
    monkeypatch.setattr(ct, "list_events_range_all",
                        lambda s, e, keys=None: [("Child_1 — Enrichment",
                                                  _event("Child_1 11am Replacement", SUNDAY, 10, 11))])
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._handle_query(update, {
        "action": "query", "status": "ok", "start": SUNDAY, "end": SUNDAY,
        "calendar_key": ["child_1_enrichment"], "asks_pickup": False}))

    assert "Parent_1" not in update.message.reply_text.await_args.args[0]


def test_a_who_answer_uses_the_same_roster_as_pickups(six_calendars, monkeypatch):
    """Saturday belongs to Parent_2. Two ways of deciding who collects a child
    would eventually disagree, and disagreeing with itself about who is
    fetching a seven-year-old is the worst thing this bot could do."""
    text = _ask(
        monkeypatch,
        {"action": "query", "status": "ok", "start": SATURDAY, "end": SATURDAY,
         "calendar_key": None, "asks_pickup": True},
        [("child_1_enrichment", _event("Child_1 10.15am Piano", SATURDAY, 9, 10, end_m=15, event_id="a"))],
    )
    assert "Parent\\_2" in text


def test_a_who_answer_honours_a_stated_pickup(six_calendars, monkeypatch):
    text = _ask(
        monkeypatch,
        {"action": "query", "status": "ok", "start": SUNDAY, "end": SUNDAY,
         "calendar_key": None, "asks_pickup": True},
        [("child_1_enrichment", _event("Child_1 11am Replacement", SUNDAY, 10, 11,
                                    description="Pickup: Grandparent_1", event_id="a"))],
    )
    assert "Grandparent\\_1" in text and "Parent\\_1" not in text


def test_a_who_answer_flags_a_clash(six_calendars, monkeypatch):
    text = _ask(
        monkeypatch,
        {"action": "query", "status": "ok", "start": SATURDAY, "end": SATURDAY,
         "calendar_key": None, "asks_pickup": True},
        [("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
         ("child_1_enrichment", _event("Child_1 10.15am Swim", SATURDAY, 9, 10, end_m=15, event_id="b"))],
    )
    assert "Parent\\_2 is needed twice" in text


def test_nothing_to_collect_falls_back_to_the_event_list(six_calendars, monkeypatch):
    """An all-day event needs no pickup. Answering "nobody" would be wrong -
    better to show what IS on and let the human read it."""
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(ct, "list_events_range_all", lambda s, e, keys=None: [])
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._handle_query(update, {
        "action": "query", "status": "ok", "start": SUNDAY, "end": SUNDAY,
        "calendar_key": None, "asks_pickup": True}))

    assert "Nothing on" in update.message.reply_text.await_args.args[0]


def test_a_who_question_with_no_day_still_answers(six_calendars, monkeypatch):
    """"who is picking child_1 up?" names no day. Falling through to an event
    list is a poor answer to a who-question."""
    text = _ask(
        monkeypatch,
        {"action": "query", "status": "ok", "start": None, "end": None,
         "calendar_key": None, "asks_pickup": True},
        [("child_1_enrichment", _event("Child_1 11am Replacement", SUNDAY, 10, 11, event_id="a"))],
    )
    assert "Parent\\_1" in text
