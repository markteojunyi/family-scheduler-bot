"""Whether a child can physically get from one event to the next.

The case that motivated this: Child_1 finishes school at 3pm and her next class
starts at 3.15pm across town. The events do not overlap, and one child never
competes with herself for a carer, so every existing check passed it in
silence - reported 2026-08-16.

Fixtures use the family's real places (seeded the same day), because a test
built on invented ones would stop proving anything the moment the table
changed underneath it.
"""
import asyncio
import datetime
import os
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import db
import main
import places
import roster

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
MONDAY = datetime.date(2026, 9, 7)


@pytest.fixture
def fresh_db(tmp_path):
    """A temp database per test - never the real one. Same shape as
    test_db.py's fixture."""
    old_path = db.DB_PATH
    db.DB_PATH = str(tmp_path / "test.db")
    db._local = threading.local()
    db.init_db()
    yield
    db._local = threading.local()
    db.DB_PATH = old_path


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


@pytest.fixture
def everyone_drives(monkeypatch):
    """Removes the transport variable from a test that is about something
    else. Without it, Child_1's weekday carer is Helper_1, who is on public
    transport, and every figure quietly gains 20 minutes."""
    monkeypatch.setattr(roster, "PICKUP_ROSTER",
                        {"weekday": {"*": "Parent_1"}, "saturday": {"*": "Parent_1"}, "sunday": {"*": "Parent_1"}})


def _at(date, hour, minute=0):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


def _event(summary, date, start_h, start_m, end_h, end_m, event_id="ev", **extra):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": _at(date, start_h, start_m).isoformat()},
        "end": {"dateTime": _at(date, end_h, end_m).isoformat()},
        **extra,
    }


SCHOOL_END = ("child_1_school", _event("Child_1 3pm Gate 2 End school", MONDAY, 14, 45, 15, 0, "s"))
CENTRE_1 = ("child_1_enrichment", _event("Child_1 4pm Centre_1", MONDAY, 15, 15, 16, 0, "b"))


def _travel(minutes):
    """A lookup that answers `minutes` for every pair, so a test can state the
    one number it is about instead of building a table."""
    return lambda a, b, people=(): minutes


def _unknown(a, b, people=()):
    return None


# ----- resolving an event to a place -----

def test_a_school_event_is_at_that_child_s_school(six_calendars):
    """No location, no Centre: line - just the calendar it lives on. This is
    what makes the feature work on events already in the calendar."""
    assert places.place_for(*SCHOOL_END) == "child_1-school"


def test_the_two_children_s_schools_are_different_places(six_calendars):
    child_2 = ("child_2_school", _event("Child_2 3pm End school", MONDAY, 14, 45, 15, 0))
    assert places.place_for(*child_2) != places.place_for(*SCHOOL_END)


def test_an_enrichment_activity_resolves_through_its_alias(six_calendars):
    assert places.place_for(*CENTRE_1) == "centre_1"


def test_the_real_swimming_title_resolves(six_calendars):
    """Taken verbatim from the live calendar - "Replacement at the Competition
    Squad time slot" has to reach `swimming` on the `squad` alias, or the one
    swimming event the family actually has is invisible to this."""
    swim = ("child_1_enrichment",
            _event("Child_1 11am Replacement at the Competition Squad time slot",
                   MONDAY, 10, 0, 11, 0))
    assert places.place_for(*swim) == "swimming"


def test_the_real_centre_1_title_resolves(six_calendars):
    """Also verbatim: "Make up centre_1" is a make-up class at Centre_1."""
    made_up = ("child_1_enrichment",
               _event("Child_1 6.45pm - 9.15pm Make up centre_1", MONDAY, 18, 45, 21, 15))
    assert places.place_for(*made_up) == "centre_1"


def test_a_stated_location_beats_the_calendar(six_calendars):
    """Somebody wrote it on the event, so it is evidence; the calendar default
    is only a default."""
    key, event = SCHOOL_END
    moved = (key, dict(event, location="Swimming"))
    assert places.place_for(*moved) == "swimming"


def test_a_centre_line_is_read_when_there_is_no_location(six_calendars):
    key, event = CENTRE_1
    tagged = (key, dict(event, description="Pickup: Parent_1\nCentre: Swimming"))
    assert places.place_for(*tagged) == "swimming"


def test_the_real_hospital_1_location_resolves(six_calendars):
    """The live event's location is "SPECIALIST CLINIC M CHILDREN'S TOWER
    LEVEL 1", which stored itself as a place of that whole name. The
    children's-tower alias is what folds it into `hospital_1` so a future appointment
    written differently lands on the same place."""
    appt = ("child_2_others",
            _event("Child_2 10.15am HOSPITAL_1 appointment", MONDAY, 10, 0, 10, 15,
                   location="SPECIALIST CLINIC M CHILDREN'S TOWER LEVEL 1"))
    assert places.place_for(*appt) == "hospital_1"


def test_an_others_event_can_name_its_place(six_calendars):
    """Child_1's maths tuition happens at home, and `others` events were not
    alias-matched at first - which would have left the one transition the
    family actually described unresolvable."""
    tuition = ("child_1_others", _event("Child_1 5pm Maths tuition", MONDAY, 16, 0, 17, 0))
    assert places.place_for(*tuition) == "home"


def test_an_unrecognised_location_still_becomes_a_place(six_calendars):
    """The family typed it, so it is real even though places.py has never
    heard of it. This is how places get in without editing anything."""
    key, event = CENTRE_1
    somewhere = (key, dict(event, location="Bishan Community Club"))
    assert places.place_for(*somewhere) == "bishan community club"


def test_a_school_title_is_not_dragged_off_to_a_centre(six_calendars):
    """"squad" is a swimming alias, but a school event is at school - matching
    activities against centres is for enrichment and others only."""
    odd = ("child_1_school", _event("Child_1 3pm Squad assembly", MONDAY, 14, 45, 15, 0))
    assert places.place_for(*odd) == "child_1-school"


# ----- three ballet studios, told apart -----

def test_child_2_s_bare_ballet_class_resolves_to_her_own_studio(six_calendars):
    """Verbatim from the live calendar: "Child_2 7.15pm Ballet class" names no
    branch. Child_2 has exactly one studio, so it can safely mean Studio_1."""
    child_2 = ("child_2_enrichment", _event("Child_2 7.15pm Ballet class", MONDAY, 19, 0, 19, 15))
    assert places.place_for(*child_2) == "ballet-studio_1"


def test_child_1_s_bare_ballet_class_resolves_to_nothing(six_calendars):
    """Child_1 has two studios told apart only by branch name, so an unqualified
    title is genuinely ambiguous. Resolving to nothing is the safe answer - no
    place means no travel check, and the bot never warns on a guess."""
    child_1 = ("child_1_enrichment", _event("Child_1 7.15pm Ballet class", MONDAY, 19, 0, 19, 15))
    assert places.place_for(*child_1) is None


def test_a_named_branch_resolves_for_the_child_who_attends_it(six_calendars):
    child_1 = ("child_1_enrichment", _event("Child_1 7pm Ballet at Studio_2", MONDAY, 18, 0, 19, 0))
    assert places.place_for(*child_1) == "ballet-studio_2"


def test_child_1_s_studio_is_not_offered_to_child_2(six_calendars):
    """Studio_2 is scoped to Child_1. If Child_2 ever starts attending it, the fix
    is to drop the `child` key - not to let the scope leak."""
    assert places.match_known("Ballet at Studio_2", child="Child_2") != "ballet-studio_2"


# ----- the arithmetic -----

def test_the_same_place_needs_no_travel():
    assert places.travel_minutes("centre_1", "centre_1") == 0


def test_an_unknown_pair_is_unknown_not_zero():
    """Returning 0 would silently declare every unmapped journey instant,
    which is the failure mode that makes a safety check worthless."""
    assert places.travel_minutes("nowhere-a", "nowhere-b") is None


def test_a_journey_reads_the_same_in_both_directions():
    """Confirmed by the family: the return leg takes the same time as the
    outbound one, so one entry serves both."""
    assert (places.travel_minutes("child_1-school", "centre_1", people=["Parent_1"])
            == places.travel_minutes("centre_1", "child_1-school", people=["Parent_1"]))


def test_the_seeded_table_holds_the_family_s_own_numbers():
    """A spot check that the answers given on 2026-08-16 are what is stored,
    by car. If this fails, the table was edited without meaning to."""
    assert places.travel_minutes("child_1-school", "child_2-school", people=["Parent_1"]) == 10
    assert places.travel_minutes("child_1-school", "ballet-studio_2", people=["Parent_1"]) == 35
    assert places.travel_minutes("centre_1", "ballet-studio_1", people=["Parent_1"]) == 5


def test_a_seeded_journey_beats_a_learned_one(monkeypatch):
    """places.py is the file the user is told to tune, so editing it has to
    actually take effect - a learned value quietly overriding a hand-written
    one would be a genuinely baffling bug."""
    pair = places.pair_key("child_1-school", "centre_1")
    monkeypatch.setitem(places.TRAVEL_MINUTES, pair, 12)
    assert places.travel_minutes("child_1-school", "centre_1", {pair: 45}, ["Parent_1"]) == 12


# ----- how the carer travels -----

def test_a_driver_gets_the_table_time():
    assert places.travel_minutes("child_1-school", "centre_1", people=["Parent_1"]) == 20


def test_public_transport_adds_twenty_minutes():
    """Helper_1 covers Child_1's weekday pickups, so this is not an edge case - it
    is most of Child_1's week."""
    assert places.travel_minutes("child_1-school", "centre_1", people=["Helper_1"]) == 40


def test_a_carer_nobody_listed_is_assumed_to_be_on_a_bus():
    """The cautious default: an unfamiliar carer makes the bot more likely to
    warn, not less."""
    assert places.travel_minutes("child_1-school", "centre_1", people=["A Friend"]) == 40


def test_the_slower_carer_decides_a_handover():
    """When two adults are involved, the pessimistic read is the safe one."""
    assert places.travel_minutes("child_1-school", "centre_1", people=["Parent_1", "Helper_1"]) == 40


def test_a_carer_is_matched_however_it_was_typed():
    """The roster says "Helper_1"; somebody typing "change the pickup to helper"
    gets "helper_1" recorded verbatim, as they should."""
    assert places.transport_for("helper_1") == places.transport_for("Helper_1")


def test_a_walk_inside_one_building_is_not_lengthened_by_a_bus():
    """Centre_1 and Ballet (Studio_1) share a building, 5 minutes apart. Adding
    20 would claim a 25-minute trip between two units of one block."""
    assert places.travel_minutes("centre_1", "ballet-studio_1", people=["Helper_1"]) == 5


# ----- the detector -----

def test_the_tight_transition_that_used_to_pass_silently(six_calendars):
    """3pm finish, 3.15pm start, 30 minutes apart. No overlap and one child,
    so nothing before this flagged it."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    clashes = roster.find_clashes(pickups, travel=_travel(30))
    assert [c.kind for c in clashes] == [roster.TIGHT_TRANSITION]


def test_enough_time_is_not_a_clash(six_calendars):
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    assert not [c for c in roster.find_clashes(pickups, travel=_travel(10))
                if c.kind == roster.TIGHT_TRANSITION]


def test_an_unknown_journey_is_never_a_clash(six_calendars):
    """A warning built on a number nobody gave is worse than no warning. The
    bot asks instead - see the unanswered_journeys tests below."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    assert not [c for c in roster.find_clashes(pickups, travel=_unknown)
                if c.kind == roster.TIGHT_TRANSITION]


def test_travel_is_not_checked_without_a_lookup(six_calendars):
    """Omitting `travel` has to behave exactly as this did before places
    existed, or every caller that has not been updated changes behaviour."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    assert not [c for c in roster.find_clashes(pickups) if c.kind == roster.TIGHT_TRANSITION]


def test_two_children_are_not_a_transition(six_calendars):
    """A transition is one child travelling. Two children at once is a pickup
    clash, which is a different problem with a different answer."""
    child_2 = ("child_2_enrichment", _event("Child_2 4pm Ballet class", MONDAY, 15, 15, 16, 0, "e"))
    pickups = roster.pickups_from_events([SCHOOL_END, child_2])
    assert not [c for c in roster.find_clashes(pickups, travel=_travel(90))
                if c.kind == roster.TIGHT_TRANSITION]


def test_an_overlap_is_reported_as_a_double_booking_not_a_transition(six_calendars):
    """Two events at the same time is a strictly worse problem, and saying it
    twice in two registers would just be noise."""
    overlapping = ("child_1_enrichment", _event("Child_1 4pm Centre_1", MONDAY, 14, 50, 16, 0, "b"))
    pickups = roster.pickups_from_events([SCHOOL_END, overlapping])
    kinds = {c.kind for c in roster.find_clashes(pickups, travel=_travel(30))}
    assert roster.CHILD_DOUBLE_BOOKED in kinds
    assert roster.TIGHT_TRANSITION not in kinds


def test_the_warning_names_both_places_and_the_gap(six_calendars):
    """"Conflict" is not actionable. The answer is usually to move one of
    them, which needs to know which two, how short, and by how much."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    clash = [c for c in roster.find_clashes(pickups, travel=_travel(30))
             if c.kind == roster.TIGHT_TRANSITION][0]
    text = roster.describe_clash(clash)
    assert "15 min" in text and "school" in text and "Centre_1" in text
    assert "needs 30" in text  # how much short, not just that it is short


def test_the_warning_says_when_the_carer_is_on_a_bus(six_calendars):
    """The same journey is 20 minutes longer for Helper_1 than for Parent_1, so a
    warning omitting the mode looks wrong to whoever knows the drive."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])  # Monday -> Helper_1
    clash = [c for c in roster.find_clashes(pickups, travel=_travel(30))
             if c.kind == roster.TIGHT_TRANSITION][0]
    assert "by public transport" in roster.describe_clash(clash)


def test_the_warning_stays_plain_for_a_driver(six_calendars, everyone_drives):
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    clash = [c for c in roster.find_clashes(pickups, travel=_travel(30))
             if c.kind == roster.TIGHT_TRANSITION][0]
    assert "by public transport" not in roster.describe_clash(clash)


def test_the_real_table_catches_child_1_s_weekday_run(six_calendars):
    """End to end on the seeded numbers: school 3pm, Centre_1 3.15pm. By car
    that is 20 min against a 15 min gap - already tight. Helper_1 is on public
    transport, so it is really 40."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    travel = lambda a, b, people=(): places.travel_minutes(a, b, None, people)
    clash = [c for c in roster.find_clashes(pickups, travel=travel)
             if c.kind == roster.TIGHT_TRANSITION][0]
    assert clash.needed == 40


# ----- the same-person buffer, now place-aware -----

def test_two_pickups_at_the_same_place_no_longer_clash(six_calendars, everyone_drives):
    """The blanket 45-minute buffer warned about one adult collecting two
    children 20 minutes apart at the same centre. That is fine, and a false
    positive on a safety warning is what teaches people to ignore it."""
    child_1 = ("child_1_enrichment", _event("Child_1 4pm Centre_1", MONDAY, 15, 0, 16, 0, "a"))
    child_2 = ("child_2_enrichment", _event("Child_2 4.20pm Centre_1", MONDAY, 15, 0, 16, 20, "b"))
    pickups = roster.pickups_from_events([child_1, child_2])
    travel = lambda a, b, people=(): 0 if a == b else None
    assert not [c for c in roster.find_clashes(pickups, travel=travel)
                if c.kind == roster.PICKUP_CLASH]


def test_a_far_apart_pair_still_clashes(six_calendars, everyone_drives):
    child_1 = ("child_1_enrichment", _event("Child_1 4pm Centre_1", MONDAY, 15, 0, 16, 0, "a"))
    child_2 = ("child_2_enrichment", _event("Child_2 4.20pm Ballet class", MONDAY, 15, 0, 16, 20, "b"))
    pickups = roster.pickups_from_events([child_1, child_2])
    assert [c for c in roster.find_clashes(pickups, travel=_travel(45))
            if c.kind == roster.PICKUP_CLASH]


def test_an_unknown_pair_keeps_the_flat_buffer(six_calendars, everyone_drives):
    """Today's behaviour is the fallback, so nothing gets quieter just because
    a journey has not been timed yet."""
    child_1 = ("child_1_enrichment", _event("Child_1 4pm Centre_1", MONDAY, 15, 0, 16, 0, "a"))
    child_2 = ("child_2_others", _event("Child_2 4.20pm Dentist", MONDAY, 15, 0, 16, 20, "b"))
    pickups = roster.pickups_from_events([child_1, child_2])
    assert [c for c in roster.find_clashes(pickups, travel=_unknown)
            if c.kind == roster.PICKUP_CLASH]


# ----- what to ask about -----

def test_an_untimed_journey_is_offered_as_a_question(six_calendars):
    found = roster.unanswered_journeys([SCHOOL_END], [CENTRE_1], travel=_unknown)
    assert len(found) == 1
    assert set(found[0]) == {"child_1-school", "centre_1"}


def test_the_question_reads_in_the_direction_the_journey_is_made(six_calendars):
    """Storage sorts the pair, but "Centre_1 to school" describes a day that
    runs backwards. The child leaves school and goes to Centre_1."""
    found = roster.unanswered_journeys([SCHOOL_END], [CENTRE_1], travel=_unknown)
    assert found == [("child_1-school", "centre_1")]


def test_a_journey_already_timed_is_not_asked_about_again(six_calendars):
    assert roster.unanswered_journeys([SCHOOL_END], [CENTRE_1], travel=_travel(20)) == []


def test_the_seeded_table_means_the_family_is_not_asked_again(six_calendars):
    """The whole point of seeding: child_1-school to Centre_1 is answered, so this
    journey never produces a question."""
    travel = lambda a, b, people=(): places.travel_minutes(a, b, None, people)
    assert roster.unanswered_journeys([SCHOOL_END], [CENTRE_1], travel=travel) == []


def test_a_generous_gap_is_not_worth_asking_about(six_calendars):
    """Three hours between two events makes the travel time irrelevant however
    far apart the places are, and asking anyway is how a bot gets muted."""
    evening = ("child_1_enrichment", _event("Child_1 8pm Centre_1", MONDAY, 19, 0, 20, 0, "b"))
    assert roster.unanswered_journeys([SCHOOL_END], [evening], travel=_unknown) == []


def test_a_pair_untouched_by_the_proposal_is_not_asked_about(six_calendars):
    """The card is asking about one thing. Two events already on the calendar
    are not this change's business."""
    other = ("child_1_others", _event("Child_1 5pm Maths tuition", MONDAY, 16, 15, 17, 0, "d"))
    found = roster.unanswered_journeys([SCHOOL_END, CENTRE_1], [other], travel=_unknown)
    assert ("child_1-school", "centre_1") not in found
    assert places.pair_key("child_1-school", "centre_1") not in found


def test_the_same_place_twice_is_never_asked_about(six_calendars):
    later = ("child_1_enrichment", _event("Child_1 5pm Centre_1", MONDAY, 16, 15, 17, 0, "b2"))
    assert roster.unanswered_journeys([CENTRE_1], [later], travel=_unknown) == []


# ----- storing an answer -----

def test_an_answer_is_remembered(fresh_db):
    pair = places.pair_key("child_1-school", "somewhere-new")
    db.set_travel_minutes(pair, 30, ADMIN_ID)
    assert db.all_travel_times()[pair] == 30


def test_answering_twice_corrects_rather_than_stacks(fresh_db):
    """Re-answering means the first number was wrong. Averaging two answers
    would produce a figure nobody gave."""
    pair = places.pair_key("child_1-school", "somewhere-new")
    db.set_travel_minutes(pair, 30, ADMIN_ID)
    db.set_travel_minutes(pair, 15, ADMIN_ID)
    assert db.all_travel_times() == {pair: 15}


def test_a_wrong_answer_can_be_taken_back(fresh_db):
    pair = places.pair_key("child_1-school", "somewhere-new")
    db.set_travel_minutes(pair, 30, ADMIN_ID)
    assert db.forget_travel_minutes(pair) == db.ANSWERED_ON_CARD
    assert db.all_travel_times() == {}


# ----- the confirm card -----

# Captured before any test stubs them. all_travel_times delegates to
# travel_times_by_source, so stubbing that one also hijacks the helper a test
# uses to check what it stored - which would silently read {} and pass nothing.
_REAL_BY_SOURCE = db.travel_times_by_source


def _REAL_TRAVEL_TIMES():
    overrides, answers = _REAL_BY_SOURCE()
    return {**answers, **overrides}


def _mock_update(chat_id=1):
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    return update


def _centre_1_proposal():
    """The 3.15pm start, as a create card would see it."""
    occurrence = {"all_day": False, "start": _at(MONDAY, 15, 15),
                  "end": _at(MONDAY, 16, 0), "date": None}
    return {"action": "create", "status": "ok", "title": "Child_1 4pm Centre_1",
            "location": None, "notes": None, "calendar_key": "child_1_enrichment",
            "occurrences": [occurrence], **occurrence}


def _stub_calendar(monkeypatch, existing, learned=None, seeded=None):
    """Only the calendar read and the two travel tables are stubbed. Place
    resolution and the detector underneath stay real, so these fail if the
    arithmetic changes.

    The seeded table is emptied by default so a card test states its own
    numbers - otherwise the real child_1-school/Centre_1 entry would answer a
    question the test is trying to ask.
    """
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across",
                        lambda start, end, keys=None: list(existing))
    monkeypatch.setattr(main.db, "all_travel_times", lambda: dict(learned or {}))
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, dict(learned or {})))
    monkeypatch.setattr(places, "TRAVEL_MINUTES", dict(seeded or {}))


def _use_real_travel_table(monkeypatch):
    """Undo both stubs, so a tap test reads back what it actually stored."""
    monkeypatch.setattr(main.db, "all_travel_times", _REAL_TRAVEL_TIMES)
    monkeypatch.setattr(main.db, "travel_times_by_source", _REAL_BY_SOURCE)


def test_the_card_warns_when_the_journey_is_known_and_too_short(six_calendars, monkeypatch, everyone_drives):
    """The whole point: it lands where a tap can still fix it."""
    pair = places.pair_key("child_1-school", "centre_1")
    _stub_calendar(monkeypatch, [SCHOOL_END], learned={pair: 30})
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    text = update.message.reply_text.await_args.args[0]
    assert "⚠️" in text and "15 min" in text


def test_the_card_asks_when_the_journey_is_unknown(six_calendars, monkeypatch):
    _stub_calendar(monkeypatch, [SCHOOL_END])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    text = update.message.reply_text.await_args.args[0]
    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    assert "How long does" in text
    assert any(b.callback_data.startswith("trav:")
               for row in keyboard.inline_keyboard for b in row)


def test_the_question_asks_for_the_car_time(six_calendars, monkeypatch):
    """Every stored number is a car time and the bus penalty is added on top,
    so an answer given with a bus journey in mind would be counted twice."""
    _stub_calendar(monkeypatch, [SCHOOL_END])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    assert "by car" in update.message.reply_text.await_args.args[0]


def test_the_question_never_takes_the_confirm_button_away(six_calendars, monkeypatch):
    """The bot not knowing a distance must not stand between the family and a
    write they came here to make."""
    _stub_calendar(monkeypatch, [SCHOOL_END])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "confirm" in data and "cancel" in data


def test_no_question_when_nothing_is_close_enough_to_matter(six_calendars, monkeypatch):
    _stub_calendar(monkeypatch, [])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    assert "How long does" not in update.message.reply_text.await_args.args[0]


def test_the_seeded_table_answers_without_asking(six_calendars, monkeypatch):
    """With the real table in place the family is warned, not questioned - the
    difference seeding was meant to make."""
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across",
                        lambda start, end, keys=None: [SCHOOL_END])
    monkeypatch.setattr(main.db, "all_travel_times", dict)
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    text = update.message.reply_text.await_args.args[0]
    assert "How long does" not in text
    assert "⚠️" in text


# ----- answering the question -----

def _tap(monkeypatch, data, pending):
    """Drive the real callback handler for one button press."""
    query = MagicMock()
    query.data = data
    query.from_user.id = ADMIN_ID
    query.message.chat_id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = 1
    monkeypatch.setattr(main.db, "peek_pending", lambda chat_id, kind: pending)
    asyncio.run(main.handle_callback(update, MagicMock()))
    return query


def _pending():
    return {"kind": "create", "journey": ["child_1-school", "centre_1"],
            "source_text": None, **_centre_1_proposal()}


def test_a_tap_stores_the_bucket_it_offered(six_calendars, monkeypatch, fresh_db):
    _stub_calendar(monkeypatch, [SCHOOL_END])

    _tap(monkeypatch, "trav:1", _pending())  # "15-30 min" -> stores the top, 30

    assert _REAL_TRAVEL_TIMES()[places.pair_key("child_1-school", "centre_1")] == 30


def test_answering_does_not_consume_the_pending_write(six_calendars, monkeypatch, fresh_db):
    """A question about a route must leave the card confirmable. If this ever
    pops instead of peeking, the confirm silently expires."""
    _stub_calendar(monkeypatch, [SCHOOL_END])
    popped = []
    monkeypatch.setattr(main.db, "pop_pending",
                        lambda chat_id, kind: popped.append(kind))

    _tap(monkeypatch, "trav:0", _pending())

    assert popped == []


def test_an_out_of_range_bucket_is_refused(six_calendars, monkeypatch, fresh_db):
    """callback_data is client-controlled, same as cat:/kid:/create_opt:."""
    _stub_calendar(monkeypatch, [SCHOOL_END])

    _tap(monkeypatch, "trav:99", _pending())

    assert _REAL_TRAVEL_TIMES() == {}


def test_answering_replaces_the_question_with_the_verdict(six_calendars, monkeypatch, fresh_db, everyone_drives):
    """The answer has to take effect on the card in front of them, or there is
    no way to tell the tap did anything."""
    _stub_calendar(monkeypatch, [SCHOOL_END])
    _use_real_travel_table(monkeypatch)

    query = _tap(monkeypatch, "trav:1", _pending())  # 30 min, gap is only 15

    text = query.edit_message_text.await_args.args[0]
    assert "How long does" not in text
    assert "⚠️" in text and "15 min" in text


# ----- the advice attached to the warning -----

def test_the_transition_warning_does_not_suggest_changing_the_carer(six_calendars, monkeypatch, everyone_drives):
    """Naming a different adult cannot fix it - the child still has the same
    distance to cover in the same minutes. Advice that cannot work is worse
    than none: it costs a tap to find out."""
    pair = places.pair_key("child_1-school", "centre_1")
    _stub_calendar(monkeypatch, [SCHOOL_END], learned={pair: 30})
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    text = update.message.reply_text.await_args.args[0]
    assert "change the pickup to" not in text
    assert "move one of them" in text


def test_a_pickup_clash_still_suggests_changing_the_carer(six_calendars, monkeypatch, everyone_drives):
    """The other kinds are coordination problems, and naming somebody else
    genuinely is the fix. Pinned so the branch above cannot swallow both."""
    child_2 = ("child_2_enrichment", _event("Child_2 4.10pm Ballet class", MONDAY, 15, 0, 16, 10, "e"))
    _stub_calendar(monkeypatch, [child_2])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _centre_1_proposal()))

    assert "change the pickup to" in update.message.reply_text.await_args.args[0]


def test_a_child_s_own_school_is_not_named_twice(six_calendars):
    """"Child_1 has 15 min to get from Child_1's school" reads like two Erins."""
    pickups = roster.pickups_from_events([SCHOOL_END, CENTRE_1])
    clash = [c for c in roster.find_clashes(pickups, travel=_travel(30))
             if c.kind == roster.TIGHT_TRANSITION][0]
    assert "from school to" in roster.describe_clash(clash)


# ----- /travel -----

def _run_travel(monkeypatch, args=None, user_id=ADMIN_ID):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    asyncio.run(main.travel(update, context))
    return update.message.reply_text


def test_travel_lists_what_it_has_learned(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    monkeypatch.setattr(places, "TRAVEL_MINUTES", {})
    db.set_travel_minutes(places.pair_key("child_1-school", "centre_1"), 30, ADMIN_ID)

    body = _run_travel(monkeypatch).await_args.args[0]

    assert "Child\\_1's school" in body and "Centre\\_1" in body and "30 min" in body


def test_travel_marks_which_numbers_come_from_the_file(six_calendars, monkeypatch, fresh_db):
    """A seeded entry BEATS a learned one, so somebody staring at a figure
    they think they corrected needs to see what is overriding them.

    Needs BOTH kinds present: the marker only earns its place when there is a
    mix to distinguish, and with everything seeded it would be on every line.
    """
    _use_real_travel_table(monkeypatch)
    monkeypatch.setattr(places, "TRAVEL_MINUTES",
                        {places.pair_key("child_1-school", "centre_1"): 10})
    db.set_travel_minutes(places.pair_key("home", "centre_2"), 25, ADMIN_ID)

    body = _run_travel(monkeypatch).await_args.args[0]

    assert "10 min" in body and "10 min _(" not in body   # the file value, unmarked
    assert "25 min _(answered)_" in body


def test_travel_drops_the_marker_when_nothing_was_learned(six_calendars, monkeypatch, fresh_db):
    """Everything seeded means the note is on all 36 lines and distinguishes
    nothing. The header already says where the numbers come from."""
    _use_real_travel_table(monkeypatch)
    monkeypatch.setattr(places, "TRAVEL_MINUTES",
                        {places.pair_key("child_1-school", "centre_1"): 10})

    body = _run_travel(monkeypatch).await_args.args[0]
    assert "_(edited)_" not in body and "_(answered)_" not in body


def test_travel_says_so_when_it_knows_nothing(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    monkeypatch.setattr(places, "TRAVEL_MINUTES", {})
    assert "don't know how long" in _run_travel(monkeypatch).await_args.args[0]


def test_travel_can_forget_a_wrong_answer(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    db.set_travel_minutes(places.pair_key("child_1-school", "centre_1"), 30, ADMIN_ID)

    _run_travel(monkeypatch, ["forget", "child_1-school", "centre_1"])

    assert _REAL_TRAVEL_TIMES() == {}


def test_travel_forget_needs_both_ends(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["forget", "centre_1"]).await_args.args[0]
    assert "both ends" in body


def test_travel_is_admin_only(six_calendars, monkeypatch, fresh_db):
    """These numbers decide whether a warning fires, so they sit behind the
    same audit capability as /hints."""
    monkeypatch.setenv("MOM_TELEGRAM_ID", "12345")
    _use_real_travel_table(monkeypatch)
    assert _run_travel(monkeypatch, user_id=12345).await_count == 0


def test_travel_says_who_the_numbers_are_for(six_calendars, monkeypatch, fresh_db):
    """A list of bare minutes looks wrong to whoever knows the drive is half
    that. The mode is part of reading the table, not a separate topic."""
    _use_real_travel_table(monkeypatch)
    monkeypatch.setattr(places, "TRAVEL_MINUTES",
                        {places.pair_key("child_1-school", "centre_1"): 20})

    body = _run_travel(monkeypatch).await_args.args[0]

    assert "by car" in body
    assert "Helper\\_1" in body and "20 min" in body


# ----- three levels of precedence -----

def test_an_edit_beats_the_seeded_table():
    """The file is the starting point; an edit is a correction made later and
    on purpose, so it has to win or editing from Telegram does nothing."""
    pair = places.pair_key("child_1-school", "centre_1")
    assert places.travel_minutes("child_1-school", "centre_1", people=["Parent_1"],
                                 overrides={pair: 25}) == 25


def test_a_bucket_tap_still_loses_to_the_seeded_table():
    """Answered in a hurry, mid-write. It must never quietly overwrite a
    number somebody sat down and set - which is the whole reason 'set' and
    'answered' are stored apart."""
    pair = places.pair_key("child_1-school", "centre_1")
    assert places.travel_minutes("child_1-school", "centre_1", {pair: 45},
                                 people=["Parent_1"]) == 20  # the places.py figure


def test_an_edit_beats_a_tap_as_well():
    pair = places.pair_key("child_1-school", "centre_1")
    assert places.travel_minutes("child_1-school", "centre_1", {pair: 45},
                                 people=["Parent_1"], overrides={pair: 25}) == 25


def test_a_tap_still_fills_a_journey_the_file_does_not_have():
    """Lowest priority is not 'ignored' - a tap is the only thing that knows
    about a place the file has never heard of."""
    pair = places.pair_key("home", "centre_2")
    assert places.travel_minutes("home", "centre_2", {pair: 30},
                                 people=["Parent_1"]) == 30


def test_the_transport_penalty_applies_to_an_edit_too(monkeypatch):
    """Every stored number is a car time, however it got there."""
    pair = places.pair_key("child_1-school", "centre_1")
    assert places.travel_minutes("child_1-school", "centre_1", people=["Helper_1"],
                                 overrides={pair: 25}) == 45


# ----- what the database keeps apart -----

def test_a_tap_does_not_downgrade_an_edit(fresh_db):
    """Belt and braces with the lookup: even the write refuses, so a stray tap
    cannot turn a deliberate 25 into a bucketed 30 in the stored row."""
    pair = places.pair_key("child_1-school", "centre_1")
    db.set_travel_minutes(pair, 25, ADMIN_ID, db.SET_BY_HAND)
    db.set_travel_minutes(pair, 30, ADMIN_ID, db.ANSWERED_ON_CARD)

    overrides, answers = db.travel_times_by_source()
    assert overrides == {pair: 25} and answers == {}


def test_an_edit_overrules_an_earlier_tap(fresh_db):
    """The other direction is exactly how you correct a tap you regret."""
    pair = places.pair_key("child_1-school", "centre_1")
    db.set_travel_minutes(pair, 30, ADMIN_ID, db.ANSWERED_ON_CARD)
    db.set_travel_minutes(pair, 25, ADMIN_ID, db.SET_BY_HAND)

    overrides, answers = db.travel_times_by_source()
    assert overrides == {pair: 25} and answers == {}


def test_forget_says_which_kind_it_dropped(fresh_db):
    pair = places.pair_key("child_1-school", "centre_1")
    db.set_travel_minutes(pair, 25, ADMIN_ID, db.SET_BY_HAND)
    assert db.forget_travel_minutes(pair) == db.SET_BY_HAND
    assert db.forget_travel_minutes(pair) == ""


# ----- /travel set and forget -----

def test_set_stores_an_override(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["set", "child_1-school", "centre_1", "25"])

    overrides, _ = _REAL_BY_SOURCE()
    assert overrides == {places.pair_key("child_1-school", "centre_1"): 25}


def test_set_accepts_the_label_not_just_the_key(six_calendars, monkeypatch, fresh_db):
    """Typing `ballet-studio_2` when the list says "Ballet (Studio_2)" is the
    friction that stops a table being maintained."""
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["set", "Child_1's school", "Ballet (Studio_2)", "40"])

    overrides, _ = _REAL_BY_SOURCE()
    assert overrides == {places.pair_key("child_1-school", "ballet-studio_2"): 40}


def test_set_refuses_a_place_it_does_not_know(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["set", "child_1-school", "narnia", "25"]).await_args.args[0]

    assert "don't know" in body and "narnia" in body
    assert _REAL_BY_SOURCE()[0] == {}


def test_set_refuses_a_time_that_is_not_a_number(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["set", "child_1-school", "centre_1", "soon"]).await_args.args[0]
    assert "number of minutes" in body


def test_forget_an_override_restores_the_file_value(six_calendars, monkeypatch, fresh_db):
    """Dropping an override and dropping an answer mean different things, and
    reporting "forgotten" for both would hide which happened."""
    _use_real_travel_table(monkeypatch)
    db.set_travel_minutes(places.pair_key("child_1-school", "centre_1"), 25,
                          ADMIN_ID, db.SET_BY_HAND)

    body = _run_travel(monkeypatch, ["forget", "child_1-school", "centre_1"]).await_args.args[0]

    assert "20 min from places.py" in body


def test_forget_an_unseeded_journey_says_it_will_ask_again(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    db.set_travel_minutes(places.pair_key("home", "centre_2"), 25, ADMIN_ID)

    body = _run_travel(monkeypatch, ["forget", "home", "centre_2"]).await_args.args[0]

    assert "ask again" in body


# ----- the three-tap editor -----

def _press(data):
    """Drive the real callback handler, with no pending state involved - which
    is the point of the design being tested."""
    query = MagicMock()
    query.data = data
    query.from_user.id = ADMIN_ID
    query.message.chat_id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = 1
    asyncio.run(main.handle_callback(update, MagicMock()))
    return query


def _buttons(query):
    kb = query.edit_message_text.await_args.kwargs.get("reply_markup")
    return [b.callback_data for row in kb.inline_keyboard for b in row] if kb else []


def test_the_edit_button_offers_every_place(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    query = _press("tvedit")

    picks = [d for d in _buttons(query) if d.startswith("tvfrom:")]
    assert len(picks) == len(main._place_choices())


def test_picking_the_first_place_excludes_it_from_the_second(six_calendars, monkeypatch, fresh_db):
    """A journey from a place to itself takes no time and is not editable."""
    _use_real_travel_table(monkeypatch)
    query = _press("tvfrom:0")

    picks = [d for d in _buttons(query) if d.startswith("tvto:")]
    assert len(picks) == len(main._place_choices()) - 1
    assert "tvto:0:0" not in picks


def test_the_minutes_step_shows_what_is_recorded_now(six_calendars, monkeypatch, fresh_db):
    """Correcting a number is much easier when you can see the one you are
    correcting."""
    _use_real_travel_table(monkeypatch)
    choices = main._place_choices()
    i, j = choices.index("child_1-school"), choices.index("centre_1")

    query = _press(f"tvto:{i}:{j}")

    assert "now 20 min by car" in query.edit_message_text.await_args.args[0]


def test_a_tapped_edit_is_stored_as_deliberate(six_calendars, monkeypatch, fresh_db):
    """Not as an answer - it came from the edit flow, so it must outrank
    places.py the way a typed /travel set does."""
    _use_real_travel_table(monkeypatch)
    choices = main._place_choices()
    i, j = choices.index("child_1-school"), choices.index("centre_1")

    _press(f"tvset:{i}:{j}:25")

    overrides, _ = _REAL_BY_SOURCE()
    assert overrides == {places.pair_key("child_1-school", "centre_1"): 25}


def test_the_table_is_shown_again_after_an_edit(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    choices = main._place_choices()
    i, j = choices.index("child_1-school"), choices.index("centre_1")

    query = _press(f"tvset:{i}:{j}:25")

    body = query.edit_message_text.await_args.args[0]
    assert "is now 25 min" in body and "_(edited)_" in body


@pytest.mark.parametrize("data", ["tvfrom:99", "tvto:0:99", "tvto:99:0", "tvset:0:99:25"])
def test_an_out_of_range_place_writes_nothing(six_calendars, monkeypatch, fresh_db, data):
    """callback_data is client-controlled, same as cat:/kid:/create_opt:.
    Unchecked, an index would wrap to a real-but-different journey, which is
    worse than an error because it looks like it worked."""
    _use_real_travel_table(monkeypatch)
    _press(data)
    assert _REAL_BY_SOURCE()[0] == {}


def test_a_minutes_value_the_buttons_never_offered_is_refused(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    choices = main._place_choices()
    i, j = choices.index("child_1-school"), choices.index("centre_1")

    _press(f"tvset:{i}:{j}:999")

    assert _REAL_BY_SOURCE()[0] == {}


def test_the_same_place_twice_is_refused(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    _press("tvset:0:0:25")
    assert _REAL_BY_SOURCE()[0] == {}


def test_editing_never_disturbs_an_open_confirm_card(six_calendars, monkeypatch, fresh_db):
    """pending_state holds ONE row per chat. If the editor used it, starting an
    edit would clobber a pending write and the family's confirm card would
    report "expired" - which is why the two place indexes ride in
    callback_data instead."""
    _use_real_travel_table(monkeypatch)
    db.save_pending(1, "confirm", {"kind": "create", "title": "Child_1 4pm Centre_1"})
    choices = main._place_choices()
    i, j = choices.index("child_1-school"), choices.index("centre_1")

    _press("tvedit")
    _press(f"tvfrom:{i}")
    _press(f"tvto:{i}:{j}")
    _press(f"tvset:{i}:{j}:25")

    still_there = db.peek_pending(1, "confirm")
    assert still_there and still_there["title"] == "Child_1 4pm Centre_1"


def test_the_editor_shows_the_car_time_not_the_adjusted_one(six_calendars, monkeypatch, fresh_db):
    """The step asks "how long by car?". Showing Helper_1's 40 there would invite
    an answer with the bus penalty already in it, and it would then be added a
    second time on every lookup."""
    _use_real_travel_table(monkeypatch)
    assert places.stored_minutes("child_1-school", "centre_1") == 20
    assert places.travel_minutes("child_1-school", "centre_1", people=["Helper_1"]) == 40


# ----- adding a place from Telegram -----

@pytest.fixture
def no_user_places():
    """The overlay is module state, so a test that adds a place has to put it
    back or the next one inherits it."""
    before = dict(places._USER_PLACES)
    yield
    places.set_user_places(before)


def test_an_added_place_resolves_a_title_that_previously_did_not(
        six_calendars, monkeypatch, fresh_db, no_user_places):
    """The point of the feature: a new centre becomes recognisable without a
    code change or a restart."""
    chess = ("child_1_enrichment", _event("Child_1 5pm Chess club", MONDAY, 16, 0, 17, 0))
    assert places.place_for(*chess) is None

    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["place", "Chess", "Club", "|", "chess"])

    assert places.place_for(*chess) == "chess-club"


def test_an_added_place_can_be_scoped_to_one_child(
        six_calendars, monkeypatch, fresh_db, no_user_places):
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["place", "Child_1", "Tutor", "|", "tutoring", "for", "Child_1"])

    for_child_1 = ("child_1_enrichment", _event("Child_1 5pm Tutoring", MONDAY, 16, 0, 17, 0))
    for_child_2 = ("child_2_enrichment", _event("Child_2 5pm Tutoring", MONDAY, 16, 0, 17, 0))
    assert places.place_for(*for_child_1) == "child-1-tutor"
    assert places.place_for(*for_child_2) is None


def test_a_too_short_alias_is_refused(six_calendars, monkeypatch, fresh_db, no_user_places):
    """An alias is a SUBSTRING match, so a three-letter one fires on any title
    that happens to contain those letters and silently mis-files events."""
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["place", "Chess", "Club", "|", "ch"]).await_args.args[0]

    assert "too short" in body
    assert places.known_places().get("chess-club") is None


def test_an_alias_that_already_means_somewhere_else_is_refused(
        six_calendars, monkeypatch, fresh_db, no_user_places):
    """Stealing "centre_1" would produce confident warnings about journeys
    nobody is making."""
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["place", "New", "Place", "|", "centre_1"]).await_args.args[0]

    assert "already means" in body and "Centre\\_1" in body


def test_a_place_already_in_the_file_is_refused(six_calendars, monkeypatch, fresh_db, no_user_places):
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["place", "Swimming"]).await_args.args[0]
    assert "already in places.py" in body


def test_an_added_place_becomes_editable(six_calendars, monkeypatch, fresh_db, no_user_places):
    """Adding a place is only useful if its journeys can then be given times."""
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["place", "Chess", "Club", "|", "chess"])

    assert "chess-club" in main._place_choices()

    _run_travel(monkeypatch, ["set", "child_1-school", "chess-club", "30"])
    overrides, _ = _REAL_BY_SOURCE()
    assert overrides == {places.pair_key("child_1-school", "chess-club"): 30}


def test_naming_no_place_explains_the_form(six_calendars, monkeypatch, fresh_db, no_user_places):
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["place"]).await_args.args[0]
    assert "Centre_2" in body


def test_a_place_survives_a_restart(six_calendars, monkeypatch, fresh_db, no_user_places):
    """It lives in SQLite, not in the module - _load_user_places is what puts
    it back, and it runs at startup."""
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["place", "Chess", "Club", "|", "chess"])

    places.set_user_places({})          # as if the process had just started
    main._load_user_places()

    assert places.known_places()["chess-club"]["label"] == "Chess Club"


def test_the_child_suffix_works_on_either_side_of_the_aliases(
        six_calendars, monkeypatch, fresh_db, no_user_places):
    """Both orders read naturally, so both are accepted rather than one being
    silently treated as part of an alias."""
    _use_real_travel_table(monkeypatch)
    _run_travel(monkeypatch, ["place", "Child_1", "Tutor", "for", "Child_1", "|", "tutoring"])

    assert places.known_places()["child-1-tutor"]["child"] == "child_1"
    assert places.known_places()["child-1-tutor"]["aliases"] == ["tutoring"]


def test_a_long_table_is_capped_rather_than_rejected(six_calendars, monkeypatch, fresh_db):
    """Telegram does not truncate an over-long message, it REJECTS it - so
    without a cap /travel would simply stop working once enough places
    accumulated, with nothing on screen saying why."""
    _use_real_travel_table(monkeypatch)
    big = {places.pair_key(f"place-{i}", f"place-{j}"): 20
           for i in range(20) for j in range(i + 1, 20)}
    monkeypatch.setattr(places, "TRAVEL_MINUTES", big)

    body = _run_travel(monkeypatch).await_args.args[0]

    assert len(body) < 4096
    assert "and 110 more" in body


# ----- remembering the syntax -----

def test_help_shows_the_travel_syntax_to_the_admin(six_calendars, monkeypatch, fresh_db):
    """The travel subcommands are the only typed syntax in the bot, so they are
    the only thing anyone has to remember."""
    _use_real_travel_table(monkeypatch)
    body = main.help_text_for(ADMIN_ID)
    assert "/travel set" in body and "/travel place" in body


def test_help_hides_the_travel_syntax_from_everyone_else(six_calendars, monkeypatch, fresh_db):
    """/travel is admin-only, and documenting a command that will silently
    ignore you is worse than not listing it."""
    monkeypatch.setenv("MOM_TELEGRAM_ID", "12345")
    _use_real_travel_table(monkeypatch)
    assert "/travel set" not in main.help_text_for(12345)


def test_travel_help_is_reachable_from_travel(six_calendars, monkeypatch, fresh_db):
    """Where you already are when you forget the syntax."""
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch, ["help"]).await_args.args[0]
    assert "/travel set" in body and "by car" in body


def test_the_examples_use_places_that_actually_exist(six_calendars, monkeypatch, fresh_db):
    """An example naming a place this family does not have is worse than no
    example - it cannot be copied and run."""
    _use_real_travel_table(monkeypatch)
    a, b = main._example_pair()
    assert a in main._place_choices() and b in main._place_choices()
    assert f"/travel set {a} {b} 25" in main.travel_help_text()


def test_the_listing_points_at_the_syntax_rather_than_repeating_it(six_calendars, monkeypatch, fresh_db):
    """The table is already long; the syntax is a screen of its own."""
    _use_real_travel_table(monkeypatch)
    body = _run_travel(monkeypatch).await_args.args[0]
    assert "/travel help" in body and "/travel place Chess" not in body


def test_the_upcoming_example_stays_with_the_command_list(six_calendars, monkeypatch, fresh_db):
    """The travel block goes last. Between the commands and their own example
    it orphaned that example from the list it explains."""
    _use_real_travel_table(monkeypatch)
    body = main.help_text_for(ADMIN_ID)
    assert body.index("_e.g._ `/upcoming") < body.index("Editing travel times")


def test_the_example_journey_is_one_somebody_might_make(six_calendars, monkeypatch, fresh_db):
    """Pairing one child's school with the OTHER child's ballet studio is a
    real journey nobody ever makes - a confusing thing to lead with."""
    _use_real_travel_table(monkeypatch)
    _, b = main._example_pair()
    assert not (places.known_places().get(b) or {}).get("child")


# ----- travel in plain words -----

def _say(result, user_id=ADMIN_ID):
    """Drive _dispatch with a parse, as a text message would."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    asyncio.run(main._dispatch(update, 1, user_id, result, "src"))
    return update.message.reply_text


def _travel_result(**kw):
    return {"action": "travel", "status": "ok", **kw}


def test_a_spoken_journey_is_applied(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25))

    assert "25 min by car" in reply.await_args.args[0]
    overrides, _ = _REAL_BY_SOURCE()
    assert overrides == {places.pair_key("child_1-school", "centre_1"): 25}


def test_the_reply_names_both_places_so_a_misparse_shows(six_calendars, monkeypatch, fresh_db):
    """Applied without a confirm card, so the reply IS the check."""
    _use_real_travel_table(monkeypatch)
    body = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1",
                               travel_minutes=25)).await_args.args[0]
    assert "Child\\_1's school" in body and "Centre\\_1" in body


def test_a_spoken_journey_offers_undo(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25))

    kb = reply.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data.startswith("tvundo:")


def test_undo_restores_the_seeded_value(six_calendars, monkeypatch, fresh_db):
    """There was no override before, so undo must DELETE rather than write a
    value back - otherwise the places.py figure would stay shadowed forever."""
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25))
    undo = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    query = _press(undo)

    assert _REAL_BY_SOURCE()[0] == {}
    assert "20 min" in query.edit_message_text.await_args.args[0]


def test_undo_restores_an_earlier_edit_rather_than_deleting(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    db.set_travel_minutes(places.pair_key("child_1-school", "centre_1"), 30,
                          ADMIN_ID, db.SET_BY_HAND)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25))
    undo = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    _press(undo)

    assert _REAL_BY_SOURCE()[0] == {places.pair_key("child_1-school", "centre_1"): 30}


def test_undo_still_reverts_the_right_journey_after_a_place_is_added(
        six_calendars, monkeypatch, fresh_db, no_user_places):
    """The reason undo carries place KEYS and not indexes. This button lives
    in chat history for days; an added place shifts every index, and an
    index-based undo would confidently revert a DIFFERENT journey."""
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25))
    undo = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    # "Aardvark" sorts first, so every index shifts by one.
    _run_travel(monkeypatch, ["place", "Aardvark", "Hall", "|", "aardvark"])
    _press(undo)

    assert _REAL_BY_SOURCE()[0] == {}


def test_an_over_long_undo_payload_omits_the_button():
    """Telegram caps callback_data at 64 bytes. A truncated payload would undo
    something unpredictable, so no button at all is the safer answer."""
    long_a = "specialist clinic m children's tower level 1"
    assert main._undo_payload(long_a, "another very long place name here", -1) == ""
    assert main._undo_keyboard("") is None


def test_a_non_admin_is_told_rather_than_ignored(six_calendars, monkeypatch, fresh_db):
    """/travel ignores non-admins in silence, which is right for a command
    they were never shown. A sentence they typed deserves an answer."""
    monkeypatch.setenv("MOM_TELEGRAM_ID", "12345")
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="Child_1's school", to_place="Centre_1", travel_minutes=25),
                 user_id=12345)

    assert "Only the admin" in reply.await_args.args[0]
    assert _REAL_BY_SOURCE()[0] == {}


def test_a_place_it_does_not_know_is_refused(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    body = _say(_travel_result(from_place="Narnia", to_place="Centre_1",
                               travel_minutes=25)).await_args.args[0]

    assert "don't know" in body and "Narnia" in body
    assert _REAL_BY_SOURCE()[0] == {}


def test_forgetting_in_words_drops_the_override(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    db.set_travel_minutes(places.pair_key("child_1-school", "centre_1"), 30,
                          ADMIN_ID, db.SET_BY_HAND)

    body = _say(_travel_result(forget=True, from_place="Child_1's school",
                               to_place="Centre_1")).await_args.args[0]

    assert "20 min from places.py" in body
    assert _REAL_BY_SOURCE()[0] == {}


# ----- the one-sided question -----

def _one_sided(known="Centre_1", minutes=20):
    return {"action": "travel", "status": "needs_clarification",
            "question": f"{minutes} minutes from where?", "source_text": "src",
            "known_place": known, "travel_minutes": minutes}


def test_one_sided_asks_with_buttons(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    reply = _say(_one_sided())

    body = reply.await_args.args[0]
    kb = reply.await_args.kwargs["reply_markup"]
    assert "from Centre_1 to where?" in body
    assert any(b.callback_data.startswith("tvone:")
               for row in kb.inline_keyboard for b in row)


def test_answering_the_one_sided_question_completes_the_edit(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    reply = _say(_one_sided())
    buttons = {b.text: b.callback_data
               for row in reply.await_args.kwargs["reply_markup"].inline_keyboard
               for b in row}

    _press(buttons["Child_1's school"])

    assert _REAL_BY_SOURCE()[0] == {places.pair_key("child_1-school", "centre_1"): 20}


def test_a_tampered_one_sided_callback_writes_nothing(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    _press("tvone:0:20:99")
    _press("tvone:0:9999:1")
    assert _REAL_BY_SOURCE()[0] == {}


# ----- adding a place in words -----

def test_a_place_can_be_introduced_in_words(six_calendars, monkeypatch, fresh_db, no_user_places):
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(new_place="Chess Club"))

    assert "Added" in reply.await_args.args[0]
    assert places.known_places()["chess-club"]["label"] == "Chess Club"


def test_a_spoken_place_gets_the_same_alias_check(six_calendars, monkeypatch, fresh_db, no_user_places):
    """Typed and spoken creation share _vet_new_place - a bad alias mis-files
    events whichever way it arrived."""
    _use_real_travel_table(monkeypatch)
    body = _say(_travel_result(new_place="Centre_1")).await_args.args[0]
    assert "already means" in body or "already in places.py" in body


def test_undoing_a_spoken_place_forgets_it(six_calendars, monkeypatch, fresh_db, no_user_places):
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(new_place="Centre_2"))
    undo = reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    _press(undo)

    assert "chess-club" not in places.known_places()


def test_school_alone_asks_which_one(six_calendars, monkeypatch, fresh_db):
    """The two girls are at different schools, 10 minutes apart and at
    different distances from everywhere else. Picking whichever sorts first
    would record the wrong journey and nothing on screen would say so."""
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="school", to_place="Centre_1", travel_minutes=25))

    body = reply.await_args.args[0]
    kb = reply.await_args.kwargs["reply_markup"]
    labels = {b.text for row in kb.inline_keyboard for b in row} - {"❌ Cancel"}
    assert "Which school did you mean?" in body
    assert labels == {"Child_1's school", "Child_2's school"}
    assert _REAL_BY_SOURCE()[0] == {}


def test_answering_which_school_completes_the_edit(six_calendars, monkeypatch, fresh_db):
    _use_real_travel_table(monkeypatch)
    reply = _say(_travel_result(from_place="school", to_place="Centre_1", travel_minutes=25))
    buttons = {b.text: b.callback_data
               for row in reply.await_args.kwargs["reply_markup"].inline_keyboard
               for b in row}

    _press(buttons["Child_2's school"])

    assert _REAL_BY_SOURCE()[0] == {places.pair_key("child_2-school", "centre_1"): 25}


def test_a_partial_name_that_means_one_place_just_works(six_calendars, monkeypatch, fresh_db):
    """"Studio_2" is unambiguous, so it should not ask. Demanding
    `ballet-studio_2` is the friction this whole change removes."""
    _use_real_travel_table(monkeypatch)
    _say(_travel_result(from_place="Studio_2", to_place="Centre_1", travel_minutes=40))

    assert _REAL_BY_SOURCE()[0] == {places.pair_key("ballet-studio_2", "centre_1"): 40}


def test_help_leads_with_plain_words(six_calendars, monkeypatch, fresh_db):
    """Typed commands are now the fallback, not the main route - the help
    should say so, or the friction this removed stays in people's heads."""
    _use_real_travel_table(monkeypatch)
    body = main.travel_help_text()
    assert "just tell me" in body
    assert body.index("just tell me") < body.index("/travel set")
