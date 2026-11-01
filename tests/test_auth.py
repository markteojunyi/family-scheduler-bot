"""Tests for calendar_tools credential handling (production-readiness 4.2/4.3).

The failure these guard against is specifically a *headless* one: on a VPS,
InstalledAppFlow.run_local_server() waits forever for a browser callback that
can never arrive, hanging a worker thread with no exception and no log line.
So the important assertion throughout is "raised AuthRequiredError" rather
than "returned something".
"""
import json
import os
import threading

import pytest

import calendar_tools
from calendar_tools import AuthRequiredError


@pytest.fixture
def isolated_token(tmp_path, monkeypatch):
    """Point TOKEN_PATH at a temp file so tests never read or clobber the
    real token.json."""
    monkeypatch.setattr(calendar_tools, "TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_PATH", raising=False)
    return tmp_path / "token.json"


# ----- the headless hang (point 2) -----

def test_missing_token_raises_instead_of_opening_browser(isolated_token, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("interactive OAuth flow must never start implicitly")

    monkeypatch.setattr(calendar_tools.InstalledAppFlow, "from_client_secrets_file", explode)

    with pytest.raises(AuthRequiredError, match="interactive OAuth is disabled"):
        calendar_tools.get_credentials()


def test_revoked_refresh_token_raises_actionable_error(isolated_token, monkeypatch):
    isolated_token.write_text(json.dumps({
        "token": "x", "refresh_token": "y", "client_id": "c",
        "client_secret": "s", "token_uri": "https://oauth2.googleapis.com/token",
    }))

    class DeadCreds:
        valid = False
        expired = True
        refresh_token = "y"

        def refresh(self, request):
            raise calendar_tools.RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(
        calendar_tools.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: DeadCreds())
    )

    with pytest.raises(AuthRequiredError) as exc:
        calendar_tools.get_credentials()

    # The message must name the actual remedies, since this is what a future
    # 3am debugging session has to work from.
    assert "--auth" in str(exc.value)
    assert "In production" in str(exc.value)  # the 7-day Testing-mode trap
    assert "service account" in str(exc.value)


def test_allow_interactive_permits_the_flow(isolated_token, monkeypatch):
    """The --auth bootstrap is the one path that may open a browser."""
    started = []

    class FakeFlow:
        def run_local_server(self, port=0):
            started.append(port)
            return _FakeCreds()

    monkeypatch.setattr(
        calendar_tools.InstalledAppFlow, "from_client_secrets_file", staticmethod(lambda *a, **k: FakeFlow())
    )
    calendar_tools.get_credentials(allow_interactive=True)
    assert started == [0]


class _FakeCreds:
    valid = True
    expired = False
    refresh_token = "r"

    def to_json(self):
        return json.dumps({"token": "fake"})


# ----- service-account mode -----

def test_service_account_mode_never_touches_oauth(isolated_token, monkeypatch, tmp_path):
    sa_file = tmp_path / "sa.json"
    sa_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", str(sa_file))
    monkeypatch.setattr(calendar_tools, "_service_account_credentials", lambda path: _FakeCreds())
    monkeypatch.setattr(
        calendar_tools.Credentials, "from_authorized_user_file",
        staticmethod(lambda *a, **k: pytest.fail("must not read token.json in service-account mode")),
    )
    assert isinstance(calendar_tools.get_credentials(), _FakeCreds)


def test_missing_service_account_file_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(AuthRequiredError, match="missing file"):
        calendar_tools.get_credentials()


# ----- atomic token write (point 3) -----

def test_write_token_is_atomic_and_leaves_no_temp(isolated_token, monkeypatch):
    monkeypatch.setattr(calendar_tools, "TOKEN_PATH", str(isolated_token))
    calendar_tools._write_token(_FakeCreds())

    assert json.loads(isolated_token.read_text()) == {"token": "fake"}
    assert not os.path.exists(f"{isolated_token}.tmp")


def test_write_token_replaces_previous_content(isolated_token, monkeypatch):
    monkeypatch.setattr(calendar_tools, "TOKEN_PATH", str(isolated_token))
    isolated_token.write_text('{"token": "old-and-longer-than-the-new-one"}')
    calendar_tools._write_token(_FakeCreds())
    # A non-atomic overwrite could leave trailing bytes of the old content.
    assert json.loads(isolated_token.read_text()) == {"token": "fake"}


def test_concurrent_get_credentials_is_serialized(isolated_token, monkeypatch):
    """Several to_thread workers hitting an expired token at once must not
    interleave their token.json writes."""
    isolated_token.write_text("{}")
    refreshes = []
    in_critical_section = []

    class SlowCreds:
        valid = False
        expired = True
        refresh_token = "y"

        def refresh(self, request):
            in_critical_section.append(1)
            assert sum(in_critical_section) == 1, "two threads refreshed concurrently"
            refreshes.append(1)
            in_critical_section.pop()

        def to_json(self):
            return json.dumps({"token": "fake"})

    monkeypatch.setattr(
        calendar_tools.Credentials, "from_authorized_user_file", staticmethod(lambda *a, **k: SlowCreds())
    )

    errors = []

    def worker():
        try:
            calendar_tools.get_credentials()
        except Exception as exc:  # surface assertion failures from the thread
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(refreshes) == 8
