"""Tests for main.py: amend logic, Markdown escaping, secret redaction, and
handler behavior (permissions, rate limit) via mocked Telegram objects."""
import asyncio
import datetime
import io
import logging
import os
import re

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
import permissions


# ----- _apply_changes -----

def _timed_event(start="2026-07-20T16:00:00+08:00", end="2026-07-20T17:00:00+08:00"):
    return {"id": "ev1", "summary": "Piano", "start": {"dateTime": start}, "end": {"dateTime": end}}


def test_apply_absolute_times():
    final = main._apply_changes(_timed_event(), {"start_time": "18:00", "end_time": "19:30"})
    assert final["all_day"] is False
    assert final["start"].hour == 18 and final["end"].hour == 19 and final["end"].minute == 30


def test_apply_start_only_keeps_duration():
    final = main._apply_changes(_timed_event(), {"start_time": "18:00"})
    assert final["end"] - final["start"] == datetime.timedelta(hours=1)


def test_apply_relative_shift_keeps_duration():
    final = main._apply_changes(_timed_event(), {"shift_minutes": 25})
    assert final["start"].minute == 25
    assert final["end"] - final["start"] == datetime.timedelta(hours=1)


def test_apply_date_only_keeps_time_of_day():
    final = main._apply_changes(_timed_event(), {"date": datetime.date(2026, 7, 22)})
    assert final["start"].date() == datetime.date(2026, 7, 22)
    assert final["start"].hour == 16


def test_apply_all_day_event_stays_all_day():
    original = {"id": "ev2", "summary": "Visit", "start": {"date": "2026-07-20"}, "end": {"date": "2026-07-21"}}
    final = main._apply_changes(original, {"date": datetime.date(2026, 7, 22)})
    assert final["all_day"] is True
    assert final["date"] == datetime.date(2026, 7, 22)


def test_apply_title_change_preserves_rest():
    # The renamed activity comes back wearing the event's end time, because
    # titles carry it by design ("Child_2 5pm Piano"). No child prefix here:
    # this fixture runs on the single-calendar test config.
    final = main._apply_changes(_timed_event(), {"title": "Violin"})
    assert final["title"] == "5pm Violin"
    assert final["start"].hour == 16


def test_amend_retitles_around_the_new_end_time():
    """A title stating 5pm on an event that now ends at 7pm would simply be
    a lie - the whole reason _retitle exists."""
    final = main._apply_changes(_timed_event(), {"start_time": "18:00", "end_time": "19:00"})
    assert final["title"] == "7pm Piano"


def test_amend_does_not_stack_time_prefixes():
    """Amending twice must not produce "5pm 6pm Piano" - the old clock
    segment has to be stripped before the new one goes on."""
    once = main._apply_changes(_timed_event(), {"start_time": "18:00"})
    twice = main._apply_changes(
        {**_timed_event(), "summary": once["title"]}, {"start_time": "19:00"}
    )
    assert twice["title"] == "7pm Piano"


def test_amend_lone_time_is_the_new_end_and_keeps_duration():
    """"move piano to 6pm" reads the same way a create does - it now finishes
    at 6 - and an hour-long event stays an hour long."""
    final = main._apply_changes(_timed_event(), {"start_time": "18:00"})
    assert final["end"].hour == 18 and final["end"].minute == 0
    assert final["end"] - final["start"] == datetime.timedelta(hours=1)


# ----- _escape_markdown (2.1) -----

def test_escape_markdown_specials():
    assert main._escape_markdown("a*b_c`d[e") == "a\\*b\\_c\\`d\\[e"


def test_escape_markdown_leaves_normal_text():
    assert main._escape_markdown("Swimming at 4pm - pool #2!") == "Swimming at 4pm - pool #2!"


# ----- _redact_secrets (2.4) -----

def test_redact_replaces_secret_values():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    assert token not in main._redact_secrets(f"error for url /bot{token}/send")


def test_formatter_redacts_tracebacks():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(main.JsonFormatter())
    test_logger = logging.getLogger("redaction_pytest")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False
    try:
        raise RuntimeError(f"bad key: {os.environ['OPENAI_API_KEY']}")
    except RuntimeError:
        test_logger.exception("boom")
    output = stream.getvalue()
    assert os.environ["OPENAI_API_KEY"] not in output
    assert "***OPENAI_API_KEY***" in output


# ----- handler behavior via mocked Telegram objects -----

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])


def _mock_update(user_id, chat_id=1):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    # A real telegram.Update has no edit_message_text (only CallbackQuery
    # does) - MagicMock fabricates any attribute access by default, which
    # would make the hasattr(update_or_query, "edit_message_text") dispatch
    # in _show_create_confirm/_show_amend_confirm silently pick the wrong
    # branch. Delete it so the mock matches real Update semantics.
    del update.edit_message_text
    return update



@pytest.fixture
def six_cal_env(monkeypatch):
    """Categorized calendars, so category_for_key resolves during learning."""
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    for category in ("SCHOOL", "ENRICHMENT", "OTHERS"):
        monkeypatch.setenv(f"DAUGHTER_1_{category}_CALENDAR_ID", f"cal-child_1-{category.lower()}")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    monkeypatch.setenv("DAUGHTER_2_SCHOOL_CALENDAR_ID", "cal-child_2-school")


def test_start_ignores_unknown_ids():
    update = _mock_update(123456789)
    asyncio.run(main.start(update, MagicMock()))
    assert update.message.reply_text.await_count == 0


def test_start_replies_to_known_ids():
    update = _mock_update(ADMIN_ID)
    asyncio.run(main.start(update, MagicMock()))
    assert update.message.reply_text.await_count == 1


def test_callback_rate_limit_blocks_write(monkeypatch):
    deleted = []
    monkeypatch.setattr(main.calendar_tools, "get_event", lambda eid: {"summary": "x"})
    monkeypatch.setattr(main.calendar_tools, "delete_event", lambda eid: deleted.append(eid))
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: main.WRITE_RATE_LIMIT)

    update = _mock_update(ADMIN_ID)
    query = update.callback_query
    query.data = "del:ev123"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert query.edit_message_text.await_args.args[0] == main.RATE_LIMIT_REPLY
    assert deleted == []


def test_callback_under_limit_deletes(monkeypatch):
    deleted = []
    monkeypatch.setattr(main.calendar_tools, "get_event", lambda eid, calendar_key=None: {"summary": "x"})
    monkeypatch.setattr(main.calendar_tools, "delete_event", lambda eid, calendar_key=None: deleted.append(eid))
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)

    update = _mock_update(ADMIN_ID)
    query = update.callback_query
    query.data = "del:|ev123"  # empty calendar_key segment = default/single-calendar mode
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert deleted == ["ev123"]


def test_callback_without_confirm_permission_is_refused(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "reader")
    update = _mock_update(777)
    query = update.callback_query
    query.data = "confirm"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    # refused via the alert on answer(); nothing edited, nothing written
    assert query.answer.await_args.kwargs.get("show_alert") is True
    assert query.edit_message_text.await_count == 0


def _callback_update(user_id, data, chat_id=1):
    update = _mock_update(user_id, chat_id)
    query = update.callback_query
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return update, query


def test_callback_from_unknown_id_is_silently_ignored(monkeypatch):
    """CLAUDE.md: unknown IDs are logged and ignored - never answered. A reply
    (even a refusal) confirms the bot exists to a stranger."""
    cleared = []
    monkeypatch.setattr(main.db, "clear_pending", lambda chat_id: cleared.append(chat_id))

    update, query = _callback_update(123456789, "cancel")
    asyncio.run(main.handle_callback(update, MagicMock()))

    assert query.answer.await_count == 0
    assert query.edit_message_text.await_count == 0
    assert cleared == []  # regression: cancel used to run before any identity check


def test_callback_cancel_still_works_for_known_id(monkeypatch):
    cleared = []
    monkeypatch.setattr(main.db, "clear_pending", lambda chat_id: cleared.append(chat_id))

    update, query = _callback_update(ADMIN_ID, "cancel")
    asyncio.run(main.handle_callback(update, MagicMock()))

    assert cleared == [1]
    assert query.edit_message_text.await_args.args[0] == "Okay, nothing changed."


@pytest.mark.parametrize("raw_index", ["-1", "99", "notanint"])
def test_create_opt_rejects_bad_index(monkeypatch, raw_index):
    """A negative index would otherwise wrap to a real-but-different option -
    confirming a time the card never displayed."""
    payload = {
        "title": "Swimming", "location": None, "notes": None, "calendar_key": None,
        "options": [
            {"start": datetime.datetime(2026, 7, 25, 16, 0), "end": datetime.datetime(2026, 7, 25, 17, 0)},
            {"start": datetime.datetime(2026, 7, 25, 17, 0), "end": datetime.datetime(2026, 7, 25, 18, 0)},
        ],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: payload if kind == "create_options" else None)
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    saved = []
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: saved.append(a))

    update, query = _callback_update(ADMIN_ID, f"create_opt:{raw_index}")
    asyncio.run(main.handle_callback(update, MagicMock()))

    assert query.edit_message_text.await_args.args[0] == main.EXPIRED_REPLY
    assert saved == []  # no confirm card was staged from a bogus index


def test_create_opt_accepts_valid_index(monkeypatch):
    payload = {
        "title": "Swimming", "location": None, "notes": None, "calendar_key": None,
        "options": [
            {"start": datetime.datetime(2026, 7, 25, 16, 0), "end": datetime.datetime(2026, 7, 25, 17, 0)},
            {"start": datetime.datetime(2026, 7, 25, 17, 0), "end": datetime.datetime(2026, 7, 25, 18, 0)},
        ],
    }
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: payload if kind == "create_options" else None)
    monkeypatch.setattr(main.db, "count_recent_writes", lambda tid, window: 0)
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)

    update, query = _callback_update(ADMIN_ID, "create_opt:1")
    asyncio.run(main.handle_callback(update, MagicMock()))

    sent = query.edit_message_text.await_args.args[0]
    assert "17:00-18:00" in sent  # the option actually chosen


def test_auth_error_gets_specific_reply(monkeypatch):
    """An expired/revoked Google token needs admin action, so it must not be
    flattened into the generic 'try again' message."""
    @main.safe_handler
    async def boom(update, context):
        raise main.calendar_tools.AuthRequiredError("token revoked")

    update = _mock_update(ADMIN_ID)
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    asyncio.run(boom(update, context))

    # The admin is the one who can fix it, so he gets the fix itself.
    sent = context.bot.send_message.await_args.args[1]
    assert "python main.py --auth" in sent
    assert "try again" not in sent.lower()


def test_present_create_escapes_malicious_title(monkeypatch):
    monkeypatch.setattr(main.calendar_tools, "find_conflicts", lambda s, e: iter([]))
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)

    update = _mock_update(ADMIN_ID)
    result = {
        "action": "create", "status": "ok", "title": "Buy milk *urgent*",
        "location": None, "notes": None, "all_day": True, "date": datetime.date(2026, 7, 25),
    }
    asyncio.run(main._present_create(update, chat_id=1, result=result))

    sent = update.message.reply_text.await_args.args[0]
    assert "\\*urgent\\*" in sent
    # regression guard: the title must NOT be nested inside our own bold
    # entity - Telegram's legacy Markdown parser doesn't honor \-escapes for
    # characters inside an open entity, so "Add *escaped-title*" silently
    # breaks the escaping (verified live: rendered the asterisks as real
    # bold instead of literal characters). "Add: " (no wrapping asterisk)
    # is the correct, safe shape.
    assert "Add *" not in sent
    assert "Add: " in sent


def test_show_amend_confirm_does_not_nest_title_in_bold(monkeypatch):
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    original = {
        "id": "abc123", "summary": "Old title",
        "start": {"dateTime": "2026-07-20T16:00:00+08:00"}, "end": {"dateTime": "2026-07-20T17:00:00+08:00"},
    }
    update = _mock_update(ADMIN_ID)  # already has edit_message_text removed, matching a real Update
    asyncio.run(main._show_amend_confirm(update, chat_id=1, original=original, changes={"title": "New *sneaky*"}))

    sent = update.message.reply_text.await_args.args[0]
    assert "\\*sneaky\\*" in sent
    assert "Update to: *" not in sent
    assert "Update to: " in sent


def test_audit_verb_for_amend_is_spelled_correctly(monkeypatch):
    entries = [
        {"timestamp": "t", "telegram_id": "1", "role": "admin", "action": "amend", "summary": "Piano", "status": "success"},
        {"timestamp": "t", "telegram_id": "1", "role": "admin", "action": "create", "summary": "Swim", "status": "success"},
        {"timestamp": "t", "telegram_id": "1", "role": "admin", "action": "delete", "summary": "Dentist", "status": "success"},
    ]
    monkeypatch.setattr(main.db, "recent_audit", lambda limit: entries)

    update = _mock_update(ADMIN_ID)
    asyncio.run(main.audit(update, MagicMock()))

    sent = update.message.reply_text.await_args.args[0]
    assert "amended" in sent
    assert "amendd" not in sent
    assert "created" in sent
    assert "deleted" in sent


# ----- LLM rate limit + photo size cap (pre-deploy cost controls) -----

class _Photo:
    def __init__(self, size):
        self.file_size = size


def test_photo_picker_prefers_largest_under_the_cap():
    variants = [_Photo(10_000), _Photo(900_000), _Photo(4_000_000)]
    assert main._pick_photo(variants).file_size == 900_000


def test_photo_picker_falls_back_to_smallest_when_all_are_big():
    variants = [_Photo(2_000_000), _Photo(7_000_000)]
    assert main._pick_photo(variants).file_size == 2_000_000


def test_photo_picker_rejects_beyond_the_hard_limit():
    assert main._pick_photo([_Photo(9_000_000)]) is None


def test_text_handler_stops_at_the_llm_limit(monkeypatch):
    """The write limit guards only confirm taps, so without this a parse loop
    would bill unmetered."""
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: main.LLM_RATE_LIMIT)
    called = {"interpret": False}
    monkeypatch.setattr(main, "interpret", lambda text: called.update(interpret=True))

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    update.message.text = "what's on tomorrow?"
    update.message.reply_text = AsyncMock()

    asyncio.run(main.handle_text(update, MagicMock()))

    assert called["interpret"] is False
    assert main.BUSY_REPLY in update.message.reply_text.await_args.args[0]


def test_text_handler_meters_before_calling_the_model(monkeypatch):
    """Logged before the call, not after - otherwise a burst of concurrent
    requests all pass the check before any of them is counted."""
    order = []
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: 0)
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: 0)
    monkeypatch.setattr(main.db, "log_usage", lambda tid, kind: order.append("logged") or 1)
    monkeypatch.setattr(main.db, "record_usage_tokens", lambda *a: None)
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: None)
    monkeypatch.setattr(main, "interpret", lambda text: order.append("model") or {"action": "unknown"})
    monkeypatch.setattr(main, "_dispatch", AsyncMock())

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 123
    update.message.text = "hi"
    update.message.reply_text = AsyncMock()

    asyncio.run(main.handle_text(update, MagicMock()))

    assert order == ["logged", "model"]


# ----- multi-date confirm card -----

def _occ(day, hour=15):
    start = datetime.datetime(2026, 8, day, hour, 45, tzinfo=main.calendar_tools.TIMEZONE)
    return {"all_day": False, "start": start,
            "end": start + datetime.timedelta(minutes=15), "date": None}


def test_multi_date_card_lists_every_date():
    """A ✅ must mean "yes, to the thing I am looking at" - so the card has to
    show all of what it commits, not a summary."""
    result = {"title": "Child_1 4pm End school", "calendar_key": None,
              "occurrences": [_occ(17), _occ(24), _occ(31)], **_occ(17)}
    update = _mock_update(ADMIN_ID)
    with patch.object(main.db, "save_pending"):
        asyncio.run(main._show_create_confirm(update, 1, result))

    text = update.message.reply_text.await_args.args[0]
    for day in ("17 Aug", "24 Aug", "31 Aug"):
        assert day in text
    assert "Add all 3?" in text


def test_multi_date_card_flags_clashing_dates_only():
    result = {"title": "Child_1 4pm End school", "calendar_key": None,
              "occurrences": [_occ(17), _occ(24), _occ(31)], **_occ(17)}
    clashes = {1: [{"summary": "Swimming"}]}
    update = _mock_update(ADMIN_ID)
    with patch.object(main.db, "save_pending"):
        asyncio.run(main._show_create_confirm(update, 1, result, clashes))

    lines = update.message.reply_text.await_args.args[0].splitlines()
    flagged = [line for line in lines if "⚠️" in line]
    assert len(flagged) == 1
    assert "24 Aug" in flagged[0] and "Swimming" in flagged[0]


def test_single_date_card_is_unchanged():
    """The multi-date branch must not alter the everyday single-event card."""
    result = {"title": "Child_2 4pm Piano", "calendar_key": None,
              "occurrences": [_occ(17)], **_occ(17)}
    update = _mock_update(ADMIN_ID)
    with patch.object(main.db, "save_pending"):
        asyncio.run(main._show_create_confirm(update, 1, result))

    text = update.message.reply_text.await_args.args[0]
    assert text.startswith("Add: Child\\_2 4pm Piano —")
    assert "Add all" not in text


def test_multi_create_reports_partial_failure_honestly():
    """One network blip must not silently lose the other dates, nor be
    reported as success."""
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return {"htmlLink": "http://example/e"}

    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = 1
    pending = {"title": "Child_1 4pm End school", "calendar_key": None,
               "location": None, "notes": None}
    occurrences = [_occ(17), _occ(24), _occ(31)]

    with patch.object(main.calendar_tools, "create_event", side_effect=flaky), \
         patch.object(main.db, "log_audit"):
        asyncio.run(main._create_many(query, 1, "admin", pending, occurrences, {}))

    text = query.edit_message_text.await_args.args[0]
    assert "Added 2 of 3" in text
    assert "24 Aug" in text   # names the one that failed


def test_multi_create_audits_every_occurrence():
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = 1
    logged = []

    with patch.object(main.calendar_tools, "create_event", return_value={"htmlLink": "x"}), \
         patch.object(main.db, "log_audit", side_effect=lambda *a, **k: logged.append(a)):
        asyncio.run(main._create_many(
            query, 1, "admin",
            {"title": "T", "calendar_key": None, "location": None, "notes": None},
            [_occ(17), _occ(24), _occ(31)], {},
        ))

    # The trail reflects all three events, but only the first is a rate-limited
    # "create" - the batch is one reviewed decision, not three (§ the write
    # rate limit that a term of weekly classes used to trip).
    assert [a[2] for a in logged] == ["create", "create_series", "create_series"]


# ----- /usage and the budget cap -----

def test_usage_ignores_non_admin(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "editor")
    update = _mock_update(777)
    asyncio.run(main.usage(update, MagicMock()))
    assert update.message.reply_text.await_count == 0


def test_usage_reports_tokens_and_labels_dollars_as_estimates(monkeypatch):
    monkeypatch.setattr(main.db, "usage_since",
                        lambda since: {"calls": 3, "prompt_tokens": 1000, "completion_tokens": 200,
                                       "total_tokens": 1200, "by_kind": {"text": 2, "image": 1}})
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: 1200)
    update = _mock_update(ADMIN_ID)
    asyncio.run(main.usage(update, MagicMock()))

    text = update.message.reply_text.await_args.args[0]
    assert "1,200 tokens" in text
    assert "1 image, 2 text" in text  # sorted by kind, so the output is stable
    assert "estimates" in text  # never presents a hardcoded price as fact


def test_budget_blocks_a_non_admin(monkeypatch):
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: 0)
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: main.MONTHLY_TOKEN_BUDGET)
    monkeypatch.setattr(main, "_warn_admin_once_daily", AsyncMock())
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "editor")

    update = _mock_update(777)
    allowed, row = asyncio.run(main._begin_llm_call(update, 777, "text"))

    assert allowed is False and row is None
    assert main.BUDGET_EXCEEDED_REPLY in update.message.reply_text.await_args.args[0]


def test_budget_never_blocks_the_admin(monkeypatch):
    """/usage and raising the cap have to stay reachable from a phone."""
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: 0)
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: main.MONTHLY_TOKEN_BUDGET * 10)
    monkeypatch.setattr(main.db, "log_usage", lambda tid, kind: 42)
    monkeypatch.setattr(main, "_warn_admin_once_daily", AsyncMock())

    update = _mock_update(ADMIN_ID)
    allowed, row = asyncio.run(main._begin_llm_call(update, ADMIN_ID, "text"))

    assert allowed is True and row == 42


def test_budget_warning_fires_once_a_day_not_once_a_message(monkeypatch):
    sent = []
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: 0)
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: int(main.MONTHLY_TOKEN_BUDGET * 0.9))
    monkeypatch.setattr(main.db, "log_usage", lambda tid, kind: 1)
    monkeypatch.setattr(main, "_budget_warned_on", None)

    update = _mock_update(ADMIN_ID)
    update.get_bot.return_value.send_message = AsyncMock(side_effect=lambda **k: sent.append(k))

    for _ in range(3):
        asyncio.run(main._begin_llm_call(update, ADMIN_ID, "text"))

    assert len(sent) == 1


def test_a_failed_warning_does_not_take_down_the_message(monkeypatch):
    """The warning is a nicety; the user's actual request is not."""
    monkeypatch.setattr(main.db, "count_recent_usage", lambda tid, w: 0)
    monkeypatch.setattr(main.db, "tokens_this_month", lambda: int(main.MONTHLY_TOKEN_BUDGET * 0.9))
    monkeypatch.setattr(main.db, "log_usage", lambda tid, kind: 7)
    monkeypatch.setattr(main, "_budget_warned_on", None)

    update = _mock_update(ADMIN_ID)
    update.get_bot.return_value.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))

    allowed, row = asyncio.run(main._begin_llm_call(update, ADMIN_ID, "text"))
    assert allowed is True and row == 7


# ----- /help lists the commands you can actually use -----

def test_help_lists_commands_for_the_admin():
    text = main.help_text_for(ADMIN_ID)
    for name, _, _ in main.COMMANDS:
        assert f"/{name}" in text


def test_help_hides_admin_commands_from_others(monkeypatch):
    """Listing a command that will silently ignore you is worse than not
    listing it."""
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "editor")
    text = main.help_text_for(777)
    assert "/upcoming" in text
    assert "/usage" not in text
    assert "/audit" not in text


def test_every_registered_command_is_documented():
    """A command added to the app but missing from COMMANDS would never show
    up in /help or the "/" menu."""
    import inspect
    source = inspect.getsource(main.main)
    registered = set(re.findall(r'CommandHandler\("(\w+)"', source))
    documented = {name for name, _, _ in main.COMMANDS}
    # /start is the same handler as /help and deliberately undocumented.
    assert registered - documented == {"start"}


# ----- learning from confirmed writes -----

def _pending_school_event():
    return {"kind": "create", "title": "Child_1 9am Gate 5 Assembly",
            "calendar_key": "child_1_school", "location": None, "notes": None}


def test_learns_category_and_gate_from_a_confirmed_write(six_cal_env, monkeypatch):
    recorded = []
    monkeypatch.setattr(main.db, "record_hint",
                        lambda tid, kind, phrase, value: recorded.append((kind, phrase, value)))

    asyncio.run(main._learn_from(ADMIN_ID, "admin", _pending_school_event()))

    assert ("category", "assembly", "school") in recorded
    assert ("gate", "assembly", "5") in recorded


def test_readers_never_teach_the_bot(six_cal_env, monkeypatch):
    """Learned phrases feed the model prompt, so learning is a path from
    semi-trusted content into the model's instructions. It must not be
    reachable by a read-only account."""
    recorded = []
    monkeypatch.setattr(main.db, "record_hint", lambda *a: recorded.append(a))

    asyncio.run(main._learn_from(999, "reader", _pending_school_event()))

    assert recorded == []


def test_generic_words_are_not_learned(six_cal_env, monkeypatch):
    """"appointment" appears across every category - learning it would make
    the bot confidently wrong."""
    recorded = []
    monkeypatch.setattr(main.db, "record_hint",
                        lambda tid, kind, phrase, value: recorded.append(phrase))

    pending = {**_pending_school_event(), "title": "Child_1 9am Gate 5 The appointment"}
    asyncio.run(main._learn_from(ADMIN_ID, "admin", pending))

    assert "appointment" not in recorded
    assert "the" not in recorded


def test_learning_failure_never_surfaces_to_the_user(six_cal_env, monkeypatch):
    """The write already succeeded and the user was told; a bookkeeping
    failure must not turn into a visible error."""
    def explode(*a):
        raise RuntimeError("db gone")

    monkeypatch.setattr(main.db, "record_hint", explode)
    asyncio.run(main._learn_from(ADMIN_ID, "admin", _pending_school_event()))  # must not raise


def test_uncategorized_setup_learns_nothing(monkeypatch):
    monkeypatch.setattr(main.calendar_tools, "category_for_key", lambda key: None)
    recorded = []
    monkeypatch.setattr(main.db, "record_hint", lambda *a: recorded.append(a))
    asyncio.run(main._learn_from(ADMIN_ID, "admin", _pending_school_event()))
    assert recorded == []


# ----- /hints and /digest -----

def test_hints_reset_clears(monkeypatch):
    monkeypatch.setattr(main.db, "clear_hints", lambda: 7)
    update = _mock_update(ADMIN_ID)
    context = MagicMock()
    context.args = ["reset"]
    asyncio.run(main.hints(update, context))
    assert "7" in update.message.reply_text.await_args.args[0]


def test_hints_ignores_non_admin(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "editor")
    update = _mock_update(777)
    context = MagicMock()
    context.args = []
    asyncio.run(main.hints(update, context))
    assert update.message.reply_text.await_count == 0


def test_digest_reports_friction(monkeypatch):
    monkeypatch.setattr(main.db, "friction_since",
                        lambda since, limit=15: [("clarify", "child_1 thing tuesday", 3)])
    update = _mock_update(ADMIN_ID)
    asyncio.run(main.digest(update, MagicMock()))
    text = update.message.reply_text.await_args.args[0]
    assert "child\\_1 thing tuesday" in text
    assert "3×" in text


def test_digest_ignores_non_admin(monkeypatch):
    monkeypatch.setitem(permissions.PERMISSIONS, "777", "reader")
    update = _mock_update(777)
    asyncio.run(main.digest(update, MagicMock()))
    assert update.message.reply_text.await_count == 0


# ----- images sent as documents (the silently-ignored screenshot bug) -----

def test_image_sent_as_a_document_is_accepted():
    """Phones often default to "send as file" for screenshots, which arrives
    as a document rather than a photo. Handling only photos meant those
    messages matched no handler and were dropped without a word."""
    message = MagicMock()
    message.photo = []
    message.document.mime_type = "image/jpeg"
    message.document.file_size = 700_000
    assert main._pick_image_source(message) is message.document


def test_non_image_document_is_ignored():
    message = MagicMock()
    message.photo = []
    message.document.mime_type = "application/pdf"
    message.document.file_size = 1000
    assert main._pick_image_source(message) is None


def test_oversized_document_is_rejected():
    message = MagicMock()
    message.photo = []
    message.document.mime_type = "image/png"
    message.document.file_size = main.HARD_PHOTO_LIMIT_BYTES + 1
    assert main._pick_image_source(message) is None


def test_photo_still_preferred_when_both_present():
    small = MagicMock(file_size=100_000)
    message = MagicMock()
    message.photo = [small]
    assert main._pick_image_source(message) is small


# ----- button wording: the decline button must name the outcome -----

def test_decline_button_names_the_action_not_just_cancel():
    """On a delete card a bare "Cancel" reads as "yes, cancel the
    appointment" when it means "abandon this request"."""
    delete_label = main._DECLINE_LABEL["delete"]
    assert "delete" in delete_label.lower()
    assert main._DECLINE_LABEL["create"] != main._DECLINE_LABEL["delete"]


def test_confirm_keyboard_offers_edit():
    rows = main._confirm_keyboard("create").inline_keyboard
    data = [b.callback_data for row in rows for b in row]
    assert data == ["confirm", "edit", "cancel"]


def test_confirm_keyboard_decline_label_matches_kind():
    for kind in ("create", "amend", "delete"):
        rows = main._confirm_keyboard(kind).inline_keyboard
        labels = [b.text for row in rows for b in row]
        assert main._DECLINE_LABEL[kind] in labels


# ----- "Change something" re-parses instead of discarding -----

def test_edit_restores_the_original_request_for_reparsing(monkeypatch):
    saved = {}
    monkeypatch.setattr(main.db, "pop_pending",
                        lambda chat_id, kind: {"kind": "create", "source_text": "Child_2 piano Tuesday 4pm",
                                               "title": "Child_2 4pm Piano"} if kind == "confirm" else None)
    monkeypatch.setattr(main.db, "save_pending",
                        lambda chat_id, kind, payload: saved.update({kind: payload}))
    monkeypatch.setattr(main.db, "log_friction", lambda *a: None)

    update = _mock_update(ADMIN_ID)
    query = update.callback_query
    query.data = "edit"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert saved["clarification"]["text"] == "Child_2 piano Tuesday 4pm"
    assert "change" in query.edit_message_text.await_args.args[0].lower()


def test_edit_falls_back_to_describing_the_proposal(monkeypatch):
    """A screenshot sent without a caption has no original wording to reuse."""
    saved = {}
    pending = {"kind": "create", "source_text": "", "title": "Child_1 9am Assembly",
               "all_day": False,
               "start": datetime.datetime(2026, 9, 3, 10, 15, tzinfo=main.calendar_tools.TIMEZONE)}
    monkeypatch.setattr(main.db, "pop_pending",
                        lambda chat_id, kind: pending if kind == "confirm" else None)
    monkeypatch.setattr(main.db, "save_pending",
                        lambda chat_id, kind, payload: saved.update({kind: payload}))
    monkeypatch.setattr(main.db, "log_friction", lambda *a: None)

    update = _mock_update(ADMIN_ID)
    query = update.callback_query
    query.data = "edit"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert "Child_1 9am Assembly" in saved["clarification"]["text"]
    assert "03 Sep 2026" in saved["clarification"]["text"]


def test_edit_on_an_expired_card_says_so(monkeypatch):
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: None)
    update = _mock_update(ADMIN_ID)
    query = update.callback_query
    query.data = "edit"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    asyncio.run(main.handle_callback(update, MagicMock()))
    assert query.edit_message_text.await_args.args[0] == main.EXPIRED_REPLY


# ----- /upcoming [child] [category] -----

def test_upcoming_without_args_covers_everything(six_cal_env):
    """Narrowing is opt-in - the bare command must keep behaving as before."""
    keys, label, error = main._resolve_scope([])
    assert (keys, label, error) == (None, None, None)


def test_upcoming_narrows_to_one_child(six_cal_env):
    keys, label, error = main._resolve_scope(["child_1"])
    assert error is None
    assert label == "Child_1"
    assert sorted(keys) == ["child_1_enrichment", "child_1_others", "child_1_school"]


def test_upcoming_child_lookup_is_case_insensitive(six_cal_env):
    assert main._resolve_scope(["CHILD_1"])[0] == main._resolve_scope(["child_1"])[0]


def test_upcoming_narrows_to_one_category(six_cal_env):
    keys, label, error = main._resolve_scope(["child_1", "school"])
    assert error is None
    assert keys == ["child_1_school"]
    assert label == "Child_1 — school"


def test_upcoming_unknown_name_lists_the_real_ones(six_cal_env):
    keys, label, error = main._resolve_scope(["bob"])
    assert keys is None
    assert "Child_1" in error and "Child_2" in error


def test_upcoming_unknown_category_lists_the_real_ones(six_cal_env):
    keys, label, error = main._resolve_scope(["child_1", "swimming"])
    assert keys is None
    assert "school" in error and "enrichment" in error


def test_upcoming_passes_the_narrowed_keys_through(six_cal_env, monkeypatch):
    captured = {}

    def fake_all(n_per_calendar=10, keys=None):
        captured["keys"] = keys
        return []

    monkeypatch.setattr(main.calendar_tools, "list_upcoming_events_all", fake_all)
    update = _mock_update(ADMIN_ID)
    context = MagicMock()
    context.args = ["child_2", "school"]

    asyncio.run(main.upcoming(update, context))

    assert captured["keys"] == ["child_2_school"]
    assert "Child_2" in update.message.reply_text.await_args.args[0]
