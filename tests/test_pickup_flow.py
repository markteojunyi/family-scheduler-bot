"""Phase B: the roster wired into cards, writes, /pickups and notifications.

test_roster.py covers the arithmetic. This covers whether the family ever
actually SEES it - which is a different question and the one that decides
whether the feature is worth anything.
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

MONDAY = datetime.date(2026, 9, 7)
SATURDAY = datetime.date(2026, 9, 12)


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


def _event(summary, date, start_h, end_h, end_m=0, event_id="ev"):
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": _at(date, start_h).isoformat()},
        "end": {"dateTime": _at(date, end_h, end_m).isoformat()},
    }


def _mock_update(user_id=ADMIN_ID, chat_id=1):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    return update


def _create_result(child_key, title, date, start_h, end_h):
    occurrence = {"all_day": False, "start": _at(date, start_h), "end": _at(date, end_h), "date": None}
    return {"action": "create", "status": "ok", "title": title, "location": None,
            "notes": None, "calendar_key": child_key, "occurrences": [occurrence], **occurrence}


def _stub_calendar(monkeypatch, existing):
    """Only list_events_range_across is stubbed - the roster logic underneath
    stays real, so these tests fail if the detector's behaviour changes."""
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(
        ct, "list_events_range_across", lambda start, end, keys=None: list(existing)
    )


# ----- the confirm card -----

def test_saturday_clash_is_shown_once_the_carer_is_known(six_calendars, monkeypatch):
    """The whole point: the warning lands where a tap can still fix it. On a
    Saturday that is one step later now - the card asks who is collecting
    first, then shows the clash for whoever was chosen."""
    _stub_calendar(monkeypatch, [("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="x"))])
    result = _create_result("child_1_enrichment", "Child_1 10.15am Swimming", SATURDAY, 9, 10)
    result["end"] = result["occurrences"][0]["end"] = _at(SATURDAY, 10, 15)

    first = _mock_update()
    asyncio.run(main._show_create_confirm(first, 1, result))
    assert "Who's collecting on Saturday?" in first.message.reply_text.await_args.args[0]

    chosen = _mock_update()
    asyncio.run(main._show_create_confirm(chosen, 1, {**result, "pickup": "Parent_2"}))
    text = chosen.message.reply_text.await_args.args[0]
    assert "⚠️" in text
    assert "Parent\\_2 is needed twice" in text
    assert "Child\\_1" in text and "Child\\_2" in text


def test_the_same_pair_on_a_weekday_is_not_flagged(six_calendars, monkeypatch):
    """Two adults on a weekday. If this warned, the family would learn to
    ignore warnings - which costs more than the feature is worth."""
    _stub_calendar(monkeypatch, [("child_2_school", _event("Child_2 4pm End school", MONDAY, 15, 16, event_id="x"))])
    update = _mock_update()
    result = _create_result("child_1_enrichment", "Child_1 4pm Swimming", MONDAY, 15, 16)

    asyncio.run(main._show_create_confirm(update, 1, result))

    assert "⚠️" not in update.message.reply_text.await_args.args[0]


def test_a_calendar_failure_never_blocks_the_card(six_calendars, monkeypatch):
    """A card that cannot warn beats no card at all, so the lookup swallows
    its own errors. Pinned because that swallowing already hid a live bug once
    (see conftest's no_real_google_api)."""
    def boom(*a, **k):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(ct, "list_events_range_across", boom)
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _create_result("child_1_enrichment", "Child_1 4pm Swimming", SATURDAY, 15, 16)))

    text = update.message.reply_text.await_args.args[0]
    assert "Child\\_1 4pm Swimming" in text
    assert "⚠️" not in text


def test_both_children_are_fetched_now_that_transitions_matter(six_calendars, monkeypatch):
    """This used to fetch only the OTHER child, because a pickup clash is by
    definition across two children and same-child overlaps were already found
    by find_conflicts_for_child.

    A tight transition is the opposite shape: one child, two events that do
    NOT overlap, on two different calendars (school then enrichment). Nothing
    else looks for it, so this child's calendars have to be read too. The two
    checks still cannot report the same problem in different words - one is
    about overlapping events, the other only ever about non-overlapping ones.
    """
    seen = {}
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)

    def capture(start, end, keys=None):
        seen["keys"] = keys
        return []

    monkeypatch.setattr(ct, "list_events_range_across", capture)
    asyncio.run(main._show_create_confirm(_mock_update(), 1, _create_result("child_1_enrichment", "Child_1 4pm Swim", SATURDAY, 15, 16)))

    assert any(k.startswith("child_2_") for k in seen["keys"])
    assert any(k.startswith("child_1_") for k in seen["keys"])
    # Still one read per calendar and no duplicates - the cost concern behind
    # the original version of this test has not gone away, it just no longer
    # justifies skipping the child being written to.
    assert len(seen["keys"]) == len(set(seen["keys"]))


def test_an_amend_is_never_compared_against_the_event_it_moves(six_calendars, monkeypatch):
    """Now that the same child's calendars are read, the booking being moved
    comes back with them. Left in, the proposal would be checked against its
    own current slot and report a clash with itself - so it is dropped by id."""
    moving = _event("Child_1 4pm Swim", SATURDAY, 15, 16, event_id="moving")
    _stub_calendar(monkeypatch, [("child_1_enrichment", moving)])

    clashes, journeys = main._pickup_clashes(
        "child_1_enrichment", "Child_1 4pm Swim",
        [{"start": _at(SATURDAY, 15), "end": _at(SATURDAY, 16), "all_day": False}],
        None, "moving",
    )
    assert clashes == []


def test_a_clash_the_calendar_already_had_is_not_reported(six_calendars, monkeypatch):
    """Two existing Saturday events already collide. Adding an unrelated
    Tuesday event must not re-raise that - the card asks about one thing and
    should report on that one thing."""
    _stub_calendar(monkeypatch, [
        ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="x")),
        ("child_2_others", _event("Child_2 10.15am Party", SATURDAY, 9, 10, end_m=15, event_id="y")),
    ])
    update = _mock_update()

    asyncio.run(main._show_create_confirm(update, 1, _create_result("child_1_school", "Child_1 4pm End school", MONDAY, 15, 16)))

    assert "⚠️" not in update.message.reply_text.await_args.args[0]


# ----- Pickup: stamped onto the event -----

def test_created_events_carry_the_rostered_carer(six_calendars):
    pending = {"calendar_key": "child_2_school", "title": "Child_2 4pm End school",
               "start": _at(MONDAY, 15), "end": _at(MONDAY, 16), "notes": None}
    assert "Pickup: Parent_1" in main._event_description(pending)
    assert "Type: School" in main._event_description(pending)


def test_the_carer_is_resolved_per_date_not_per_batch(six_calendars):
    """A weekly class running into a Saturday is collected by someone else
    that day - which the roster only knows if it is asked about each date."""
    pending = {"calendar_key": "child_2_enrichment", "title": "Child_2 10am Piano", "notes": None}
    weekday = {"start": _at(MONDAY, 9), "end": _at(MONDAY, 10)}
    weekend = {"start": _at(SATURDAY, 9), "end": _at(SATURDAY, 10)}
    assert "Pickup: Parent_1" in main._event_description(pending, weekday)
    assert "Pickup: Parent_2" in main._event_description(pending, weekend)


def test_notes_survive_alongside_the_schema(six_calendars):
    pending = {"calendar_key": "child_2_school", "title": "Child_2 4pm End school",
               "start": _at(MONDAY, 15), "end": _at(MONDAY, 16), "notes": "bring swimsuit"}
    assert "bring swimsuit" in main._event_description(pending)


def test_what_is_written_can_be_read_back(six_calendars):
    """The stamped Pickup: is the same field the detector reads as an
    override, so a round-trip failure would silently unhook the whole thing."""
    pending = {"calendar_key": "child_2_school", "title": "Child_2 4pm End school",
               "start": _at(MONDAY, 15), "end": _at(MONDAY, 16), "notes": None}
    description = main._event_description(pending)
    assert roster.pickup_override({"description": description}) == "Parent_1"


# ----- /pickups -----

def test_pickups_lists_who_collects_whom(six_calendars, monkeypatch):
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(
        ct, "list_events_range_across",
        lambda start, end, keys=None: [
            ("child_2_school", _event("Child_2 4pm End school", MONDAY, 15, 16, event_id="a")),
            ("child_1_enrichment", _event("Child_1 5pm Swimming", MONDAY, 16, 17, event_id="b")),
        ],
    )
    update = _mock_update()
    asyncio.run(main.pickups(update, MagicMock()))

    text = update.message.reply_text.await_args.args[0]
    assert "Parent\\_1" in text and "Helper\\_1" in text
    assert "End school" in text and "Swimming" in text


def test_pickups_surfaces_a_clash_the_bot_never_saw_created(six_calendars, monkeypatch):
    """The reason this command exists: both parents can edit Google Calendar
    directly, and those changes never pass a confirm card."""
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(
        ct, "list_events_range_across",
        lambda start, end, keys=None: [
            ("child_2_enrichment", _event("Child_2 10am Piano", SATURDAY, 9, 10, event_id="a")),
            ("child_1_enrichment", _event("Child_1 10.15am Swim", SATURDAY, 9, 10, end_m=15, event_id="b")),
        ],
    )
    update = _mock_update()
    asyncio.run(main.pickups(update, MagicMock()))

    text = update.message.reply_text.await_args.args[0]
    assert "Needs sorting out" in text
    assert "Parent\\_2 is needed twice" in text


def test_pickups_ignores_strangers(six_calendars, monkeypatch):
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: None)
    update = _mock_update(user_id=404)
    asyncio.run(main.pickups(update, MagicMock()))
    update.message.reply_text.assert_not_awaited()


# ----- telling the other adult -----

def _context():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


def test_the_other_adult_is_told_when_a_clash_is_accepted(six_calendars, monkeypatch):
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {"111": "admin", "222": "editor"})
    context = _context()

    asyncio.run(main._tell_the_other_adult(context, "111", {"pickup_clash": "Parent_2 is needed twice"}))

    assert context.bot.send_message.await_count == 1
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 222
    assert "Parent_2 is needed twice" in context.bot.send_message.await_args.kwargs["text"]


def test_nobody_is_told_when_there_was_no_clash(six_calendars, monkeypatch):
    """Not a running commentary on the calendar - only the case that needs
    two people to agree."""
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {"111": "admin", "222": "editor"})
    context = _context()
    asyncio.run(main._tell_the_other_adult(context, "111", {"pickup_clash": None}))
    context.bot.send_message.assert_not_awaited()


def test_the_confirmer_is_not_told_about_their_own_change(six_calendars, monkeypatch):
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {"111": "admin"})
    context = _context()
    asyncio.run(main._tell_the_other_adult(context, "111", {"pickup_clash": "x"}))
    context.bot.send_message.assert_not_awaited()


def test_readers_are_never_told(six_calendars, monkeypatch):
    """Grandparents do not need to be pulled into a coordination problem."""
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {"111": "admin", "333": "reader"})
    context = _context()
    asyncio.run(main._tell_the_other_adult(context, "111", {"pickup_clash": "x"}))
    context.bot.send_message.assert_not_awaited()


def test_a_blocked_recipient_does_not_fail_the_write(six_calendars, monkeypatch):
    """The write already succeeded and was audited by this point."""
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {"111": "admin", "222": "editor"})
    context = _context()
    context.bot.send_message = AsyncMock(side_effect=RuntimeError("bot blocked"))
    asyncio.run(main._tell_the_other_adult(context, "111", {"pickup_clash": "x"}))  # must not raise
