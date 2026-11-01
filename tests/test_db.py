"""Tests for db.py: pending-state persistence, audit log, rate-limit counter.
Each test gets a fresh temp database via the fixture - never the real one."""
import datetime
import threading

import pytest

import db


@pytest.fixture
def fresh_db(tmp_path):
    old_path = db.DB_PATH
    db.DB_PATH = str(tmp_path / "test.db")
    db._local = threading.local()  # drop any cached connection to the old path
    db.init_db()
    yield
    db._local = threading.local()
    db.DB_PATH = old_path


# ----- pending-state expiry (HITL: a stale ✅ must not stay actionable) -----

def _age_pending(chat_id, minutes):
    """Backdate a stored row's created_at, simulating the passage of time."""
    stale = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    ).isoformat()
    conn = db._connection()
    conn.execute("UPDATE pending_state SET created_at = ? WHERE chat_id = ?", (stale, chat_id))
    conn.commit()


def test_expired_pending_is_not_returned(fresh_db):
    db.save_pending(1, "confirm", {"title": "Swimming"})
    _age_pending(1, db.PENDING_TTL_MINUTES + 1)
    assert db.pop_pending(1, "confirm") is None


def test_expired_pending_row_is_deleted(fresh_db):
    db.save_pending(1, "confirm", {"title": "Swimming"})
    _age_pending(1, db.PENDING_TTL_MINUTES + 1)
    db.pop_pending(1, "confirm")
    row = db._connection().execute(
        "SELECT COUNT(*) FROM pending_state WHERE chat_id = ?", (1,)
    ).fetchone()
    assert row[0] == 0  # cleared, not left to linger


def test_pending_just_inside_ttl_still_works(fresh_db):
    db.save_pending(1, "confirm", {"title": "Swimming"})
    _age_pending(1, db.PENDING_TTL_MINUTES - 1)
    assert db.pop_pending(1, "confirm") == {"title": "Swimming"}


def test_unparseable_timestamp_treated_as_expired(fresh_db):
    db.save_pending(1, "confirm", {"title": "Swimming"})
    conn = db._connection()
    conn.execute("UPDATE pending_state SET created_at = 'not-a-timestamp' WHERE chat_id = ?", (1,))
    conn.commit()
    assert db.pop_pending(1, "confirm") is None


def test_stale_clarification_does_not_prefix_later_message(fresh_db):
    """The subtler half of the bug: a forgotten clarification would silently
    get glued onto an unrelated message sent days later."""
    db.save_pending(5, "clarification", {"text": "swimming lesson"})
    _age_pending(5, db.PENDING_TTL_MINUTES + 60)
    assert db.pop_pending(5, "clarification") is None


# ----- pending state -----

def test_save_and_pop_roundtrip_with_datetimes(fresh_db):
    payload = {
        "kind": "create",
        "title": "Swimming",
        "all_day": False,
        "start": datetime.datetime(2026, 7, 20, 16, 0, tzinfo=datetime.timezone.utc),
        "end": datetime.datetime(2026, 7, 20, 17, 0, tzinfo=datetime.timezone.utc),
    }
    db.save_pending(1, "confirm", payload)
    got = db.pop_pending(1, "confirm")
    assert got == payload
    assert isinstance(got["start"], datetime.datetime)  # not a string


def test_pop_consumes_the_entry(fresh_db):
    db.save_pending(1, "confirm", {"title": "x"})
    assert db.pop_pending(1, "confirm") is not None
    assert db.pop_pending(1, "confirm") is None


def test_kind_mismatch_returns_none_and_keeps_row(fresh_db):
    db.save_pending(1, "confirm", {"title": "x"})
    assert db.pop_pending(1, "amend_pick") is None
    assert db.pop_pending(1, "confirm") == {"title": "x"}


def test_nested_date_in_changes_roundtrips(fresh_db):
    db.save_pending(2, "amend_pick", {"changes": {"date": datetime.date(2026, 8, 1)}})
    got = db.pop_pending(2, "amend_pick")
    assert got["changes"]["date"] == datetime.date(2026, 8, 1)


def test_list_of_dicts_with_datetimes_roundtrips(fresh_db):
    """Trade-off ranking's 'options' payload: a LIST of dicts, each holding
    real datetime objects - the encoder must recurse into list elements,
    not just nested dicts, or json.dumps blows up on a raw datetime."""
    options = [
        {"start": datetime.datetime(2026, 7, 25, 16, 0, tzinfo=datetime.timezone.utc),
         "end": datetime.datetime(2026, 7, 25, 17, 0, tzinfo=datetime.timezone.utc)},
        {"start": datetime.datetime(2026, 7, 25, 17, 0, tzinfo=datetime.timezone.utc),
         "end": datetime.datetime(2026, 7, 25, 18, 0, tzinfo=datetime.timezone.utc)},
    ]
    db.save_pending(4, "create_options", {"title": "Swim", "options": options})
    got = db.pop_pending(4, "create_options")
    assert got["options"] == options
    for opt in got["options"]:
        assert isinstance(opt["start"], datetime.datetime)


def test_new_save_replaces_previous_pending(fresh_db):
    db.save_pending(1, "clarification", {"text": "old"})
    db.save_pending(1, "confirm", {"title": "new"})
    assert db.pop_pending(1, "clarification") is None
    assert db.pop_pending(1, "confirm") == {"title": "new"}


def test_clear_pending(fresh_db):
    db.save_pending(3, "clarification", {"text": "hi"})
    db.clear_pending(3)
    assert db.pop_pending(3, "clarification") is None


def test_chats_are_isolated(fresh_db):
    db.save_pending(1, "confirm", {"title": "mine"})
    db.save_pending(2, "confirm", {"title": "yours"})
    assert db.pop_pending(1, "confirm") == {"title": "mine"}
    assert db.pop_pending(2, "confirm") == {"title": "yours"}


# ----- audit log -----

def test_audit_entries_newest_first_with_status(fresh_db):
    db.log_audit(555, "admin", "create", "Swimming")
    db.log_audit(555, "admin", "delete", "Dentist")
    db.log_audit(555, "admin", "amend", "Piano", status="failed")
    entries = db.recent_audit(10)
    assert [e["action"] for e in entries] == ["amend", "delete", "create"]
    assert entries[0]["status"] == "failed"
    assert entries[2]["status"] == "success"


def test_audit_limit(fresh_db):
    for i in range(5):
        db.log_audit(555, "admin", "create", f"event {i}")
    assert len(db.recent_audit(3)) == 3


# ----- rate-limit counter -----

def test_count_recent_writes_filters_by_user(fresh_db):
    db.log_audit(111, "admin", "create", "X")
    db.log_audit(111, "admin", "create", "Y")
    db.log_audit(222, "editor", "create", "Z")
    assert db.count_recent_writes(111, window_minutes=10) == 2
    assert db.count_recent_writes(222, window_minutes=10) == 1


def test_count_recent_writes_excludes_old_entries(fresh_db):
    db.log_audit(111, "admin", "create", "recent")
    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
    conn = db._connection()
    conn.execute(
        "INSERT INTO audit_log (timestamp, telegram_id, role, action, summary, details, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (old_ts, "111", "admin", "create", "stale", "{}", "success"),
    )
    conn.commit()
    assert db.count_recent_writes(111, window_minutes=10) == 1


def test_failed_attempts_count_toward_limit(fresh_db):
    db.log_audit(111, "admin", "create", "X", status="failed")
    assert db.count_recent_writes(111, window_minutes=10) == 1


def test_pending_survives_non_date_strings_under_date_keys(fresh_db):
    """The category-clarification payload carries the model's raw output,
    which uses "date" and leaves it "" when the user said "Tuesday". Decoding
    used to raise ValueError on that and strand the whole request - the live
    bug behind "Something went wrong on my end" when a category was tapped."""
    payload = {"raw": {"date": "", "day": "tuesday", "start": "not-a-time"}, "source_text": "x"}
    db.save_pending(7, "category", payload)
    got = db.pop_pending(7, "category")

    assert got["raw"]["date"] == ""
    assert got["raw"]["start"] == "not-a-time"


def test_raw_model_output_round_trips_untouched(fresh_db):
    """The mirror of the test above, and the *second* live "something went
    wrong": a date the model CAN parse was the dangerous case, because it
    converted silently. A dated screenshot ("Thursday, 3 September 2026") puts
    a real "2026-09-03" in raw; decoding turned it into a datetime.date, and
    event_parser - which re-parses raw from scratch and rightly expects a
    string - called .strip() on it and took the handler down.

    raw is a verbatim record of what the model said. Nothing in it is a value
    the bot has resolved yet, so nothing in it may be coerced."""
    raw = {"action": "create", "title": "Teachers' Day", "date": "2026-09-03",
           "end_time": "10:30", "dates": ["2026-09-03", "2026-09-10"]}
    db.save_pending(9, "child", {"raw": raw, "source_text": "", "children": ["Child_1", "Child_2"]})
    got = db.pop_pending(9, "child")

    assert got["raw"] == raw
    assert isinstance(got["raw"]["date"], str)


def test_pending_still_decodes_real_dates(fresh_db):
    """The tolerant decode must not have cost us the actual conversion."""
    db.save_pending(8, "confirm", {"date": datetime.date(2026, 8, 4)})
    assert db.pop_pending(8, "confirm")["date"] == datetime.date(2026, 8, 4)


def test_write_rate_limit_ignores_non_write_audit_rows(fresh_db):
    """The count must filter on action. Without it, any future non-write entry
    in audit_log would eat into the write allowance."""
    for _ in range(3):
        db.log_audit(42, "admin", "create", "Piano")
    db.log_audit(42, "admin", "query", "what's on")
    db.log_audit(42, "admin", "email_send", "draft")

    assert db.count_recent_writes(42, 10) == 3


# ----- usage metering (LLM rate limit) -----

def test_usage_is_counted_per_user_and_window(fresh_db):
    db.log_usage(111, "text")
    db.log_usage(111, "image")
    db.log_usage(222, "text")
    assert db.count_recent_usage(111, 10) == 2
    assert db.count_recent_usage(222, 10) == 1


def test_usage_excludes_rows_outside_the_window(fresh_db):
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
    conn = db._connection()
    conn.execute("INSERT INTO usage_log (timestamp, telegram_id, kind) VALUES (?, ?, ?)", (old, "111", "text"))
    conn.commit()
    db.log_usage(111, "text")
    assert db.count_recent_usage(111, 10) == 1


def test_usage_metering_does_not_touch_the_write_limit(fresh_db):
    """usage_log is deliberately a separate table - metering every parse must
    not eat into the write allowance, which is what reusing audit_log would do."""
    for _ in range(20):
        db.log_usage(111, "text")
    assert db.count_recent_writes(111, 10) == 0


def test_prune_usage_drops_only_stale_rows(fresh_db):
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=60)).isoformat()
    conn = db._connection()
    conn.execute("INSERT INTO usage_log (timestamp, telegram_id, kind) VALUES (?, ?, ?)", (old, "111", "text"))
    conn.commit()
    db.log_usage(111, "text")

    assert db.prune_usage(10) == 1
    assert db.count_recent_usage(111, 10) == 1


# ----- schema migration (the deploy-breaking case) -----

def test_migration_adds_columns_to_an_older_database(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists, so a database created before the token columns would never gain
    them - and the first /usage query would fail at runtime, on the server."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "old.db"))
    db._local = threading.local()
    conn = db._connection()
    # Exactly the pre-token schema.
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT NOT NULL, telegram_id TEXT NOT NULL, kind TEXT NOT NULL)"
    )
    conn.commit()

    db.init_db()  # must migrate, not skip

    columns = {row[1] for row in db._connection().execute("PRAGMA table_info(usage_log)")}
    assert {"prompt_tokens", "completion_tokens", "model"} <= columns
    db._local = threading.local()


def test_migration_is_idempotent(fresh_db):
    """It runs on every start, so a second pass must not raise 'duplicate
    column name'."""
    db.init_db()
    db.init_db()
    assert db._add_missing_columns(db._connection()) == []


def test_existing_rows_survive_the_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "old2.db"))
    db._local = threading.local()
    conn = db._connection()
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT NOT NULL, telegram_id TEXT NOT NULL, kind TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO usage_log (timestamp, telegram_id, kind) VALUES (?, ?, ?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), "111", "text"),
    )
    conn.commit()

    db.init_db()

    # The old row is still counted, with NULL tokens treated as zero.
    stats = db.usage_since(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5))
    assert stats["calls"] == 1
    assert stats["total_tokens"] == 0
    db._local = threading.local()


# ----- token accounting -----

def test_tokens_are_recorded_against_the_right_row(fresh_db):
    first = db.log_usage(111, "text")
    second = db.log_usage(111, "image")
    db.record_usage_tokens(second, 100, 20, "gpt-5-nano")

    stats = db.usage_since(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5))
    assert first != second
    assert stats["calls"] == 2
    assert stats["total_tokens"] == 120
    assert stats["by_kind"] == {"text": 1, "image": 1}


def test_record_usage_tokens_ignores_a_missing_row_id(fresh_db):
    """A call that failed before logging must not blow up the handler."""
    db.record_usage_tokens(None, 10, 5, "m")  # must not raise


def test_tokens_this_month_excludes_last_month(fresh_db):
    db.log_usage(111, "text")
    row = db.log_usage(111, "text")
    db.record_usage_tokens(row, 50, 10, "m")

    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)).isoformat()
    conn = db._connection()
    conn.execute(
        "INSERT INTO usage_log (timestamp, telegram_id, kind, prompt_tokens, completion_tokens)"
        " VALUES (?, ?, ?, ?, ?)",
        (old, "111", "text", 9999, 9999),
    )
    conn.commit()

    assert db.tokens_this_month() == 60


def test_budget_window_is_a_calendar_month_not_a_rolling_30_days(fresh_db):
    """The cap resets on the 1st. A rolling 30-day window would behave very
    differently near month end, so this pins which one is intended."""
    row = db.log_usage(1, "text")
    db.record_usage_tokens(row, 100, 100, "m")

    now = datetime.datetime.now(datetime.timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    just_before = month_start - datetime.timedelta(minutes=1)

    conn = db._connection()
    conn.execute(
        "INSERT INTO usage_log (timestamp, telegram_id, kind, prompt_tokens, completion_tokens)"
        " VALUES (?, ?, ?, ?, ?)",
        (just_before.isoformat(), "1", "text", 5000, 5000),
    )
    conn.commit()

    # Last month's 10,000 tokens are excluded even though they are recent.
    assert db.tokens_this_month() == 200


# ----- learned hints -----

def test_top_hints_picks_the_most_confirmed_value_per_phrase(fresh_db):
    """A phrase filed under two categories must not hand the prompt a
    contradiction - the majority wins."""
    for _ in range(4):
        db.record_hint(1, "category", "swimming", "enrichment")
    db.record_hint(1, "category", "swimming", "others")

    hints = db.top_hints("category")
    assert hints == [("swimming", "enrichment", 4)]


def test_top_hints_is_capped(fresh_db):
    """These go into every prompt, so unbounded growth is both a cost and a
    prompt-stuffing vector."""
    for i in range(50):
        db.record_hint(1, "category", f"word{i}", "enrichment")
    assert len(db.top_hints("category", limit=20)) == 20


def test_hint_kinds_are_separate(fresh_db):
    db.record_hint(1, "category", "assembly", "school")
    db.record_hint(1, "gate", "assembly", "5")
    assert db.top_hints("category") == [("assembly", "school", 1)]
    assert db.top_hints("gate") == [("assembly", "5", 1)]


def test_clear_hints_removes_everything(fresh_db):
    db.record_hint(1, "category", "piano", "enrichment")
    assert db.clear_hints() == 1
    assert db.top_hints("category") == []


# ----- friction log -----

def test_friction_groups_and_counts(fresh_db):
    for _ in range(3):
        db.log_friction(1, "clarify", "child_1 thing tuesday")
    db.log_friction(1, "not_found", "appointment")

    rows = db.friction_since(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7))
    assert rows[0] == ("clarify", "child_1 thing tuesday", 3)
    assert ("not_found", "appointment", 1) in rows


def test_friction_detail_is_truncated(fresh_db):
    """Free text from a message must not put an unbounded blob in the db."""
    db.log_friction(1, "clarify", "x" * 5000)
    rows = db.friction_since(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
    assert len(rows[0][1]) == 200


def test_init_db_reports_what_it_migrated(tmp_path, monkeypatch):
    """The deploy's only observable proof that the migration ran."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "rep.db"))
    db._local = threading.local()
    conn = db._connection()
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT NOT NULL, telegram_id TEXT NOT NULL, kind TEXT NOT NULL)"
    )
    conn.commit()

    added = db.init_db()
    assert set(added) == {"usage_log.prompt_tokens", "usage_log.completion_tokens", "usage_log.model"}
    assert db.init_db() == []  # nothing left to do on the second run
    db._local = threading.local()


def test_missing_columns_is_empty_on_a_current_database(fresh_db):
    assert db.missing_columns() == []


def test_missing_columns_names_what_an_old_database_lacks(tmp_path, monkeypatch):
    """The startup guard's input. An un-migrated database does not fail
    politely - the query is below safe_handler, so the family would get
    "Something went wrong on my end" for every message with nothing saying
    why. This is what lets main() refuse to start instead."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "old.db"))
    db._local = threading.local()
    conn = db._connection()
    conn.execute(
        "CREATE TABLE usage_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " timestamp TEXT NOT NULL, telegram_id TEXT NOT NULL, kind TEXT NOT NULL)"
    )
    conn.commit()

    assert set(db.missing_columns()) == {
        "usage_log.prompt_tokens", "usage_log.completion_tokens", "usage_log.model",
        # This database predates travel_times entirely, so the table itself is
        # reported rather than a column of it. init_db creates it below, which
        # is the whole point: an empty list afterwards is the proof.
        "travel_times (table absent)",
    }

    db.init_db()
    assert db.missing_columns() == []
    db._local = threading.local()
