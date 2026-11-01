"""Turn an exception into something a person can act on.

"Something went wrong on my end - please try again" was the reply to every
failure, and for most of them it was wrong advice. On 2026-09-25 the OpenAI
account ran out of credit: every message failed, the bot told the family to
try again, and nothing anywhere said why. Retrying could not have worked.

So a failure is diagnosed rather than apologised for. Each diagnosis carries:

- `reply`: what a family member is told. Plain words, no jargon, and it says
  whether retrying will help.
- `admin_reply`: what Parent_1 is told when it happens to him - the same thing
  plus the technical detail and the fix, since he is the one who can act.
- `alert`: whether Parent_1 should hear about it when it happens to SOMEONE ELSE.
  Only for things that need him (an empty account, a revoked token, a full
  disk) - not for a network blip that fixes itself.
- `sure_not_saved`: for the write paths. Some failures prove a write never
  happened (Google refused the credentials before the request); others
  (a timeout) leave it genuinely unknown, and saying "that didn't save" when
  it might have would invite a duplicate.

Nothing here decides what happens next - it only describes. Kept out of
main.py so it can be tested without the bot around it.
"""
import json
import secrets
import socket
import sqlite3
import ssl
from dataclasses import dataclass

import httplib2
import openai
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from telegram import error as tg_error

import calendar_tools
import event_parser

# Who the family is told will handle it. Matches the existing
# BUDGET_EXCEEDED_REPLY, which already names him.
ADMIN_NAME = "Parent_1"

# OpenAI signals an empty prepaid balance as a 429, the same status as an
# ordinary rate limit - but one clears itself in a minute and the other never
# does. Told apart by these codes; both have been seen in the wild.
_NO_CREDIT_CODES = {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}

# Google reports "slow down" as a 403 as well as a 429, distinguished only by
# the reason string in the body.
_GOOGLE_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
                        "dailyLimitExceeded"}


@dataclass(frozen=True)
class Diagnosis:
    kind: str
    reply: str
    admin_reply: str = ""
    alert: bool = False
    sure_not_saved: bool = False
    # Unrecognised failures get a reference so a report from the family can be
    # matched to the traceback in the log.
    needs_ref: bool = False

    def for_admin(self) -> str:
        return self.admin_reply or self.reply


def new_ref() -> str:
    """Short, unambiguous, easy to read out: no 0/O or 1/I/L."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(5))


def diagnose(exc) -> Diagnosis:
    """The most specific diagnosis available for `exc`.

    Follows `raise ... from` (__cause__) so a wrapped error is still
    recognised, but NOT the implicit __context__: a KeyError raised while an
    HttpError was being handled is a KeyError, and blaming Google for it would
    send Parent_1 to the wrong place.
    """
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        found = _diagnose_one(current)
        if found is not None:
            return found
        current = current.__cause__
    return _unknown(exc)


def _diagnose_one(exc):
    for check in (_ai, _parse, _google, _network, _storage, _telegram):
        found = check(exc)
        if found is not None:
            return found
    return None


# ----- the AI service -----

def _ai(exc):
    if not isinstance(exc, openai.OpenAIError):
        return None

    if isinstance(exc, openai.RateLimitError):
        codes = {str(getattr(exc, "code", "") or ""), str(getattr(exc, "type", "") or "")}
        if codes & _NO_CREDIT_CODES or any(c in str(exc) for c in _NO_CREDIT_CODES):
            return Diagnosis(
                "ai_no_credit",
                "I can't read messages right now — the AI account I use has run out of "
                f"credit, so trying again won't help yet. {ADMIN_NAME} has been told.",
                "I can't read any messages: the OpenAI account has run out of credit "
                "(insufficient_quota). Top it up at platform.openai.com → Settings → "
                "Billing. No restart needed — the next message will work.",
                alert=True,
            )
        return Diagnosis(
            "ai_rate_limited",
            "My AI service is asking me to slow down — please wait a minute and try again.",
        )

    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return Diagnosis(
            "ai_key_rejected",
            "I can't read messages right now — my AI service is rejecting my key, so "
            f"trying again won't help. {ADMIN_NAME} has been told.",
            f"OpenAI rejected OPENAI_API_KEY ({_status(exc)}). Check the key in the VM's "
            ".env, then `sudo systemctl restart familybot`.",
            alert=True,
        )

    if isinstance(exc, openai.NotFoundError):
        return Diagnosis(
            "ai_model_missing",
            f"I can't read messages right now — my AI service is misconfigured. {ADMIN_NAME} "
            "has been told.",
            f"OpenAI says the model '{event_parser.MODEL}' doesn't exist or this key can't "
            "use it. Check OPENAI_MODEL in .env, then restart familybot.",
            alert=True,
        )

    if isinstance(exc, openai.BadRequestError):
        # Usually one odd input (a huge or unreadable image). If it is the
        # schema, it will fail on everything - which is why Parent_1 hears of it.
        return Diagnosis(
            "ai_rejected_request",
            "My AI service couldn't process that one. If it was a screenshot, try a "
            "clearer or smaller one — or type the details out instead.",
            f"OpenAI refused the request (400): {_short(exc)}. If every message does "
            "this, the tool schema or prompt is invalid.",
            alert=True,
        )

    # Timeout first: APITimeoutError is a subclass of APIConnectionError.
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return Diagnosis(
            "ai_unreachable",
            "I couldn't reach my AI service just now — probably a network blip. Please "
            "try again in a minute.",
        )

    if isinstance(exc, openai.InternalServerError) or _status(exc, 0) >= 500:
        return Diagnosis(
            "ai_down",
            "My AI service is having problems on its end — please try again in a few "
            "minutes.",
        )

    return None


def _parse(exc):
    if isinstance(exc, event_parser.ParseError):
        return Diagnosis(
            "ai_garbled",
            "I got a garbled answer back while reading that — please send it again, or "
            "say it a little differently.",
            f"The model returned something unusable: {_short(exc)}. One-off is noise; "
            "repeated means the schema or prompt needs a look.",
        )
    return None


# ----- Google (Calendar and Gmail) -----

def _google(exc):
    if isinstance(exc, (calendar_tools.AuthRequiredError, RefreshError)):
        return _google_auth("Google Calendar")
    if not isinstance(exc, HttpError):
        return None

    status = _status(exc, 0)
    service = "Gmail" if "gmail" in (getattr(exc, "uri", "") or "") else "Google Calendar"
    reason = _google_reason(exc)

    if status == 401:
        return _google_auth(service)

    if status == 429 or (status == 403 and reason in _GOOGLE_RATE_REASONS):
        return Diagnosis(
            "google_rate_limited",
            f"{service} is asking me to slow down — please wait a minute and try again.",
            sure_not_saved=True,
        )

    if status == 403:
        return Diagnosis(
            "google_forbidden",
            f"{service} refused me access. {ADMIN_NAME} has been told.",
            f"{service} returned 403 Forbidden ({reason or 'no reason given'}). Usually a "
            "calendar's sharing changed — check it is still shared with the bot's "
            "Google account, with permission to make changes.",
            alert=True, sure_not_saved=True,
        )

    if status in (404, 410):
        if _is_event_url(exc):
            # Most often someone deleted or moved it in Google Calendar between
            # the card being shown and the button being tapped. Not Parent_1's job.
            return Diagnosis(
                "google_event_gone",
                "I couldn't find that event any more — it may have been deleted or moved "
                "already. /upcoming shows what's there now.",
                sure_not_saved=True,
            )
        return Diagnosis(
            "google_calendar_missing",
            f"One of the calendars I'm set up to use can't be found. {ADMIN_NAME} has been "
            "told.",
            f"{service} returned {status} for a calendar, not an event: a *_CALENDAR_ID in "
            "the VM's .env is wrong, or the calendar was deleted or unshared.",
            alert=True, sure_not_saved=True,
        )

    if status >= 500:
        return Diagnosis(
            "google_down",
            f"{service} is having problems on its end — please try again in a few minutes.",
        )

    return None


def _google_auth(service):
    return Diagnosis(
        "google_auth",
        f"I've lost access to {service} and can't fix it myself. {ADMIN_NAME} has been told.",
        f"{service} access has expired or been revoked. On the laptop run "
        "`python main.py --auth`, then copy the new token.json to the VM and "
        "`sudo systemctl restart familybot`.",
        alert=True, sure_not_saved=True,
    )


def _google_reason(exc) -> str:
    try:
        body = json.loads(exc.content.decode("utf-8") if isinstance(exc.content, bytes)
                          else exc.content)
        return body["error"]["errors"][0]["reason"]
    except Exception:
        return ""


def _is_event_url(exc) -> bool:
    uri = (getattr(exc, "uri", "") or "").split("?", 1)[0].rstrip("/")
    # .../calendars/<id>/events/<eventId>  vs  .../calendars/<id>/events
    return "/events/" in uri


# ----- the machine itself -----

def _network(exc):
    network = (TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError,
               httplib2.error.ServerNotFoundError)
    if isinstance(exc, network):
        return Diagnosis(
            "network",
            "I couldn't reach the internet just now — please try again in a minute.",
            f"Network error: {type(exc).__name__}: {_short(exc)}. If it persists, check "
            "the VM is up and has connectivity.",
        )
    return None


def _storage(exc):
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    text = str(exc).lower()
    if "disk is full" in text:
        # A full disk stops SQLite writing, and every message begins by logging
        # a usage row - so every single message fails while the bot looks fine.
        return Diagnosis(
            "storage_full",
            "I can't save anything right now — the server has run out of disk space, so "
            f"trying again won't help. {ADMIN_NAME} has been told.",
            "The VM's disk is full. `df -h` to confirm; `sudo journalctl --vacuum-size=200M` "
            "usually frees enough. Then send any message to check.",
            alert=True, sure_not_saved=True,
        )
    if "database is locked" in text:
        return Diagnosis(
            "storage_busy",
            "I was busy saving something else — please try again in a moment.",
            sure_not_saved=True,
        )
    return None


def _telegram(exc):
    if isinstance(exc, tg_error.BadRequest) and "parse entities" in str(exc).lower():
        # The reply's own formatting broke - text that should have been escaped
        # wasn't. That is a bug in the bot, never the family's doing.
        return Diagnosis(
            "formatting_bug",
            f"I tripped over my own formatting writing that reply. {ADMIN_NAME} has been told.",
            f"Telegram rejected a reply's Markdown: {_short(exc)}. Some model- or "
            "user-derived text reached a Markdown message unescaped.",
            alert=True, needs_ref=True,
        )
    if isinstance(exc, (tg_error.TimedOut, tg_error.NetworkError)):
        return Diagnosis(
            "telegram_unreachable",
            "I had trouble talking to Telegram just now — please try again in a minute.",
        )
    return None


def _unknown(exc):
    return Diagnosis(
        "unknown",
        "Something went wrong on my end that I don't recognise. Please try once more — "
        f"if it happens again, send {ADMIN_NAME} the reference below.",
        f"Unexpected {type(exc).__name__}: {_short(exc)}",
        alert=True, needs_ref=True,
    )


# ----- wording helpers -----

def _status(exc, default="?"):
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "resp", None)
        status = getattr(resp, "status", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return default


def _short(exc, limit=200) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def with_ref(text: str, diagnosis: Diagnosis, ref: str, for_admin: bool) -> str:
    if not diagnosis.needs_ref:
        return text
    if for_admin:
        return f"{text}\n\nRef {ref} — `journalctl -u familybot | grep {ref}` for the traceback."
    return f"{text}\n\nRef: {ref}"


def failure_reply(lead_not_saved: str, lead_unsure: str, diagnosis: Diagnosis,
                  for_admin: bool = False) -> str:
    """Wording for a write or send that failed. Says for certain that nothing
    happened only when the error proves it; otherwise asks for a check first,
    because retrying a write that DID land makes a duplicate."""
    lead = lead_not_saved if diagnosis.sure_not_saved else lead_unsure
    detail = diagnosis.for_admin() if for_admin else diagnosis.reply
    return f"{lead} {detail}"
