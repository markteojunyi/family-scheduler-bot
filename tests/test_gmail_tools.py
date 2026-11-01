"""Sending mail: the allowlist, and the things that must be impossible.

This is the first code in the project that can make something LEAVE the
system. THREAT_MODEL.md §6 says recipient addresses must come only from
configuration and never from content, so most of this file is about proving
an address cannot be talked into existence.
"""
import os

import pytest

import calendar_tools as ct
import gmail_tools as gt


@pytest.fixture
def centres(monkeypatch):
    monkeypatch.setenv(
        "ENRICHMENT_EMAILS",
        "Swim Centre <swim@example.com>, Piano Studio <piano@example.com>",
    )


# ----- the allowlist -----

def test_addresses_are_read_from_configuration(centres):
    assert gt.recipients() == {
        "swim centre": "swim@example.com",
        "piano studio": "piano@example.com",
    }


def test_a_bare_address_is_its_own_name(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "swim@example.com")
    assert gt.recipients() == {"swim@example.com": "swim@example.com"}


def test_a_malformed_entry_is_dropped_not_guessed(monkeypatch):
    """Half-parsing an address means sending somewhere nobody intended."""
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Broken <not-an-address>, Good <ok@example.com>")
    assert gt.recipients() == {"good": "ok@example.com"}


def test_no_configuration_means_no_recipients(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "")
    assert gt.recipients() == {}
    with pytest.raises(gt.EmailNotConfigured):
        gt.resolve_recipient("swim")


# ----- resolving a centre the user named -----

@pytest.mark.parametrize("typed", ["Swim Centre", "swim centre", "swim", "SWIM"])
def test_a_centre_can_be_named_loosely(centres, typed):
    assert gt.resolve_recipient(typed) == ("swim centre", "swim@example.com")


def test_an_unknown_centre_is_refused(centres):
    with pytest.raises(gt.UnknownRecipient):
        gt.resolve_recipient("Karate Club")


def test_an_ambiguous_centre_is_refused(monkeypatch):
    """Refused rather than picked. Sending the family's scheduling details to
    the wrong centre is worse than not sending."""
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Piano Studio <a@x.com>, Piano School <b@x.com>")
    with pytest.raises(gt.UnknownRecipient):
        gt.resolve_recipient("piano")


def test_an_address_from_content_can_never_be_resolved(centres):
    """The injection this defends against: text saying "reply instead to X".
    resolve_recipient only ever returns configured addresses, so an address
    appearing in a message has nowhere to go."""
    with pytest.raises(gt.UnknownRecipient):
        gt.resolve_recipient("attacker@evil.com")


# ----- sending -----

def test_send_refuses_an_address_not_in_the_allowlist(centres, monkeypatch):
    """The last-moment check. The caller is meant to have resolved this
    already, but the whole security story rests on it."""
    monkeypatch.setattr(gt, "_service", lambda: pytest.fail("must not reach Gmail"))
    with pytest.raises(gt.UnknownRecipient):
        gt.send("attacker@evil.com", "hi", "body")


def test_send_builds_a_plain_text_message_for_an_allowed_address(centres, monkeypatch):
    captured = {}

    class _Send:
        def execute(self):
            return {"id": "m1", "threadId": "t1"}

    class _Messages:
        def send(self, userId, body):
            captured["userId"] = userId
            captured["raw"] = body["raw"]
            return _Send()

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    monkeypatch.setattr(gt, "_service", lambda: _Service())
    monkeypatch.setenv("GMAIL_USER", "parent@example.com")

    result = gt.send("swim@example.com", "Schedule change", "Dear Swim Centre,\n\nPlease move…")

    assert result == {"id": "m1", "threadId": "t1"}
    import base64
    decoded = base64.urlsafe_b64decode(captured["raw"]).decode()
    assert "To: swim@example.com" in decoded
    assert "Subject: Schedule change" in decoded
    assert "Please move" in decoded


# ----- scopes -----

def test_the_gmail_scope_is_requested_only_when_email_is_configured(monkeypatch):
    """Adding a scope invalidates the saved consent. A family not using email
    must never be forced through a re-authorisation for a feature they do not
    have."""
    monkeypatch.setenv("ENRICHMENT_EMAILS", "")
    assert ct.scopes() == [ct.CALENDAR_SCOPE]

    monkeypatch.setenv("ENRICHMENT_EMAILS", "Swim <swim@example.com>")
    assert ct.GMAIL_SEND_SCOPE in ct.scopes()


def test_the_bot_never_asks_to_read_mail(monkeypatch):
    """Replies arrive by being forwarded to Telegram, so there is no reason
    to read a mailbox - and a send-only scope means it cannot, whatever a
    prompt injection asks for."""
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Swim <swim@example.com>")
    assert not any("readonly" in s or "modify" in s for s in ct.scopes())


# ----- preflight -----

def test_preflight_is_silent_when_email_is_off(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "")
    gt.preflight()


def test_preflight_rejects_an_allowlist_that_parsed_to_nothing(monkeypatch):
    monkeypatch.setenv("ENRICHMENT_EMAILS", "Broken <nope>")
    with pytest.raises(ValueError, match="no valid addresses"):
        gt.preflight()


def test_preflight_catches_a_token_granted_before_the_gmail_scope(centres, monkeypatch):
    """Otherwise this surfaces as a 403 on the first real send, hours later,
    with the family watching."""
    class _Creds:
        scopes = [ct.CALENDAR_SCOPE]

    monkeypatch.setattr(ct, "get_credentials", lambda *a, **k: _Creds())
    with pytest.raises(ct.AuthRequiredError, match="re-authorise|Re-authorise"):
        gt.preflight()


def test_preflight_passes_with_the_scope_granted(centres, monkeypatch):
    class _Creds:
        scopes = [ct.CALENDAR_SCOPE, ct.GMAIL_SEND_SCOPE]

    monkeypatch.setattr(ct, "get_credentials", lambda *a, **k: _Creds())
    gt.preflight()


# ----- the daily cap -----

def test_the_daily_cap_stops_a_runaway(monkeypatch):
    monkeypatch.setattr(gt, "DAILY_SEND_LIMIT", 3)
    assert not gt.sends_today(2)
    assert gt.sends_today(3)
    assert gt.sends_today(99)


# ----- what the approver sees -----

def test_a_short_draft_is_shown_in_full():
    """Approving a summary of an email is not approving the email."""
    body = "Dear Swim Centre,\n\nCould we move Child_1 to Sunday?\n\nThanks"
    assert gt.quote_for_review(body) == body


def test_an_absurdly_long_draft_says_it_was_trimmed():
    assert "truncated" in gt.quote_for_review("x" * 5000)


def test_the_subject_is_built_in_python_not_by_the_model():
    subject = gt.default_subject("Child_1", "Swimming")
    assert "Child_1" in subject and "Swimming" in subject
