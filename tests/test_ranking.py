"""Ranking several candidate slots for one class.

CLAUDE.md Flow 2, over slots the USER named rather than ones the bot invented.
Asked for 2026-08-17: "I will give it a few date and time and I want it to
suggest by ranking, the best dates and time for this new class."

Ordered by how hard the problem is to FIX, not how alarming it sounds:
double-booked (impossible) > nobody rostered > one carer twice > too tight to
travel > clear.
"""
import asyncio
import datetime
import os
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import db
import event_parser as ep
import main
import roster

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
MONDAY = datetime.date(2026, 9, 7)
SATURDAY = datetime.date(2026, 9, 12)
SUNDAY = datetime.date(2026, 9, 13)
TODAY = datetime.date(2026, 9, 1)


@pytest.fixture
def fresh_db(tmp_path):
    old = db.DB_PATH
    db.DB_PATH = str(tmp_path / "test.db")
    db._local = threading.local()
    db.init_db()
    yield
    db._local = threading.local()
    db.DB_PATH = old


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID",
                               f"cal-{child}-{category}")


def _at(date, hour, minute=0):
    return datetime.datetime.combine(date, datetime.time(hour, minute), tzinfo=ct.TIMEZONE)


def _event(summary, date, sh, sm, eh, em, event_id="ev"):
    return {"id": event_id, "summary": summary,
            "start": {"dateTime": _at(date, sh, sm).isoformat()},
            "end": {"dateTime": _at(date, eh, em).isoformat()}}


def _slot(date, sh, sm, eh, em):
    return {"all_day": False, "start": _at(date, sh, sm), "end": _at(date, eh, em),
            "date": None, "time_anchor": "end"}


def _stub(monkeypatch, existing):
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across",
                        lambda s, e, keys=None: list(existing))
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))


def _rank(monkeypatch, slots, existing=(), key="child_1_enrichment", title="Child_1 4pm Swimming"):
    _stub(monkeypatch, existing)
    update = MagicMock()
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    result = {"action": "create", "status": "ok", "title": title, "location": None,
              "notes": None, "pickup": None, "calendar_key": key, "slot_options": slots}
    asyncio.run(main._present_ranked_slots(update, 1, result))
    return update.message.reply_text.await_args


def _order(args):
    """The slot times, in the order they were listed."""
    return [line.split("*")[1] for line in args.args[0].splitlines()
            if line.startswith(("✅", "🟡", "⚠️", "❌"))]


# ----- reading several candidates out of one message -----

def test_several_alternatives_are_resolved(six_calendars):
    raw = {"slot_options": [
        {"date": "2026-09-10", "start_time": "16:00"},
        {"date": "2026-09-12", "start_time": "10:00"},
    ]}
    slots = ep.resolve_slot_options(raw, TODAY)
    assert len(slots) == 2
    assert slots[0]["start"].hour == 16 and slots[1]["start"].hour == 10


def test_a_single_candidate_is_not_a_choice(six_calendars):
    """One option is an ordinary create. Ranking a list of one would put a
    chooser in front of a decision nobody is making."""
    raw = {"slot_options": [{"date": "2026-09-10", "start_time": "16:00"}]}
    assert ep.resolve_slot_options(raw, TODAY) == []


def test_the_same_slot_twice_collapses(six_calendars):
    raw = {"slot_options": [
        {"date": "2026-09-10", "start_time": "16:00"},
        {"date": "2026-09-10", "start_time": "16:00"},
    ]}
    assert ep.resolve_slot_options(raw, TODAY) == []  # one distinct slot is no choice


def test_one_unusable_candidate_does_not_sink_the_rest(six_calendars):
    """Three good times and one piece of nonsense should still rank three."""
    raw = {"slot_options": [
        {"date": "2026-09-10", "start_time": "16:00"},
        {"date": "not-a-date", "start_time": "10:00"},
        {"date": "2026-09-12", "start_time": "10:00"},
    ]}
    assert len(ep.resolve_slot_options(raw, TODAY)) == 2


def test_a_candidate_with_no_time_is_dropped(six_calendars):
    """"Thursday" alone is not a slot - and inventing an hour for it would be
    ranking a time nobody proposed."""
    raw = {"slot_options": [
        {"date": "2026-09-10"},
        {"date": "2026-09-12", "start_time": "10:00"},
        {"date": "2026-09-13", "start_time": "14:00"},
    ]}
    slots = ep.resolve_slot_options(raw, TODAY)
    assert len(slots) == 2


def test_too_many_candidates_are_capped(six_calendars):
    raw = {"slot_options": [{"date": "2026-09-10", "start_time": f"{9 + i:02d}:00"}
                            for i in range(12)]}
    assert len(ep.resolve_slot_options(raw, TODAY)) <= ep.MAX_SLOT_OPTIONS


def test_options_do_not_become_a_multi_date_create(six_calendars):
    """`dates` means add ALL of them; slot_options means pick ONE. Confusing
    the two would silently create four classes instead of offering four."""
    result = ep._finalize({
        "action": "create", "title": "swimming", "child": "child_1",
        "category": "enrichment",
        "slot_options": [{"date": "2026-09-10", "start_time": "16:00"},
                         {"date": "2026-09-12", "start_time": "10:00"}],
    }, "src")
    assert len(result["slot_options"]) == 2
    assert len(result["occurrences"]) == 1  # nothing is created by offering


# ----- the ordering -----

def test_a_clear_slot_beats_everything(six_calendars, monkeypatch):
    booked = ("child_1_enrichment", _event("Child_1 5pm Ballet at Studio_3", MONDAY, 16, 0, 17, 0))
    args = _rank(monkeypatch,
                 [_slot(MONDAY, 16, 30, 17, 30), _slot(SUNDAY, 14, 0, 15, 0)],
                 [booked])
    assert _order(args)[0].startswith("Sun")


def test_a_double_booking_ranks_last(six_calendars, monkeypatch):
    """Impossible beats merely awkward: she cannot be in two places, so this
    one has to move whatever anyone decides."""
    booked = ("child_1_enrichment", _event("Child_1 5pm Ballet at Studio_3", MONDAY, 16, 0, 17, 0))
    args = _rank(monkeypatch,
                 [_slot(MONDAY, 16, 30, 17, 30), _slot(SUNDAY, 14, 0, 15, 0)],
                 [booked])
    assert _order(args)[-1].startswith("Mon")
    assert "already booked" in args.args[0]


def test_a_tight_transition_ranks_below_clear_but_above_impossible(six_calendars, monkeypatch):
    school = ("child_1_school", _event("Child_1 3pm Gate 2 End school", MONDAY, 14, 45, 15, 0, "s"))
    booked = ("child_1_enrichment", _event("Child_1 6pm Ballet at Studio_3", MONDAY, 17, 0, 18, 0, "b"))
    args = _rank(monkeypatch,
                 [_slot(MONDAY, 17, 30, 18, 30),   # overlaps ballet
                  _slot(MONDAY, 15, 0, 16, 0),     # straight after school
                  _slot(SUNDAY, 14, 0, 15, 0)],    # clear
                 [school, booked])
    order = _order(args)
    assert order[0].startswith("Sun")
    assert order[-1].startswith("Mon 07 Sep, 17:30")


def test_two_clear_slots_break_the_tie_by_being_sooner(six_calendars, monkeypatch):
    """Not by whichever the model happened to list first."""
    args = _rank(monkeypatch, [_slot(SUNDAY, 14, 0, 15, 0), _slot(SATURDAY, 10, 0, 11, 0)])
    assert _order(args)[0].startswith("Sat")


def test_every_row_says_why(six_calendars, monkeypatch):
    """A ranking with no reasons is just an opinion."""
    booked = ("child_1_enrichment", _event("Child_1 5pm Ballet at Studio_3", MONDAY, 16, 0, 17, 0))
    body = _rank(monkeypatch,
                 [_slot(MONDAY, 16, 30, 17, 30), _slot(SUNDAY, 14, 0, 15, 0)],
                 [booked]).args[0]
    assert "clear" in body and "already booked" in body


def test_the_best_row_is_marked_only_when_it_is_actually_clear(six_calendars, monkeypatch):
    booked = ("child_1_enrichment", _event("Child_1 5pm Ballet at Studio_3", MONDAY, 16, 0, 17, 0))
    ok = _rank(monkeypatch, [_slot(SUNDAY, 14, 0, 15, 0), _slot(MONDAY, 16, 30, 17, 30)],
               [booked]).args[0]
    assert "← best" in ok

    all_bad = _rank(monkeypatch, [_slot(MONDAY, 16, 30, 17, 30), _slot(MONDAY, 16, 15, 17, 15)],
                    [booked]).args[0]
    assert "← best" not in all_bad
    assert "Every option has a problem" in all_bad


def test_an_all_day_event_still_blocks_a_slot(six_calendars, monkeypatch):
    """An all-day event produces no PICKUP, so the roster never sees it - but
    "she is away that whole day" is exactly what this is being asked."""
    away = ("child_1_school", {"id": "a", "summary": "Child_1 School closure",
                            "start": {"date": MONDAY.isoformat()},
                            "end": {"date": MONDAY.isoformat()}})
    args = _rank(monkeypatch, [_slot(MONDAY, 16, 0, 17, 0), _slot(SUNDAY, 14, 0, 15, 0)], [away])
    assert _order(args)[-1].startswith("Mon")


def test_a_calendar_failure_still_ranks_rather_than_failing(six_calendars, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across", boom)
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))
    update = MagicMock()
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    asyncio.run(main._present_ranked_slots(update, 1, {
        "action": "create", "status": "ok", "title": "Child_1 4pm Swimming",
        "location": None, "notes": None, "pickup": None,
        "calendar_key": "child_1_enrichment",
        "slot_options": [_slot(SUNDAY, 14, 0, 15, 0), _slot(SATURDAY, 10, 0, 11, 0)],
    }))
    assert len(_order(update.message.reply_text.await_args)) == 2


# ----- picking one -----

def test_nothing_is_written_by_ranking(six_calendars, monkeypatch):
    """Showing options is not a proposal. The human tap that commits a
    calendar write stays exactly where it has always been."""
    written = []
    monkeypatch.setattr(ct, "create_event", lambda *a, **k: written.append(a))
    _rank(monkeypatch, [_slot(SUNDAY, 14, 0, 15, 0), _slot(SATURDAY, 10, 0, 11, 0)])
    assert written == []


def test_the_buttons_match_the_rows(six_calendars, monkeypatch):
    args = _rank(monkeypatch, [_slot(SUNDAY, 14, 0, 15, 0), _slot(SATURDAY, 10, 0, 11, 0)])
    buttons = [b for row in args.kwargs["reply_markup"].inline_keyboard for b in row]
    assert len(buttons) == 3  # two options plus decline
    assert buttons[0].callback_data == "create_opt:0"


def test_the_stored_options_are_in_ranked_order(six_calendars, monkeypatch):
    """The index in callback_data points into the list as SHOWN, so the two
    must not be sorted differently - tapping the top row has to pick the top
    row's slot."""
    saved = {}
    monkeypatch.setattr(main.db, "save_pending",
                        lambda chat_id, kind, payload: saved.update(payload))
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))
    update = MagicMock()
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    asyncio.run(main._present_ranked_slots(update, 1, {
        "action": "create", "status": "ok", "title": "Child_1 4pm Swimming",
        "location": None, "notes": None, "pickup": None,
        "calendar_key": "child_1_enrichment",
        "slot_options": [_slot(SUNDAY, 14, 0, 15, 0), _slot(SATURDAY, 10, 0, 11, 0)],
    }))
    assert saved["options"][0]["start"].date() == SATURDAY


def test_the_short_reason_is_short(six_calendars):
    """describe_clash names both events and both times, which is unreadable
    when every row of a list carries one."""
    school = ("child_1_school", _event("Child_1 3pm Gate 2 End school", MONDAY, 14, 45, 15, 0, "s"))
    later = ("child_1_enrichment", _event("Child_1 4pm Swimming", MONDAY, 15, 5, 16, 0, "b"))
    pickups = roster.pickups_from_events([school, later])
    clash = [c for c in roster.find_clashes(pickups, travel=lambda a, b, people=(): 30)
             if c.kind == roster.TIGHT_TRANSITION][0]
    short = roster.summarise_clash(clash)
    assert len(short) < len(roster.describe_clash(clash))
    assert "needs 30" in short


def test_a_malformed_time_is_skipped_not_fatal(six_calendars):
    """"9:00" instead of "09:00" is enough to raise out of the time parser.
    Unhandled, one bad candidate takes the whole request down to "something
    went wrong" - which is a poor answer to a message offering three times."""
    raw = {"slot_options": [
        {"date": "2026-09-10", "start_time": "9:00"},
        {"date": "2026-09-12", "start_time": "10:00"},
        {"date": "2026-09-13", "start_time": "14:00"},
    ]}
    assert len(ep.resolve_slot_options(raw, TODAY)) == 2


# ----- moving an existing class -----

def _existing(date=MONDAY, sh=16, sm=0, eh=17, em=0, event_id="p"):
    return _event("Child_1 5pm Piano", date, sh, sm, eh, em, event_id)


def _press(data, monkeypatch, pending):
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
    monkeypatch.setattr(main.db, "pop_pending",
                        lambda chat_id, kind: pending if kind in ("amend_options", "amend_pick") else None)
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    asyncio.run(main.handle_callback(update, MagicMock()))
    return query


def test_a_move_with_several_times_is_ranked(six_calendars, monkeypatch):
    """The other half of the ranking: same scoring, but over an event that
    already exists."""
    original = _existing()
    _stub(monkeypatch, [("child_1_enrichment", original)])
    update = MagicMock()
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text

    asyncio.run(main._present_amend_options(
        update, 1, original,
        {"changes": {}, "slot_options": [_slot(SUNDAY, 14, 0, 15, 0),
                                         _slot(SATURDAY, 10, 0, 11, 0)]},
        "child_1_enrichment",
    ))

    body = update.message.reply_text.await_args.args[0]
    assert "Move" in body and "Child\\_1 5pm Piano" in body
    assert _order(update.message.reply_text.await_args)[0].startswith("Sat")


def test_the_event_being_moved_is_not_ranked_against_itself(six_calendars, monkeypatch):
    """Left in, the slot it currently occupies would score as a double
    booking - the one time it definitely does not clash with."""
    original = _existing()
    _stub(monkeypatch, [("child_1_enrichment", original)])
    update = MagicMock()
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text

    asyncio.run(main._present_amend_options(
        update, 1, original,
        # The first candidate IS where the event already sits.
        {"changes": {}, "slot_options": [_slot(MONDAY, 16, 0, 17, 0),
                                         _slot(SUNDAY, 14, 0, 15, 0)]},
        "child_1_enrichment",
    ))

    assert "already booked" not in update.message.reply_text.await_args.args[0]


def test_picking_a_slot_moves_the_event_to_it(six_calendars, monkeypatch):
    original = _existing()
    monkeypatch.setattr(ct, "get_event", lambda eid, key=None: original)
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))
    pending = {"event_id": "p", "calendar_key": "child_1_enrichment", "changes": {},
               "source_text": None,
               "options": [_slot(SATURDAY, 10, 0, 11, 0), _slot(SUNDAY, 14, 0, 15, 0)]}

    query = _press("amend_opt:0", monkeypatch, pending)

    sent = query.edit_message_text.await_args.args[0]
    assert "Sat 12 Sep" in sent and "10:00-11:00" in sent


def test_the_moved_event_is_retitled_for_its_new_time(six_calendars, monkeypatch):
    """Titles carry the time. Keeping "5pm" on an event moved to Saturday
    10am is the same self-contradiction the create path had."""
    original = _existing()
    monkeypatch.setattr(ct, "get_event", lambda eid, key=None: original)
    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))
    pending = {"event_id": "p", "calendar_key": "child_1_enrichment", "changes": {},
               "source_text": None,
               "options": [_slot(SATURDAY, 10, 0, 11, 0)]}

    query = _press("amend_opt:0", monkeypatch, pending)

    sent = query.edit_message_text.await_args.args[0]
    assert "11am Piano" in sent and "5pm Piano" in sent  # "Was: 5pm" -> "Update to: 11am"


def test_an_out_of_range_amend_option_changes_nothing(six_calendars, monkeypatch):
    patched = []
    monkeypatch.setattr(ct, "patch_event", lambda *a, **k: patched.append(a))
    pending = {"event_id": "p", "calendar_key": "child_1_enrichment", "changes": {},
               "options": [_slot(SATURDAY, 10, 0, 11, 0)]}

    query = _press("amend_opt:99", monkeypatch, pending)

    assert patched == []
    assert main.EXPIRED_REPLY in query.edit_message_text.await_args.args[0]


def test_other_changes_survive_the_slot_pick(six_calendars, monkeypatch):
    """"move piano to thursday or saturday, and grandparent_1 is collecting" must
    not lose the carer when a time is tapped."""
    changed = main._changes_for_slot({"pickup": "Grandparent_1"}, _slot(SATURDAY, 10, 0, 11, 0))
    assert changed["pickup"] == "Grandparent_1"
    assert changed["date"] == SATURDAY
    assert changed["start_time"] == "10:00:00"


def test_a_move_offering_only_times_is_not_refused(six_calendars):
    """A list of alternatives IS the change. Without this, "move piano to
    thursday 4pm or saturday 10am" was rejected for changing nothing."""
    result = ep._finalize({
        "action": "amend", "title": "piano",
        "slot_options": [{"date": "2026-09-10", "start_time": "16:00"},
                         {"date": "2026-09-12", "start_time": "10:00"}],
    }, "src")
    assert result["status"] == "ok"
    assert len(result["slot_options"]) == 2


def test_a_move_with_nothing_at_all_still_asks(six_calendars):
    result = ep._finalize({"action": "amend", "title": "piano"}, "src")
    assert result["status"] == "needs_clarification"


# ----- candidate times from a screenshot -----

def test_the_vision_prompt_allows_a_choice_of_times(six_calendars):
    """A centre's replacement-class notice listing three times is a common
    thing to forward. Read as three events to add, it silently creates two
    classes nobody asked for - so the image instruction has to name the
    difference, exactly as the text schema does."""
    assert "slot_options" in ep.INTERPRET_TOOL["function"]["parameters"]["properties"]
    assert "slot_options" in ep.IMAGE_INSTRUCTION
    # And says which reading is wrong, since "several times" and "several
    # dates" are the two things being told apart.
    assert "do NOT put them in dates" in ep.IMAGE_INSTRUCTION


def test_a_screenshot_offering_times_is_ranked_not_created(six_calendars, monkeypatch):
    """The image path shares _finalize with text, so a parse carrying
    slot_options must come out as a choice from either direction."""
    raw = {"action": "create", "title": "swimming replacement", "child": "child_1",
           "category": "enrichment",
           "slot_options": [{"date": "2026-09-10", "start_time": "16:00"},
                            {"date": "2026-09-12", "start_time": "10:00"},
                            {"date": "2026-09-13", "start_time": "14:00"}]}
    monkeypatch.setattr(ep, "_call_model", lambda content: raw)

    result = ep.interpret_image([{"type": "image_url", "image_url": {"url": "data:,"}}],
                                source_text="")

    assert len(result["slot_options"]) == 3
    assert len(result["occurrences"]) == 1  # nothing created by offering


def test_a_screenshot_with_one_time_is_still_an_ordinary_create(six_calendars, monkeypatch):
    """The overwhelmingly common case must not change."""
    raw = {"action": "create", "title": "swimming", "child": "child_1",
           "category": "enrichment", "date": "2026-09-10", "start_time": "16:00"}
    monkeypatch.setattr(ep, "_call_model", lambda content: raw)

    result = ep.interpret_image([{"type": "image_url", "image_url": {"url": "data:,"}}],
                                source_text="")

    assert not result.get("slot_options")
    assert result["status"] == "ok"


# ----- the shape the model actually returns -----
#
# As an array of {date, day, start_time, end_time} objects, gpt-4.1-mini
# looped on this field and emitted 326,000 characters of repeated entries
# before the response was cut off - unparseable JSON, and ~80k completion
# tokens billed for nothing. A flat array of short strings gives it far less
# room to run.

def test_a_slot_given_as_a_string_is_read(six_calendars):
    raw = {"slot_options": ["2026-09-10 16:00", "2026-09-12 10:00"]}
    slots = ep.resolve_slot_options(raw, TODAY)
    assert len(slots) == 2
    assert slots[0]["start"].hour == 16


def test_a_weekday_word_is_still_resolved_in_python(six_calendars):
    """The model is unreliable at date maths, so "thursday" must reach
    _resolve_when rather than being turned into a date by the model."""
    slots = ep.resolve_slot_options({"slot_options": ["thursday 16:00",
                                                      "friday 10:00"]}, TODAY)
    assert len(slots) == 2
    assert slots[0]["start"].date().weekday() == 3


def test_a_dict_is_still_accepted(six_calendars):
    """The model occasionally returns the old shape anyway, and dropping a
    candidate the user genuinely named over an encoding detail is worse than
    accepting both."""
    raw = {"slot_options": [{"date": "2026-09-10", "start_time": "16:00"},
                            {"date": "2026-09-12", "start_time": "10:00"}]}
    assert len(ep.resolve_slot_options(raw, TODAY)) == 2


def test_a_single_digit_hour_is_normalised(six_calendars):
    """"9:00" is what a model writes when asked for a time, and it is not
    valid isoformat - the earlier version raised on it."""
    slots = ep.resolve_slot_options({"slot_options": ["2026-09-10 9:00",
                                                      "2026-09-12 10:00"]}, TODAY)
    assert len(slots) == 2
    assert slots[0]["start"].hour == 9


def test_a_time_written_with_a_dot_is_read(six_calendars):
    slots = ep.resolve_slot_options({"slot_options": ["2026-09-10 16.30",
                                                      "2026-09-12 10:00"]}, TODAY)
    assert slots[0]["start"].minute == 30


def test_a_string_with_no_time_is_dropped(six_calendars):
    slots = ep.resolve_slot_options({"slot_options": ["thursday", "friday 10:00",
                                                      "saturday 11:00"]}, TODAY)
    assert len(slots) == 2


def test_the_schema_caps_the_array(six_calendars):
    """A hard maxItems as well as the Python cap. The Python side cannot stop
    the model generating them, only stop them being used - and the generation
    is what cost the tokens."""
    field = ep.INTERPRET_TOOL["function"]["parameters"]["properties"]["slot_options"]
    assert field["items"]["type"] == "string"
    assert field["maxItems"] == 6


def test_a_runaway_generation_is_capped(six_calendars):
    """The real cost of the loop was ~80k completion tokens, not the failure.
    max_tokens turns an expensive failure into a cheap one."""
    assert ep.MAX_PARSE_TOKENS <= 2000


def test_truncated_arguments_become_a_parse_error(six_calendars, monkeypatch):
    """A cut-off response left JSONDecodeError escaping from deep inside a
    handler. It belongs in the same handled path as "no tool call at all"."""
    class _Fn:
        arguments = '{"action": "create", "slot_options": ["2026-09-1'

    class _Call:
        function = _Fn()

    class _Msg:
        tool_calls = [_Call()]

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = None
        model = "test"

    monkeypatch.setattr(ep, "_get_client", lambda: type("C", (), {
        "chat": type("Ch", (), {
            "completions": type("Co", (), {
                "create": staticmethod(lambda **kw: _Resp())
            })()
        })()
    })())

    with pytest.raises(ep.ParseError):
        ep._call_model("anything")


def test_a_ranked_enrichment_slot_is_retitled_with_its_range(six_calendars, monkeypatch):
    """Slots go through the same _occurrence as an ordinary create, so an
    enrichment candidate given as a range carries time_anchor="range" and the
    chosen row is retitled with both ends - not with the first option's clock,
    and not collapsed back to the end time."""
    slots = ep.resolve_slot_options(
        {"slot_options": [{"date": "2026-09-12", "start_time": "10:00", "end_time": "11:00"},
                          {"date": "2026-09-13", "start_time": "14:00", "end_time": "15:00"}]},
        TODAY, "child_1_enrichment",
    )
    assert [s["time_anchor"] for s in slots] == ["range", "range"]

    monkeypatch.setattr(ct, "list_events_range_across", lambda s, e, keys=None: [])
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "travel_times_by_source", lambda: ({}, {}))
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: (
        {"title": "Child_1 10-11am Piano", "calendar_key": "child_1_enrichment",
         "location": None, "notes": None, "pickup": None, "source_text": None,
         "options": slots} if kind == "create_options" else None))
    query = MagicMock()
    query.data = "create_opt:1"
    query.from_user.id = ADMIN_ID
    query.message.chat_id = 1
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = ADMIN_ID
    update.effective_chat.id = 1
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)

    asyncio.run(main.handle_callback(update, MagicMock()))

    sent = query.edit_message_text.await_args.args[0]
    assert "2-3pm Piano" in sent      # the slot actually picked
    assert "10-11am" not in sent      # not the title the card was built with


def test_a_ranked_school_slot_keeps_its_end_only_title(six_calendars):
    """The rule is enrichment-only here too."""
    slots = ep.resolve_slot_options(
        {"slot_options": [{"date": "2026-09-12", "start_time": "10:00", "end_time": "11:00"},
                          {"date": "2026-09-13", "start_time": "14:00", "end_time": "15:00"}]},
        TODAY, "child_1_school",
    )
    assert [s["time_anchor"] for s in slots] == ["end", "end"]
