"""Sending mail to enrichment centres. Send-only, allowlisted, never automatic.

THREAT_MODEL.md §6 names three things email changes at once. The design agreed
2026-08-10 removes two of them outright:

  - untrusted inbound  -> the bot never reads a mailbox. Replies come back by
                          a family member forwarding them to Telegram, so the
                          scope is gmail.send and nothing else.
  - a scheduled trigger -> there is no watcher and no timer. Every send is a
                          human tapping ✅ on a card showing the full text.

What remains is the outbound channel itself: for the first time, something the
bot produces leaves the system. Two controls sit on that, and both are here
rather than in the prompt, because a prompt is advice and this needs a rule:

  1. Recipients come ONLY from ENRICHMENT_EMAILS. An address appearing in a
     message, a screenshot or a forwarded email can never be written to - the
     resolver matches a name against configured addresses and refuses anything
     it cannot match. "Reply instead to attacker@evil.com" has nowhere to go.
  2. A hard daily cap. A loop, a bad parse or a compromised account cannot
     turn the family's mailbox into a sender of hundreds of messages.
"""
import base64
import datetime
import os
import re
import threading
from email.message import EmailMessage

from googleapiclient.discovery import build

import calendar_tools

_local = threading.local()

# Sends allowed per rolling 24h, across everyone. Generous for a family that
# emails a centre a few times a term, and a hard stop otherwise. Counted from
# the audit log, so a restart cannot reset it.
DAILY_SEND_LIMIT = int(os.environ.get("DAILY_EMAIL_LIMIT") or 10)

_ADDRESS_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class EmailNotConfigured(RuntimeError):
    """ENRICHMENT_EMAILS is empty, so there is nobody the bot may write to."""


class UnknownRecipient(ValueError):
    """A centre that is not in the allowlist. Deliberately fatal rather than
    a fallback to some default address: sending the family's scheduling
    details to the wrong place is worse than not sending at all."""


def recipients() -> dict:
    """The allowlist, as {lowercased centre name: address}.

    Configured as `Name <addr>` pairs separated by commas, e.g.
        ENRICHMENT_EMAILS="Swim Centre <swim@x.com>, Piano <piano@y.com>"
    Malformed entries are skipped rather than guessed at - a half-parsed
    address is a message sent somewhere nobody intended.
    """
    raw = (os.environ.get("ENRICHMENT_EMAILS") or "").strip()
    found = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        match = re.match(r"^(.*?)\s*<\s*([^>]+?)\s*>$", entry)
        if match:
            name, address = match.group(1).strip(), match.group(2).strip()
        else:
            name, address = entry, entry
        if not _ADDRESS_RE.match(address):
            continue
        found[name.lower()] = address
    return found


def resolve_recipient(centre) -> tuple:
    """(name, address) for a centre the user named, or raise.

    Matched the same way child names are (calendar_tools._names_match): exact,
    a word of a longer name, or a prefix. Substring-anywhere matching would
    let "piano" reach "Grand Centre_3" AND "Piano Studio" and silently
    pick one.
    """
    known = recipients()
    if not known:
        raise EmailNotConfigured(
            "No enrichment centre addresses are configured (ENRICHMENT_EMAILS)."
        )
    needle = (centre or "").strip().lower()
    if not needle:
        raise UnknownRecipient("No centre was named.")

    matches = [n for n in known if calendar_tools._names_match(needle, n)]
    if len(matches) == 1:
        return matches[0], known[matches[0]]
    if not matches:
        raise UnknownRecipient(
            f"\"{centre}\" is not a centre I can email. Configured: "
            f"{', '.join(sorted(known)) or 'none'}."
        )
    raise UnknownRecipient(
        f"\"{centre}\" matches several centres: {', '.join(sorted(matches))}."
    )


def _service():
    # Per-thread, same reason as calendar_tools._service(): httplib2 is not
    # thread-safe and every call runs in an asyncio.to_thread worker.
    if not hasattr(_local, "service"):
        _local.service = build("gmail", "v1", credentials=calendar_tools.get_credentials())
    return _local.service


def send(to_address: str, subject: str, body: str) -> dict:
    """Send one plain-text email. Returns {'id', 'threadId'} from Gmail.

    `to_address` must already have come out of resolve_recipient - this does
    not take a centre name, so there is no path where a caller passes through
    something a message said.
    """
    if to_address not in set(recipients().values()):
        # Belt and braces. The caller is meant to have resolved this already,
        # but the whole security story rests on it, so it is checked at the
        # last possible moment too.
        raise UnknownRecipient(f"{to_address} is not in the allowlist.")

    message = EmailMessage()
    message["To"] = to_address
    message["Subject"] = subject
    sender = (os.environ.get("GMAIL_USER") or "").strip()
    if sender:
        message["From"] = sender
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return _service().users().messages().send(userId="me", body={"raw": encoded}).execute()


def preflight() -> None:
    """Fail at startup, not mid-message, when email is switched on but unusable.

    Mirrors calendar_tools.preflight(). The two failures worth catching early
    are a token granted before the Gmail scope existed - which 403s on the
    first send, hours later, with the family watching - and an allowlist that
    parsed to nothing because of a typo.
    """
    if not calendar_tools.email_configured():
        return
    if not recipients():
        raise ValueError(
            "ENRICHMENT_EMAILS is set but no valid addresses parsed from it. "
            "Expected: \"Swim Centre <swim@example.com>, Piano <piano@example.com>\"."
        )
    creds = calendar_tools.get_credentials()
    granted = set(getattr(creds, "scopes", None) or [])
    if granted and calendar_tools.GMAIL_SEND_SCOPE not in granted:
        raise calendar_tools.AuthRequiredError(
            "Email is configured but the saved Google token predates the Gmail "
            "scope. Re-authorise with `python main.py --auth` on a machine with a "
            "browser, then copy token.json to this host."
        )


def sends_today(count_since) -> bool:
    """Whether the daily cap is already used up. Takes the count rather than
    reading the database itself, so this module stays free of storage."""
    return count_since >= DAILY_SEND_LIMIT


def quote_for_review(body: str, limit: int = 3000) -> str:
    """The draft as it will be sent, trimmed only if absurdly long.

    Shown in full on the approval card on purpose: approving a summary of an
    email is not approving the email. THREAT_MODEL.md §6 requires the human
    step to be real, and a human cannot approve text they were not shown.
    """
    body = body or ""
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n[… truncated for display — this is longer than expected, check before sending]"


def default_subject(child: str, activity: str) -> str:
    """Deterministic, not model-written. The subject is what the centre sees
    first and what threads the conversation; there is nothing for a model to
    add, and one less field it can be talked into changing."""
    parts = [p for p in (child, activity) if p]
    return f"Schedule change request: {' — '.join(parts)}" if parts else "Schedule change request"


def _today_start(now=None):
    now = now or datetime.datetime.now(calendar_tools.TIMEZONE)
    return now - datetime.timedelta(days=1)
