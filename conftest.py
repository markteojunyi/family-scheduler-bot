"""Pytest bootstrap: runs before any test module imports project code.

Sets dummy env vars so tests are deterministic and never read the real
`.env` (load_dotenv() doesn't override vars that are already set). Living at
the project root also puts the root on sys.path, so `import main` works from
tests/ without packaging.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:TEST-FAKE-BOT-TOKEN-abcdef")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-fake-key-123456")
os.environ.setdefault("CALENDAR_ID", "test-calendar-id")
os.environ.setdefault("GOOGLE_CREDENTIALS_PATH", "credentials.json")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "999000111")

# Every *optional* var the real .env might define. Blanking them is what makes
# the isolation actually hold: setdefault above only protects the five names
# listed there, so anything else in .env would still reach the tests via
# load_dotenv() at import time - which is exactly how six real calendars once
# leaked in and broke seven multi-calendar tests. Empty string (not deletion)
# is deliberate: load_dotenv() skips vars that are already set, and every
# consumer checks truthiness, so "" reads as unset everywhere.
_OPTIONAL_VARS = [
    "GOOGLE_SERVICE_ACCOUNT_PATH",
    "DAUGHTER_1_NAME",
    "DAUGHTER_2_NAME",
    "DAUGHTER_1_CALENDAR_ID",
    "DAUGHTER_2_CALENDAR_ID",
    "WIFE_TELEGRAM_ID",
    "MOM_TELEGRAM_ID",
    "DAD_TELEGRAM_ID",
    # Email. ENRICHMENT_EMAILS especially: it decides whether the Gmail scope
    # is requested at all, so a real value leaking in would change what
    # calendar_tools.scopes() returns under every test in the suite.
    "ENRICHMENT_EMAILS",
    "GMAIL_USER",
    "DAILY_EMAIL_LIMIT",
]
_OPTIONAL_VARS += [
    f"DAUGHTER_{n}_{category}_CALENDAR_ID"
    for n in (1, 2)
    for category in ("SCHOOL", "ENRICHMENT", "OTHERS")
]

for _var in _OPTIONAL_VARS:
    os.environ.setdefault(_var, "")


import pytest


@pytest.fixture(autouse=True)
def no_real_google_api(monkeypatch):
    """Make any unstubbed Google Calendar call fail instantly, and loudly.

    Blanking env vars stops the real *calendars* leaking in; it does nothing to
    stop a real *API call*. Adding the pickup-clash lookup to the confirm card
    proved that: the suite quietly started authenticating with the live
    token.json and firing 404s at Google on every create test, and still went
    green - because the caller catches failures on purpose, so a card can warn
    or not but never crash.

    A test that reaches the network is either wrong or is testing an error
    path by accident. Failing here forces it to be stubbed deliberately, which
    is also what keeps the suite fast: this call alone doubled its runtime.
    """
    import calendar_tools

    def refuse():
        raise AssertionError(
            "Test tried to reach the real Google Calendar API. Stub the "
            "calendar_tools function it calls (e.g. list_events_range_across)."
        )

    monkeypatch.setattr(calendar_tools, "_service", refuse)
