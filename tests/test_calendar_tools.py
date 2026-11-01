"""Pure-logic tests for calendar_tools: fuzzy title matching and date helpers.
No network calls - the Google API functions themselves are not exercised here."""
import datetime

import calendar_tools as ct


# ----- _title_matches -----

def test_exact_substring_matches():
    assert ct._title_matches("dentist", "Dentist appointment")


def test_case_insensitive():
    assert ct._title_matches("SWIMMING", "swimming class")


def test_all_tokens_present_in_any_order():
    assert ct._title_matches("chips potato", "eat potato chips")


def test_majority_of_tokens_is_enough():
    # 2 of 3 tokens present (0.67 >= 0.6 threshold) - filler words shouldn't block a match
    assert ct._title_matches("eat potato chips", "potato chips")


def test_minority_of_tokens_is_not_enough():
    assert not ct._title_matches("piano recital hall", "swimming class")


def test_empty_needle_never_matches():
    assert not ct._title_matches("", "anything")
    assert not ct._title_matches("   ", "anything")


def test_no_match_at_all():
    assert not ct._title_matches("dentist", "swimming class")


# ----- event_start_date -----

def test_all_day_event_date():
    event = {"start": {"date": "2026-07-25"}}
    assert ct.event_start_date(event) == datetime.date(2026, 7, 25)


def test_timed_event_date():
    event = {"start": {"dateTime": "2026-07-25T16:00:00+08:00"}}
    assert ct.event_start_date(event) == datetime.date(2026, 7, 25)


def test_event_without_start_returns_none():
    assert ct.event_start_date({}) is None
    assert ct.event_start_date({"start": {}}) is None


# ----- reminders (30 min before the end, plus a day before) -----

def _mins(start_h, start_m, end_h, end_m):
    day = datetime.date(2026, 8, 6)
    start = datetime.datetime.combine(day, datetime.time(start_h, start_m), tzinfo=ct.TIMEZONE)
    end = datetime.datetime.combine(day, datetime.time(end_h, end_m), tzinfo=ct.TIMEZONE)
    return ct._reminder_minutes(start, end)


def test_short_block_reminder_lands_30min_before_the_end():
    """The default 15-minute block ending at 16:00: 30 before the end is
    15:30, which is 15 minutes before the 15:45 start."""
    assert _mins(15, 45, 16, 0) == [1440, 15]


def test_long_event_reminder_clamps_to_the_start():
    """30 before the end of an hour-long event falls inside it, and Google
    cannot express a negative offset - so it fires at the start instead."""
    assert _mins(16, 0, 17, 0) == [1440, 0]


def test_exactly_30_minute_event_fires_at_start():
    assert _mins(16, 0, 16, 30) == [1440, 0]


def test_all_day_reminder_is_the_evening_before_not_midnight():
    """1440 would buzz at 00:00; 360 is 18:00 the previous evening."""
    assert ct._reminder_minutes(all_day=True) == [ct.ALL_DAY_EVENING_BEFORE_MINUTES]


def test_reminders_never_collapse_to_a_duplicate():
    """A 1-day-long timed event would produce 1440 twice; Google rejects
    duplicate overrides."""
    day = datetime.date(2026, 8, 6)
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=ct.TIMEZONE)
    mins = ct._reminder_minutes(start, start + datetime.timedelta(minutes=1440))
    assert len(mins) == len(set(mins))


def test_created_event_body_carries_both_reminders(monkeypatch):
    captured = {}

    class _Events:
        def insert(self, calendarId, body):
            captured["body"] = body
            return self

        def execute(self):
            return {"id": "ev1"}

    monkeypatch.setattr(ct, "_service", lambda: type("S", (), {"events": lambda self: _Events()})())
    monkeypatch.setattr(ct, "_calendar_id", lambda key=None: "cal")
    day = datetime.date(2026, 8, 6)
    start = datetime.datetime.combine(day, datetime.time(15, 45), tzinfo=ct.TIMEZONE)
    end = datetime.datetime.combine(day, datetime.time(16, 0), tzinfo=ct.TIMEZONE)
    ct.create_event("Child_2 4pm Piano", start_dt=start, end_dt=end)

    reminders = captured["body"]["reminders"]
    assert reminders["useDefault"] is False
    assert [o["minutes"] for o in reminders["overrides"]] == [1440, 15]
