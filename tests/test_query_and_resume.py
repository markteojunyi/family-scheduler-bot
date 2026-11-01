"""Two NL improvements:
  - "is Child_1 free Saturday morning?" -> a yes/no, limited to the part of day
  - a typed answer to a clarifying question is resumed against the stalled
    parse, not glued onto the original message and re-parsed
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
SAT = datetime.date(2026, 9, 12)


@pytest.fixture
def six_calendars(monkeypatch):
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")


def _at(date, h, m=0):
    return datetime.datetime.combine(date, datetime.time(h, m), tzinfo=ct.TIMEZONE)


def _ev(summary, date, sh, eh):
    return {"summary": summary,
            "start": {"dateTime": _at(date, sh).isoformat()},
            "end": {"dateTime": _at(date, eh).isoformat()}}


# ===== time-of-day + availability =====

@pytest.mark.parametrize("part,first_hour", [
    ("morning", 6), ("afternoon", 12), ("evening", 17),
])
def test_part_of_day_windows(part, first_hour):
    tf, tt = ep._query_time_window({"part_of_day": part})
    assert tf.hour == first_hour


def test_at_time_becomes_an_hour_window():
    tf, tt = ep._query_time_window({"at_time": "16:00"})
    assert (tf.hour, tt.hour) == (16, 17)


def test_no_time_of_day_is_none():
    assert ep._query_time_window({}) == (None, None)


def test_finalize_query_carries_the_time_window(six_calendars):
    r = ep._finalize({"action": "query", "child": "Child_1", "day": "saturday",
                      "part_of_day": "morning", "asks_free": True}, "src")
    assert r["asks_free"] is True
    assert r["time_from"] == datetime.time(6, 0)
    assert r["time_to"] == datetime.time(12, 0)


def test_all_day_event_always_counts_as_a_clash():
    entry = ("Child_1 — School", {"start": {"date": "2026-09-12"}, "end": {"date": "2026-09-13"},
                                "summary": "Sports day"})
    assert main._event_in_time_window(entry, datetime.time(6), datetime.time(12)) is True


def _run_query(monkeypatch, result, events):
    monkeypatch.setattr(ct, "list_events_range_all", lambda s, e, keys=None: list(events))
    monkeypatch.setattr(ct, "list_events_range", lambda s, e, key=None: list(events))
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    asyncio.run(main._handle_query(update, result))
    return update.message.reply_text.await_args.args[0]


def _free_result(**over):
    base = {"action": "query", "status": "ok", "start": SAT, "end": SAT,
            "calendar_key": ["child_1_school", "child_1_enrichment", "child_1_others"],
            "asks_free": True, "time_from": datetime.time(6), "time_to": datetime.time(12)}
    base.update(over)
    return base


def test_free_when_nothing_overlaps(six_calendars, monkeypatch):
    text = _run_query(monkeypatch, _free_result(),
                      [("Child_1 — Enrichment", _ev("Child_1 3pm Swim", SAT, 14, 15))])
    assert text.startswith("✅") and "is free" in text and "morning" in text


def test_not_free_lists_the_clash(six_calendars, monkeypatch):
    text = _run_query(monkeypatch, _free_result(),
                      [("Child_1 — Enrichment", _ev("Child_1 10am Swim", SAT, 9, 10))])
    assert text.startswith("⚠️") and "isn't free" in text
    assert "Swim" in text


def test_a_plain_query_still_lists(six_calendars, monkeypatch):
    text = _run_query(monkeypatch, _free_result(asks_free=False),
                      [("Child_1 — Enrichment", _ev("Child_1 10am Swim", SAT, 9, 10))])
    assert "isn't free" not in text and "Swim" in text


# ===== resume a stalled parse instead of gluing =====

def test_interpret_attaches_raw_to_a_clarification(six_calendars, monkeypatch):
    monkeypatch.setattr(ep, "_call_model",
                        lambda content, **kw: {"action": "create", "title": "swimming", "child": "Child_2"})
    r = ep.interpret("add swimming for child_2")
    assert r["status"] == "needs_clarification"
    assert r["raw"]["title"] == "swimming"


def test_resume_fills_the_gap_and_locks_the_action(six_calendars, monkeypatch):
    raw = {"action": "create", "title": "swimming", "child": "Child_2", "category": "enrichment"}
    # The model, shown only the answer, must not be able to re-classify.
    def fake(content, **kw):
        assert "add swimming for child_2" not in content, "original wording re-fed to the model"
        return {"action": "amend", "day": "tomorrow", "end_time": "16:00"}
    monkeypatch.setattr(ep, "_call_model", fake)

    r = ep.resume_with_answer(raw, "add swimming for child_2", "When is swimming?", "tomorrow 4pm")
    assert r["action"] == "create"          # locked, not the model's "amend"
    assert r["status"] == "ok"
    assert r["occurrences"][0]["start"].hour == 16


def test_resume_does_not_overwrite_a_filled_field(six_calendars, monkeypatch):
    raw = {"action": "create", "title": "swimming", "child": "Child_2", "category": "enrichment"}
    monkeypatch.setattr(ep, "_call_model",
                        lambda content, **kw: {"title": "PIANO", "day": "monday", "end_time": "17:00"})
    r = ep.resume_with_answer(raw, "add swimming for child_2", "When?", "monday 5pm")
    assert "Swimming" in r["title"] and "PIANO" not in r["title"].upper().replace("SWIMMING", "")


def test_handle_text_resumes_when_raw_is_stashed(six_calendars, monkeypatch):
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main, "_begin_llm_call", AsyncMock(return_value=(True, 1)))
    monkeypatch.setattr(main, "_show_typing", AsyncMock())
    monkeypatch.setattr(main, "_record_tokens", AsyncMock())
    monkeypatch.setattr(main.db, "pop_pending", lambda cid, kind: (
        {"text": "add swimming for child_2", "raw": {"action": "create", "title": "swimming"},
         "question": "When?"} if kind == "clarification" else None))
    calls = {}
    monkeypatch.setattr(main, "_resume_answer",
                        lambda raw, orig, q, ans: calls.setdefault("resume", (orig, ans)) or ({"action": "create", "status": "ok"}, None))
    monkeypatch.setattr(main, "_parse_text", lambda t: calls.setdefault("parse", t) or ({}, None))
    monkeypatch.setattr(main, "_dispatch", AsyncMock())

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = ADMIN_ID
    update.message.text = "tomorrow 4pm"
    update.message.reply_text = AsyncMock()
    asyncio.run(main.handle_text(update, MagicMock()))

    assert calls.get("resume") == ("add swimming for child_2", "tomorrow 4pm")
    assert "parse" not in calls  # the glue-and-reparse path was not taken
