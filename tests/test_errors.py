"""Every failure says something true (errors.py).

Built from REAL exception objects, constructed the way the libraries raise
them, rather than from mocks - the classification keys off attributes like
`code`, `status_code` and `resp.status`, and a mock would happily provide
whichever one the code happened to read.

2026-09-25: the OpenAI account ran out of credit. Every message failed, the
family was told "please try again", and nothing said why.
"""
import asyncio
import json
import os
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import httplib2
import httpx
import openai
import pytest
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from telegram import error as tg_error

import calendar_tools
import errors
import event_parser
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
EDITOR_ID = 222333444

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_CAL = "https://www.googleapis.com/calendar/v3/calendars/cal-child_1-school"


def _openai(cls, status, body=None, message="error"):
    response = httpx.Response(status, request=httpx.Request("POST", _OPENAI_URL))
    return cls(message, response=response, body=body)


def _google(status, uri=_CAL + "/events", reason=None):
    content = json.dumps({"error": {"code": status, "message": "x",
                                    "errors": [{"reason": reason or "x"}]}}).encode()
    return HttpError(httplib2.Response({"status": status}), content, uri=uri)


# ----- the failure that started this -----

def test_an_empty_openai_account_is_named_not_retried():
    """The exact error from 2026-09-25, body and all."""
    exc = _openai(openai.RateLimitError, 429, body={
        "message": "You have no credits remaining. Add credits to continue using the API",
        "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted",
    }, message="Error code: 429 - insufficient_quota")
    d = errors.diagnose(exc)
    assert d.kind == "ai_no_credit"
    assert "run out of credit" in d.reply
    assert "won't help" in d.reply
    assert "Billing" in d.admin_reply
    assert d.alert


def test_an_ordinary_rate_limit_is_told_to_wait_not_to_top_up():
    """Same status code, opposite advice - this one clears itself."""
    exc = _openai(openai.RateLimitError, 429, body={"type": "requests", "code": "rate_limit_exceeded"})
    d = errors.diagnose(exc)
    assert d.kind == "ai_rate_limited"
    assert "wait a minute" in d.reply
    assert not d.alert


@pytest.mark.parametrize("cls,status,kind,alert", [
    (openai.AuthenticationError, 401, "ai_key_rejected", True),
    (openai.PermissionDeniedError, 403, "ai_key_rejected", True),
    (openai.NotFoundError, 404, "ai_model_missing", True),
    (openai.BadRequestError, 400, "ai_rejected_request", True),
    (openai.InternalServerError, 500, "ai_down", False),
])
def test_openai_status_errors(cls, status, kind, alert):
    d = errors.diagnose(_openai(cls, status))
    assert d.kind == kind
    assert d.alert is alert


def test_an_unreachable_ai_service_is_a_blip():
    req = httpx.Request("POST", _OPENAI_URL)
    for exc in (openai.APIConnectionError(request=req), openai.APITimeoutError(request=req)):
        d = errors.diagnose(exc)
        assert d.kind == "ai_unreachable"
        assert "try again in a minute" in d.reply
        assert not d.alert


def test_a_garbled_model_answer_is_worth_retrying():
    d = errors.diagnose(event_parser.ParseError("Model returned unparseable arguments"))
    assert d.kind == "ai_garbled"
    assert "send it again" in d.reply


# ----- Google -----

def test_google_auth_failures_all_point_at_reauthorising():
    for exc in (calendar_tools.AuthRequiredError("revoked"), RefreshError("invalid_grant"),
                _google(401)):
        d = errors.diagnose(exc)
        assert d.kind == "google_auth"
        assert "python main.py --auth" in d.admin_reply
        assert d.sure_not_saved and d.alert


def test_gmail_is_named_as_gmail():
    d = errors.diagnose(_google(401, uri="https://gmail.googleapis.com/gmail/v1/users/me/messages/send"))
    assert "Gmail" in d.reply


def test_a_google_403_is_a_permission_problem_unless_it_says_slow_down():
    assert errors.diagnose(_google(403, reason="forbidden")).kind == "google_forbidden"
    for reason in ("rateLimitExceeded", "userRateLimitExceeded"):
        d = errors.diagnose(_google(403, reason=reason))
        assert d.kind == "google_rate_limited"
        assert not d.alert
    assert errors.diagnose(_google(429)).kind == "google_rate_limited"


def test_a_missing_event_is_not_a_missing_calendar():
    """Tapping delete on an event somebody already removed in Google Calendar
    is everyday, not Parent_1's problem. A calendar ID that 404s breaks everything
    and is."""
    gone = errors.diagnose(_google(404, uri=_CAL + "/events/abc123"))
    assert gone.kind == "google_event_gone"
    assert not gone.alert
    assert "/upcoming" in gone.reply

    missing = errors.diagnose(_google(404, uri=_CAL + "/events?timeMin=x"))
    assert missing.kind == "google_calendar_missing"
    assert missing.alert
    assert "_CALENDAR_ID" in missing.admin_reply


def test_google_5xx_is_their_problem():
    d = errors.diagnose(_google(503))
    assert d.kind == "google_down"
    assert not d.sure_not_saved   # a 5xx can come back after the write landed


# ----- the machine -----

def test_a_full_disk_and_a_locked_database_are_told_apart():
    full = errors.diagnose(sqlite3.OperationalError("database or disk is full"))
    assert full.kind == "storage_full" and full.alert
    assert "journalctl --vacuum" in full.admin_reply
    busy = errors.diagnose(sqlite3.OperationalError("database is locked"))
    assert busy.kind == "storage_busy" and not busy.alert


def test_network_errors_are_blips():
    for exc in (TimeoutError(), ConnectionResetError(), httplib2.ServerNotFoundError("x")):
        assert errors.diagnose(exc).kind == "network"


def test_a_markdown_failure_is_the_bots_own_bug():
    d = errors.diagnose(tg_error.BadRequest("Can't parse entities: can't find end of the entity"))
    assert d.kind == "formatting_bug"
    assert d.alert and d.needs_ref


def test_anything_else_is_admitted_with_a_reference():
    d = errors.diagnose(KeyError("summary"))
    assert d.kind == "unknown"
    assert d.needs_ref and d.alert
    assert "KeyError" in d.admin_reply
    assert errors.with_ref(d.reply, d, "K3F9Q", for_admin=False).endswith("Ref: K3F9Q")
    assert "grep K3F9Q" in errors.with_ref(d.for_admin(), d, "K3F9Q", for_admin=True)


def test_an_explicitly_wrapped_error_is_still_recognised():
    try:
        try:
            raise TimeoutError()
        except TimeoutError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert errors.diagnose(outer).kind == "network"


def test_an_error_raised_while_handling_another_is_not_blamed_on_it():
    """A KeyError thrown inside an `except HttpError:` block is a KeyError.
    Following __context__ would send Parent_1 to check Google for a bug in the bot."""
    try:
        try:
            raise _google(503)
        except HttpError:
            raise KeyError("oops")
    except KeyError as exc:
        assert errors.diagnose(exc).kind == "unknown"


def test_refs_are_readable():
    ref = errors.new_ref()
    assert len(ref) == 5
    assert not set(ref) & set("0O1IL")


# ----- the wording of a failed write -----

def test_a_write_says_didnt_only_when_the_error_proves_it():
    """A timeout can come back after Google made the change. Saying "didn't
    save" then invites a retry, and a retry makes a duplicate."""
    proven = errors.diagnose(_google(401))
    unsure = errors.diagnose(TimeoutError())
    assert errors.failure_reply("That didn't save.", "That may not have saved.", proven).startswith("That didn't save.")
    assert errors.failure_reply("That didn't save.", "That may not have saved.", unsure).startswith("That may not have saved.")


# ----- wired into the bot -----

def _update(user_id):
    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = user_id
    return update


def _no_credit():
    return _openai(openai.RateLimitError, 429,
                   body={"type": "insufficient_quota", "code": "credit_balance_exhausted"})


@pytest.fixture
def roles(monkeypatch):
    table = {ADMIN_ID: "admin", EDITOR_ID: "editor"}
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: table.get(tid))
    monkeypatch.setattr(main, "_last_admin_alert", {})


def _run(user_id, exc):
    @main.safe_handler
    async def boom(update, context):
        raise exc

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    asyncio.run(boom(_update(user_id), context))
    return context.bot.send_message.await_args_list


def test_the_family_hears_plain_words_and_mark_is_alerted(roles):
    calls = _run(EDITOR_ID, _no_credit())
    replies = [c.args[1] for c in calls if c.args]
    alerts = [c.kwargs["text"] for c in calls if "text" in c.kwargs]

    assert len(replies) == 1 and "run out of credit" in replies[0]
    assert "insufficient_quota" not in replies[0]      # no jargon for the family
    assert len(alerts) == 1
    assert "the editor" in alerts[0] and "Billing" in alerts[0]
    alert_call = next(c for c in calls if "text" in c.kwargs)
    assert alert_call.kwargs["chat_id"] == ADMIN_ID


def test_mark_gets_the_detail_directly_and_no_second_alert(roles):
    calls = _run(ADMIN_ID, _no_credit())
    assert len(calls) == 1
    assert "Billing" in calls[0].args[1]


def test_one_outage_is_one_alert_not_one_per_message(roles):
    """Five tries at a message during an outage must not become five alerts."""
    @main.safe_handler
    async def boom(update, context):
        raise _no_credit()

    context = MagicMock()
    context.bot.send_message = AsyncMock()
    for _ in range(5):
        asyncio.run(boom(_update(EDITOR_ID), context))

    alerts = [c for c in context.bot.send_message.await_args_list if "text" in c.kwargs]
    assert len(alerts) == 1


def test_a_blip_does_not_alert_mark(roles):
    calls = _run(EDITOR_ID, TimeoutError())
    assert len(calls) == 1
    assert "try again in a minute" in calls[0].args[1]


def test_a_failed_alert_never_hides_the_reply(roles):
    @main.safe_handler
    async def boom(update, context):
        raise _no_credit()

    context = MagicMock()

    async def send(*args, **kwargs):
        if "text" in kwargs:
            raise tg_error.NetworkError("down")

    context.bot.send_message = AsyncMock(side_effect=send)
    asyncio.run(boom(_update(EDITOR_ID), context))
    assert any(c.args and "run out of credit" in c.args[1]
               for c in context.bot.send_message.await_args_list)
