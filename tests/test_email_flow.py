"""Drafting an email, approving it, and the things that must never happen.

test_gmail_tools.py covers the allowlist in isolation. This covers the wiring:
who is allowed to trigger a send, what the approver is actually shown, and
whether a tap can reach Gmail without one.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import calendar_tools as ct
import db
import event_parser as ep
import gmail_tools as gt
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
WIFE_ID = 222
MONDAY = datetime.date(2026, 9, 7)


@pytest.fixture
def email_on(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Swim Centre <swim@example.com>")
    monkeypatch.setenv("DAUGHTER_1_NAME", "Child_1")
    monkeypatch.setenv("DAUGHTER_2_NAME", "Child_2")
    monkeypatch.delenv("CALENDAR_ID", raising=False)
    for slot, child in ((1, "child_1"), (2, "child_2")):
        for category in ct.CATEGORIES:
            monkeypatch.setenv(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID", f"cal-{child}-{category}")
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "count_recent_emails", lambda minutes: 0)
    monkeypatch.setattr(main, "_find_candidates", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(ep, "compose_email", lambda *a, **k: "Dear Swim Centre,\n\nCould we move Child_1?\n\nThank you,")


def _update(user_id=ADMIN_ID):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = 1
    update.message.reply_text = AsyncMock()
    del update.edit_message_text
    return update


def _query(user_id=ADMIN_ID):
    query = MagicMock()
    query.data = "sendmail"
    query.from_user.id = user_id
    query.message.chat.id = 1
    query.edit_message_text = AsyncMock()
    query.answer = AsyncMock()
    return query


EMAIL_RESULT = {"action": "email", "status": "ok", "centre": "swim centre",
                "request": "move Saturday's class to Sunday",
                "search_text": "", "prefer_date": None}


# ----- the parser -----

def test_asking_to_contact_a_centre_is_an_email(email_on):
    result = ep._finalize(
        {"action": "email", "centre": "swim centre", "request": "move to Sunday"}, "src")
    assert result["action"] == "email"
    assert result["centre"] == "swim centre"


def test_an_email_with_no_centre_asks_who(email_on):
    result = ep._finalize({"action": "email", "request": "move to Sunday"}, "src")
    assert result["status"] == "needs_clarification"


def test_an_email_with_nothing_to_ask_asks_what(email_on):
    result = ep._finalize({"action": "email", "centre": "swim centre"}, "src")
    assert result["status"] == "needs_clarification"


# ----- who may trigger one -----

def test_only_the_admin_can_email_a_centre(email_on, monkeypatch):
    """Separate from `write` on purpose. The editor may reshape the family's
    own calendar all day; writing to an outside party in the family's name is
    a different kind of act."""
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {str(ADMIN_ID): "admin", str(WIFE_ID): "editor"})
    monkeypatch.setattr(main, "_present_email", AsyncMock())
    update = _update(WIFE_ID)

    asyncio.run(main._dispatch(update, 1, WIFE_ID, dict(EMAIL_RESULT), "src"))

    main._present_email.assert_not_awaited()
    assert "admin" in update.message.reply_text.await_args.args[0].lower()


def test_the_admin_gets_the_draft(email_on, monkeypatch):
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {str(ADMIN_ID): "admin"})
    update = _update()

    asyncio.run(main._dispatch(update, 1, ADMIN_ID, dict(EMAIL_RESULT), "src"))

    assert "Send this email?" in update.message.reply_text.await_args.args[0]


# ----- what the card shows -----

def test_the_card_shows_the_recipient_and_the_whole_body(email_on):
    update = _update()
    asyncio.run(main._present_email(update, 1, ADMIN_ID, dict(EMAIL_RESULT)))

    text = update.message.reply_text.await_args.args[0]
    assert "swim@example.com" in text          # the actual address, not just a name
    assert "Could we move Child\\_1?" in text        # the body verbatim
    assert "Subject:" in text


def test_an_unknown_centre_never_produces_a_draft(email_on, monkeypatch):
    """Refused before the model is asked to write anything, so there is never
    a half-finished email looking for somewhere to go."""
    monkeypatch.setattr(ep, "compose_email", lambda *a, **k: pytest.fail("drafted for nobody"))
    update = _update()

    asyncio.run(main._present_email(update, 1, ADMIN_ID, {**EMAIL_RESULT, "centre": "Karate Club"}))

    assert "not a centre I can email" in update.message.reply_text.await_args.args[0]


def test_an_address_in_the_message_is_not_a_recipient(email_on, monkeypatch):
    """The §6 injection: content saying "reply instead to X". There is no code
    path from message text to an address."""
    monkeypatch.setattr(ep, "compose_email", lambda *a, **k: pytest.fail("drafted for nobody"))
    update = _update()

    asyncio.run(main._present_email(update, 1, ADMIN_ID,
                                    {**EMAIL_RESULT, "centre": "attacker@evil.com"}))

    assert "not a centre I can email" in update.message.reply_text.await_args.args[0]


def test_email_off_says_so_rather_than_failing(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "")
    update = _update()
    asyncio.run(main._present_email(update, 1, ADMIN_ID, dict(EMAIL_RESULT)))
    assert "isn't set up" in update.message.reply_text.await_args.args[0]


# ----- sending -----

@pytest.fixture
def ready_to_send(monkeypatch):
    sent = {}
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Swim Centre <swim@example.com>")
    monkeypatch.setattr(main.db, "pop_pending", lambda cid, kind: {
        "address": "swim@example.com", "centre": "Swim Centre", "subject": "Schedule change",
        "body": "Dear Swim Centre,", "event_id": "ev1", "calendar_key": "child_1_enrichment",
        "summary": "Child_1 10am Swimming"})
    monkeypatch.setattr(main.db, "count_recent_emails", lambda minutes: 0)
    monkeypatch.setattr(main.db, "log_audit", lambda *a, **k: None)
    monkeypatch.setattr(main.db, "track_thread", lambda *a, **k: sent.setdefault("tracked", a))
    def _send(to, subject, body):
        sent["sent"] = {"to": to, "subject": subject, "body": body}
        return {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr(gt, "send", _send)
    return sent


def test_a_tap_sends_and_remembers_the_thread(ready_to_send):
    query = _query()
    asyncio.run(main._send_pending_email(query, ADMIN_ID, "admin", 1))

    assert ready_to_send["sent"]["to"] == "swim@example.com"
    assert ready_to_send["tracked"][0] == "t1"
    assert "Sent to Swim Centre" in query.edit_message_text.await_args.args[0]


def test_nothing_is_sent_without_a_pending_card(monkeypatch):
    """No card, no send - the approval is the authorisation, so a stray tap
    on an expired message must not reach Gmail."""
    monkeypatch.setattr(main.db, "pop_pending", lambda cid, kind: None)
    monkeypatch.setattr(gt, "send", lambda *a, **k: pytest.fail("sent with no approval"))
    query = _query()

    asyncio.run(main._send_pending_email(query, ADMIN_ID, "admin", 1))

    assert "expired" in query.edit_message_text.await_args.args[0]


def test_the_daily_cap_is_rechecked_at_send_time(ready_to_send, monkeypatch):
    """A card can sit for half an hour. The cap bounds what LEAVES the system,
    not what was proposed."""
    monkeypatch.setattr(main.db, "count_recent_emails", lambda minutes: 99)
    monkeypatch.setattr(gt, "send", lambda *a, **k: pytest.fail("sent past the cap"))
    query = _query()

    asyncio.run(main._send_pending_email(query, ADMIN_ID, "admin", 1))

    assert "limit reached" in query.edit_message_text.await_args.args[0]


def test_a_failed_send_is_audited_and_admitted(ready_to_send, monkeypatch):
    logged = []
    monkeypatch.setattr(main.db, "log_audit",
                        lambda tid, role, action, summary, details=None, status="success":
                        logged.append(status))
    monkeypatch.setattr(gt, "send", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp")))
    query = _query()

    asyncio.run(main._send_pending_email(query, ADMIN_ID, "admin", 1))

    assert "failed" in logged
    # An unrecognised failure cannot prove nothing went out, so it asks for a
    # check rather than claiming "didn't send" and inviting a duplicate email.
    sent = query.edit_message_text.await_args.args[0]
    assert "may not have sent" in sent
    assert "Sent folder" in sent


def test_an_editor_cannot_tap_send(email_on, monkeypatch):
    monkeypatch.setattr(main.permissions, "PERMISSIONS", {str(ADMIN_ID): "admin", str(WIFE_ID): "editor"})
    monkeypatch.setattr(main, "_send_pending_email", AsyncMock())
    query = _query(WIFE_ID)
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 1
    update.effective_user.id = WIFE_ID

    asyncio.run(main.handle_callback(update, MagicMock()))

    main._send_pending_email.assert_not_awaited()
    query.answer.assert_awaited()


# ----- the thread record -----

def test_a_sent_thread_can_be_found_again(tmp_path, monkeypatch):
    """Stored so a forwarded reply can be tied back to what was asked, instead
    of the bot re-reading an email thread from scratch."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    db.track_thread("t1", "m1", "Swim Centre", "swim@example.com",
                    "ev1", "child_1_enrichment", "Child_1 10am Swimming")

    threads = db.open_threads()
    assert len(threads) == 1 and threads[0]["centre"] == "Swim Centre"

    db.close_thread("t1")
    assert db.open_threads() == []


def test_emails_are_counted_across_everyone(tmp_path, monkeypatch):
    """Not per-user, unlike the write limit. A cap on email protects people
    OUTSIDE the family, and they do not care which account sent it."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    db.log_audit(ADMIN_ID, "admin", "email", "one")
    db.log_audit(WIFE_ID, "editor", "email", "two")
    db.log_audit(ADMIN_ID, "admin", "create", "not an email")

    assert db.count_recent_emails(60) == 2


# ----- ✏️ on an email card -----

def test_edit_works_on_an_email_card(email_on, monkeypatch):
    """Email cards stash their state under a different pending kind and offer
    the same ✏️ button. The edit handler only looked for "confirm", so this
    answered "expired" - found by probing the flow, not by any unit test."""
    popped, saved = [], []

    def _pop(chat_id, kind):
        popped.append(kind)
        if kind == "email_confirm":
            return {"address": "swim@example.com", "centre": "Swim Centre",
                    "subject": "s", "body": "b", "summary": "move Saturday",
                    "source_text": "ask the swim centre to move saturday"}
        return None

    monkeypatch.setattr(main.db, "pop_pending", _pop)
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.append((kind, payload)))
    monkeypatch.setattr(main, "_note_friction", AsyncMock())
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")

    query = _query()
    query.data = "edit"
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 1
    update.effective_user.id = ADMIN_ID

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert "email_confirm" in popped
    assert "What should I change?" in query.edit_message_text.await_args.args[0]
    kind, payload = saved[0]
    assert kind == "clarification"
    assert payload["card"] == "email"
    assert payload["text"] == "ask the swim centre to move saturday"


def test_an_uncaptioned_email_card_still_has_something_to_edit(email_on, monkeypatch):
    """_describe_pending reads dates and times an email card does not have."""
    saved = []
    monkeypatch.setattr(main.db, "pop_pending", lambda cid, kind: (
        {"centre": "Swim Centre", "summary": "move Saturday", "source_text": ""}
        if kind == "email_confirm" else None))
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.append(payload))
    monkeypatch.setattr(main, "_note_friction", AsyncMock())
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")

    query = _query()
    query.data = "edit"
    update = MagicMock()
    update.callback_query = query
    update.effective_chat.id = 1
    update.effective_user.id = ADMIN_ID

    asyncio.run(main.handle_callback(update, MagicMock()))

    assert "Swim Centre" in saved[0]["text"]
