"""Pure-logic tests for event_parser: date resolution, query windows,
create/amend finalization, and the 2.3 sanity bounds."""
import datetime

import event_parser as ep

TODAY = datetime.date(2026, 7, 19)  # a Sunday


# ----- _resolve_when -----

def test_explicit_date():
    assert ep._resolve_when("2026-08-01", None, TODAY) == (datetime.date(2026, 8, 1), None)


def test_invalid_date_string():
    assert ep._resolve_when("not-a-date", None, TODAY) == (None, "invalid_date")


def test_today_and_tonight():
    assert ep._resolve_when(None, "today", TODAY) == (TODAY, None)
    assert ep._resolve_when(None, "tonight", TODAY) == (TODAY, None)


def test_tomorrow_variants():
    expected = TODAY + datetime.timedelta(days=1)
    for word in ("tomorrow", "tmr", "tmrw"):
        assert ep._resolve_when(None, word, TODAY) == (expected, None)


def test_same_weekday_means_today():
    # saying "sunday" on a Sunday means today, not next week
    assert ep._resolve_when(None, "sunday", TODAY) == (TODAY, None)


def test_next_same_weekday_means_next_week():
    assert ep._resolve_when(None, "next sunday", TODAY) == (TODAY + datetime.timedelta(days=7), None)


def test_upcoming_weekday():
    # Monday is tomorrow relative to Sunday the 19th
    assert ep._resolve_when(None, "monday", TODAY) == (datetime.date(2026, 7, 20), None)


def test_unknown_day_reference():
    assert ep._resolve_when(None, "someday", TODAY) == (None, "invalid_day")


def test_no_input_at_all():
    assert ep._resolve_when(None, None, TODAY) == (None, None)


# ----- _query_window -----

def test_week_window():
    assert ep._query_window({"range": "week"}, TODAY) == (TODAY, TODAY + datetime.timedelta(days=6))


def test_month_window():
    assert ep._query_window({"range": "month"}, TODAY) == (TODAY, TODAY + datetime.timedelta(days=29))


def test_weekend_on_sunday_includes_current_weekend():
    saturday = TODAY - datetime.timedelta(days=1)
    assert ep._query_window({"range": "weekend"}, TODAY) == (saturday, saturday + datetime.timedelta(days=1))


def test_weekend_midweek_finds_coming_saturday():
    wednesday = datetime.date(2026, 7, 22)
    saturday = datetime.date(2026, 7, 25)
    assert ep._query_window({"range": "weekend"}, wednesday) == (saturday, saturday + datetime.timedelta(days=1))


def test_specific_day():
    assert ep._query_window({"day": "tomorrow"}, TODAY) == (
        TODAY + datetime.timedelta(days=1),
        TODAY + datetime.timedelta(days=1),
    )


def test_upcoming_is_open_ended():
    assert ep._query_window({}, TODAY) == (None, None)


# ----- _finalize_create -----

def test_create_without_title_asks():
    result = ep._finalize_create({"action": "create"}, TODAY, "src")
    assert result["status"] == "needs_clarification"


def test_create_without_date_asks():
    result = ep._finalize_create({"action": "create", "title": "Swimming"}, TODAY, "src")
    assert result["status"] == "needs_clarification"


def test_create_no_time_defaults_to_all_day():
    result = ep._finalize_create({"action": "create", "title": "Visit", "date": "2026-07-25"}, TODAY, "src")
    assert result["status"] == "ok"
    assert result["all_day"] is True
    assert result["date"] == datetime.date(2026, 7, 25)


def test_lone_time_is_read_as_the_end_time():
    """A single time is when the thing FINISHES, whichever field the model
    happened to put it in - the family keys in pickup times, not start times."""
    for field in ("start_time", "end_time"):
        result = ep._finalize_create(
            {"action": "create", "title": "Swim", "date": "2026-07-25", field: "16:00"}, TODAY, "src"
        )
        assert result["all_day"] is False, field
        assert result["end"].hour == 16 and result["end"].minute == 0, field
        assert result["end"] - result["start"] == datetime.timedelta(minutes=ep.EVENT_BLOCK_MINUTES), field


def test_lone_time_default_is_configurable_for_an_uncategorised_calendar(monkeypatch):
    """With no category to consult, LONE_TIME_MEANS_START is the fallback - for
    a family whose messages are mostly drop-offs. Per-message wording still
    wins over it."""
    raw = {"action": "create", "title": "Swim", "date": "2026-07-25", "end_time": "16:00"}
    monkeypatch.setattr(ep, "LONE_TIME_MEANS_START", True)
    assert ep._finalize_create(raw, TODAY, "src")["start"].hour == 16
    monkeypatch.setattr(ep, "LONE_TIME_MEANS_START", False)
    assert ep._finalize_create(raw, TODAY, "src")["start"].hour == 15


def test_explicit_range_is_left_alone():
    """The short block is only a stand-in for a missing start. A stated range
    must survive intact, or a real two-hour commitment would shrink to 15min."""
    result = ep._finalize_create(
        {"action": "create", "title": "Camp", "date": "2026-07-25",
         "start_time": "09:00", "end_time": "11:00"},
        TODAY, "src",
    )
    assert result["start"].hour == 9 and result["end"].hour == 11


def test_create_far_future_date_asks_for_confirmation():
    result = ep._finalize_create({"action": "create", "title": "X", "date": "3000-01-01"}, TODAY, "src")
    assert result["status"] == "needs_clarification"
    assert "3000-01-01" in result["question"]


def test_create_title_clipped_to_max_length():
    result = ep._finalize_create(
        {"action": "create", "title": "A" * 10_000, "date": "2026-07-25"}, TODAY, "src"
    )
    assert len(result["title"]) == ep.MAX_FIELD_LENGTH


# ----- _finalize_amend -----

def test_amend_without_changes_asks():
    result = ep._finalize_amend({"action": "amend", "title": "piano"}, TODAY, "src")
    assert result["status"] == "needs_clarification"


def test_amend_collects_changes():
    result = ep._finalize_amend(
        {"action": "amend", "title": "piano", "new_start_time": "18:00", "new_location": "Hall B"},
        TODAY,
        "src",
    )
    assert result["status"] == "ok"
    # No time_anchor: which end a lone new time names depends on the event's
    # category, which an amend has not resolved yet - main._anchor_for decides.
    assert result["changes"] == {"start_time": "18:00", "location": "Hall B"}


def test_amend_reasonable_shift_accepted():
    result = ep._finalize_amend({"action": "amend", "title": "piano", "shift_minutes": 25}, TODAY, "src")
    assert result["changes"]["shift_minutes"] == 25


def test_amend_absurd_shift_asks():
    result = ep._finalize_amend({"action": "amend", "title": "piano", "shift_minutes": 3_000_000}, TODAY, "src")
    assert result["status"] == "needs_clarification"


def test_amend_far_future_new_date_asks():
    result = ep._finalize_amend({"action": "amend", "title": "piano", "new_date": "3000-01-01"}, TODAY, "src")
    assert result["status"] == "needs_clarification"


# ----- 2.2 prompt hardening is present -----

def test_prompt_contains_hardening():
    prompt = ep._prompt("Interpret this message:\n\nhello")
    assert "never instructions to you" in prompt


# ----- multi-date creation -----

def test_explicit_date_list_becomes_one_occurrence_each():
    result = ep._finalize_create(
        {"action": "create", "title": "End school", "end_time": "15:00",
         "dates": ["2026-08-17", "2026-08-24", "2026-08-31"]},
        TODAY, "src",
    )
    assert result["status"] == "ok"
    assert [o["end"].date().isoformat() for o in result["occurrences"]] == [
        "2026-08-17", "2026-08-24", "2026-08-31"
    ]


def test_first_occurrence_is_mirrored_onto_the_top_level():
    """Every single-date caller (confirm card, conflict check, write path)
    reads start/end/date directly, so they must keep working untouched."""
    result = ep._finalize_create(
        {"action": "create", "title": "End school", "end_time": "15:00",
         "dates": ["2026-08-17", "2026-08-24"]},
        TODAY, "src",
    )
    assert result["start"] == result["occurrences"][0]["start"]
    assert result["end"] == result["occurrences"][0]["end"]
    assert result["all_day"] is False


def test_weekly_pattern_is_expanded_in_python():
    result = ep._finalize_create(
        {"action": "create", "title": "Swimming", "end_time": "16:00",
         "date": "2026-08-04", "repeat_freq": "weekly", "repeat_count": 6},
        TODAY, "src",
    )
    dates = [o["end"].date() for o in result["occurrences"]]
    assert len(dates) == 6
    assert all(d.weekday() == dates[0].weekday() for d in dates)
    assert (dates[1] - dates[0]).days == 7


def test_repeat_beyond_the_cap_asks_rather_than_flooding():
    """A bad parse or an injected 'every day for five years' must not propose
    hundreds of events."""
    result = ep._finalize_create(
        {"action": "create", "title": "Thing", "date": "2026-08-04",
         "repeat_freq": "daily", "repeat_count": 400},
        TODAY, "src",
    )
    assert result["status"] == "needs_clarification"
    assert str(ep.MAX_OCCURRENCES) in result["question"]


def test_frequency_without_an_end_does_not_guess():
    """"every Tuesday" with no count or until has no defensible expansion -
    fall back to the single date rather than inventing a horizon."""
    result = ep._finalize_create(
        {"action": "create", "title": "Swimming", "end_time": "16:00",
         "date": "2026-08-04", "repeat_freq": "weekly"},
        TODAY, "src",
    )
    assert len(result["occurrences"]) == 1


def test_all_day_multi_date():
    result = ep._finalize_create(
        {"action": "create", "title": "Term break", "dates": ["2026-09-01", "2026-09-02"]},
        TODAY, "src",
    )
    assert all(o["all_day"] for o in result["occurrences"])
    assert [o["date"].isoformat() for o in result["occurrences"]] == ["2026-09-01", "2026-09-02"]


def test_unparseable_date_in_a_list_does_not_lose_the_others():
    result = ep._finalize_create(
        {"action": "create", "title": "X", "dates": ["2026-08-17", "garbage", "2026-08-24"]},
        TODAY, "src",
    )
    assert len(result["occurrences"]) == 2


def test_duplicate_dates_are_collapsed():
    result = ep._finalize_create(
        {"action": "create", "title": "X", "dates": ["2026-08-17", "2026-08-17"]},
        TODAY, "src",
    )
    assert len(result["occurrences"]) == 1


# ----- generic references for delete/amend -----

def test_delete_without_a_title_is_allowed_when_a_day_is_given():
    """"cancel her appointment today" names no event, but a day is enough to
    offer a pick list."""
    result = ep._finalize({"action": "delete", "day": "today"}, "src")
    assert result["status"] == "ok"
    assert result["search_text"] == ""
    assert result["prefer_date"] is not None


def test_delete_with_neither_title_nor_day_still_asks():
    result = ep._finalize({"action": "delete"}, "src")
    assert result["status"] == "needs_clarification"


def test_amend_without_a_title_is_allowed_when_a_day_is_given():
    result = ep._finalize(
        {"action": "amend", "day": "today", "new_end_time": "18:00"}, "src"
    )
    assert result["status"] == "ok"
    assert result["search_text"] == ""


# ----- travel: statements about the map, not the diary -----
#
# The MODEL's half of this - telling "swimming at 4pm" from "swimming is 20
# minutes from school" - is checked in scripts/smoke_parser.py, because these
# tests hand _finalize a pre-baked dict and never call the model at all. What
# is checked here is everything after the classification.

def _travel(source=None, **raw):
    """`source` defaults to a sentence naming whatever places are being
    passed, because _finalize_travel now verifies that the message actually
    mentions them - a fixture whose source text says "src" would have every
    place dropped as invented."""
    if source is None:
        named = [str(raw.get(k)) for k in ("from_place", "to_place", "new_place")
                 if raw.get(k)]
        source = " to ".join(named) or "src"
    return ep._finalize({"action": "travel", **raw}, source)


def test_two_places_and_a_duration_is_ready_to_apply():
    result = _travel(from_place="school", to_place="Centre_1", travel_minutes=25)
    assert result["status"] == "ok"
    assert result["from_place"] == "school" and result["to_place"] == "Centre_1"
    assert result["travel_minutes"] == 25


def test_place_names_are_passed_through_untouched():
    """Turning a name into a place key happens in main, against the one
    resolver that already exists there. A second one here could disagree, and
    then a journey typed one way and said another would land in two rows."""
    result = _travel(from_place="  Child_1's School ", to_place="centre_1", travel_minutes=10)
    assert result["from_place"] == "Child_1's School"


def test_one_place_asks_which_other_one():
    """Guessing home would quietly record the wrong journey, and nothing on
    screen would say so."""
    result = _travel(to_place="Centre_1", travel_minutes=20)
    assert result["status"] == "needs_clarification"
    assert result["known_place"] == "Centre_1" and result["travel_minutes"] == 20
    assert "from where" in result["question"]


def test_no_duration_asks_for_one():
    result = _travel(from_place="school", to_place="Centre_1")
    assert result["status"] == "needs_clarification"
    assert "minutes" in result["question"]


def test_a_nonsense_duration_is_refused_not_stored():
    """A stray year read as minutes would be stored as a number so large it
    suppresses every future warning on that journey."""
    result = _travel(from_place="school", to_place="Centre_1", travel_minutes=2026)
    assert result["status"] == "needs_clarification"


def test_a_negative_duration_is_refused():
    result = _travel(from_place="school", to_place="Centre_1", travel_minutes=-5)
    assert result["status"] == "needs_clarification"


def test_a_duration_given_as_text_still_counts():
    result = _travel(from_place="school", to_place="Centre_1", travel_minutes="25")
    assert result["status"] == "ok" and result["travel_minutes"] == 25


def test_introducing_a_place_is_its_own_request():
    result = _travel(new_place="Centre_2")
    assert result["status"] == "ok" and result["new_place"] == "Centre_2"


def test_a_place_introduced_with_a_journey_is_added_first():
    """The add wins. Resolving the journey half first would look it up against
    a place that does not exist yet and fail with "I don't know Chess
    Academy" - so the message that introduces it would be the one message that
    could not work."""
    result = _travel(new_place="Centre_2", from_place="school", travel_minutes=20)
    assert result["status"] == "ok" and result["new_place"] == "Centre_2"


def test_forgetting_needs_both_ends():
    result = _travel(forget_travel=True, from_place="school")
    assert result["status"] == "needs_clarification"


def test_forgetting_both_ends_is_ready_to_apply():
    result = _travel(forget_travel=True, from_place="school", to_place="Centre_1")
    assert result["status"] == "ok" and result["forget"] is True


def test_a_place_the_message_never_mentions_is_dropped():
    """Live failure 2026-08-17: "centre_1 is 20 minutes away" came back as
    Home -> Centre_1. "Away" implies home to a language model, and Home is in
    the place list the prompt supplies - so it filled in an end the user never
    said. A plausible journey recorded against a distance nobody gave is the
    worst kind of wrong answer, so the instruction is verified, not trusted."""
    result = _travel(source="centre_1 is 20 minutes away",
                     from_place="Home", to_place="Centre_1", travel_minutes=20)
    assert result["status"] == "needs_clarification"
    assert result["known_place"] == "Centre_1"


def test_a_shortened_place_name_still_counts_as_stated():
    """"school" standing in for "Child_1's school" is a normalisation, not an
    invention - dropping it would make the guard worse than the problem."""
    result = _travel(source="school to centre_1 takes 25 minutes",
                     from_place="Child_1's school", to_place="Centre_1", travel_minutes=25)
    assert result["status"] == "ok"


def test_the_check_is_skipped_when_there_is_no_text():
    """A screenshot has no words to check against, and refusing every place on
    that basis would disable the feature for images entirely."""
    result = _travel(source="", from_place="Home", to_place="Centre_1", travel_minutes=20)
    assert result["status"] == "ok"


# ----- dates the model wrote loosely -----

def test_a_listed_date_in_human_form_is_still_read():
    """Live failure 2026-08-17: asked for three dates the model returned
    "10 Sep" rather than ISO, all three were dropped, and the whole request
    fell through to "when is that?" - losing the message over a formatting
    preference."""
    dates = ep._parse_listed_dates(["10 Sep", "17 Sep", "24 Sep"], TODAY)
    assert len(dates) == 3
    assert dates[0] == datetime.date(2026, 9, 10)


def test_iso_dates_still_work():
    dates = ep._parse_listed_dates(["2026-09-10", "2026-09-17"], TODAY)
    assert dates == [datetime.date(2026, 9, 10), datetime.date(2026, 9, 17)]


def test_month_first_and_ordinals_are_read():
    assert ep._loose_date("Sep 10", TODAY) == datetime.date(2026, 9, 10)
    assert ep._loose_date("10th September", TODAY) == datetime.date(2026, 9, 10)


def test_a_bare_day_and_month_means_the_next_one():
    """"10 Jan" in July means next January, not one six months gone."""
    assert ep._loose_date("10 Jan", datetime.date(2026, 7, 19)).year == 2027


def test_a_recent_past_date_is_left_alone():
    """Adding something from last week is ordinary; rolling it forward a year
    would be a surprising thing to do to it."""
    assert ep._loose_date("15 Jul", datetime.date(2026, 7, 19)) == datetime.date(2026, 7, 15)


def test_nonsense_is_still_dropped():
    assert ep._loose_date("not-a-date", TODAY) is None
    assert ep._loose_date("32 Sep", TODAY) is None


# ----- range titles for enrichment classes -----
#
# The rule itself is exercised against real calendars in test_multi_calendar;
# these pin the string handling it rests on, which has no calendar in it.


def _at(hour, minute=0):
    return datetime.datetime(2026, 9, 11, hour, minute)


def test_format_clock_range_shapes():
    """The meridiem is dropped from the first half only when both share it,
    which is how a person writes it."""
    assert ep.format_clock_range(_at(16), _at(17)) == "4-5pm"
    assert ep.format_clock_range(_at(9), _at(11)) == "9-11am"
    assert ep.format_clock_range(_at(11), _at(13)) == "11am-1pm"
    assert ep.format_clock_range(_at(15, 30), _at(17)) == "3.30-5pm"
    assert ep.format_clock_range(_at(16), _at(17, 30)) == "4-5.30pm"


def test_a_range_title_round_trips():
    """THE load-bearing one. format_title and split_title are inverses, and a
    clock the splitter cannot recognise is left on the front of the ACTIVITY -
    so the next amend stamps a second clock on top of it."""
    title = ep.format_title("piano", "Child_2", (_at(16), _at(17)))
    assert title == "Child_2 4-5pm Piano"
    activity, gate = ep.split_title(title, "Child_2")
    assert activity == "Piano"
    assert gate is None
    # ... and rebuilding from what came back is stable, not cumulative
    assert ep.format_title(activity, "Child_2", (_at(16), _at(17))) == title


def test_a_range_title_round_trips_with_a_gate():
    title = ep.format_title("assembly", "Child_1", (_at(9), _at(11)), gate="2")
    assert title == "Child_1 9-11am Gate 2 Assembly"
    assert ep.split_title(title, "Child_1") == ("Assembly", "2")


def test_the_clock_regex_knows_ranges_but_not_bare_numbers():
    for good in ("4pm", "3.30pm", "4-5pm", "9-11am", "11am-1pm", "3.30-5pm"):
        assert ep._CLOCK_RE.match(good), good
    for bad in ("4", "4-5", "Piano", "4-5xm", ""):
        assert not ep._CLOCK_RE.match(bad), bad


def test_title_clock_reads_a_range_back():
    """What /upcoming leans on to lead each line with a time."""
    assert ep.title_clock("Child_2 4-5pm Piano", "Child_2") == "4-5pm"
    assert ep.title_clock("Child_2 5pm Piano", "Child_2") == "5pm"


def test_range_lead_clock_gives_the_start_with_its_meridiem():
    """The list view shows the start alone so the column stays aligned; the
    stored title keeps the full range."""
    assert ep.range_lead_clock("4-5pm") == "4pm"
    assert ep.range_lead_clock("9-11am") == "9am"
    assert ep.range_lead_clock("11am-1pm") == "11am"
    assert ep.range_lead_clock("3.30-5pm") == "3.30pm"
    # a plain clock and a missing one pass straight through
    assert ep.range_lead_clock("5pm") == "5pm"
    assert ep.range_lead_clock(None) is None


def test_title_time_returns_both_ends_for_a_range():
    occ = ep._occurrence(datetime.date(2026, 9, 11), datetime.time(16), datetime.time(17),
                         range_title=True)
    assert occ["time_anchor"] == "range"
    assert ep.title_time(occ) == (occ["start"], occ["end"])
    # ... and without the flag nothing changes
    plain = ep._occurrence(datetime.date(2026, 9, 11), datetime.time(16), datetime.time(17))
    assert plain["time_anchor"] == "end"
    assert ep.title_time(plain) == plain["end"]


def test_a_half_built_range_does_not_crash_format_title():
    """title_time hands over whatever the occurrence holds. A pair with a None
    in it must degrade to no clock, not raise inside format_clock."""
    assert ep.format_title("piano", "Child_2", (None, _at(17))) == "Child_2 Piano"
    assert ep.format_title("piano", "Child_2", (None, None)) == "Child_2 Piano"
