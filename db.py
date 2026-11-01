import datetime
import json
import sqlite3
import threading

DB_PATH = "family_scheduler.db"

# One pending action per chat at a time (clarification text, a confirm card,
# or an amend picker) - a single row per chat_id mirrors that.
_DATETIME_KEYS = {"start", "end"}
_DATE_KEYS = {"date"}

# Subtrees stored verbatim, with no date coercion applied inside them.
#
# "raw" is the model's own unprocessed output, kept so a button tap can finish
# a stalled parse without a second model call. event_parser re-parses it from
# scratch, so its "date" is a plain string still awaiting validation - NOT a
# date the bot has already resolved. Coercing it turned "2026-09-03" into a
# datetime.date, and _resolve_when then called .strip() on it and took the
# whole handler down with an AttributeError. Anything under here must survive
# the round trip byte for byte.
_VERBATIM_KEYS = {"raw"}

# How long a pending action stays actionable. A confirm card is a
# human-in-the-loop checkpoint: tapping ✅ must mean "yes, to the thing I am
# looking at right now". Without a TTL, a card from days ago stays live and a
# stale clarification silently prefixes an unrelated later message - both turn
# a ✅ into consent for something the user has long stopped thinking about.
# 30 minutes comfortably covers "reply after putting the kids to bed" while
# ruling out next-day confusion.
PENDING_TTL_MINUTES = 30

_local = threading.local()


def _connection():
    # sqlite3 connections aren't safe to share across threads; each thread
    # (main.py calls this via asyncio.to_thread) gets its own.
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(DB_PATH)
    return _local.conn


def init_db():
    """Create anything missing and migrate anything outdated. Returns the
    columns the migration added, so a deploy can actually be seen to have
    worked rather than assumed."""
    conn = _connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_state (
            chat_id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            telegram_id TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            summary TEXT NOT NULL,
            details TEXT NOT NULL,
            status TEXT NOT NULL
        )
        """
    )
    # Deliberately NOT audit_log. That table is the accountability trail -
    # "who changed what" - read by /audit and used to rate-limit writes.
    # Metering every parse would pollute both. Kept separate, and prunable,
    # since these rows have no value once the window has passed.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            telegram_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            model TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_lookup ON usage_log (telegram_id, timestamp)")

    # Conversations the bot started with an enrichment centre. Only threads it
    # opened itself are ever recognised - a forwarded reply that matches
    # nothing here is treated as ordinary untrusted text, which is what stops
    # a stranger's email being read as an answer to a question nobody asked.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracked_threads (
            thread_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            centre TEXT NOT NULL,
            address TEXT NOT NULL,
            event_id TEXT,
            calendar_key TEXT,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
        )
        """
    )

    # What the family's confirmed choices have taught the bot: a phrase and
    # the category or gate it turned out to mean. Only admin/editor writes
    # reach here (see main.py), because this feeds back into the prompt and
    # would otherwise be a path from an untrusted screenshot into the model.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS learned_hints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            telegram_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            phrase TEXT NOT NULL,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hint_lookup ON learned_hints (kind, phrase)")

    # Where the bot had to ask, guess or give up. Read by /digest so the
    # wording that trips it up can be fixed deliberately rather than guessed at.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS friction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            telegram_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL
        )
        """
    )

    # How long the family said a journey between two places takes. Keyed by
    # the pair itself rather than an autoincrement id, so answering the same
    # question twice CORRECTS the number instead of stacking a second opinion
    # next to it - unlike learned_hints, where repeats are the signal.
    #
    # Place keys arrive already sorted and normalised by places.pair_key, so a
    # lookup finds the journey whichever direction it is being made in.
    #
    # `source` separates a deliberate /travel edit ('set') from a bucket tapped
    # on a confirm card ('answered'). They rank differently against places.py:
    # a considered edit beats the file, a hurried tap loses to it, so a mis-tap
    # can never quietly overwrite a number somebody set on purpose.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS travel_times (
            place_a TEXT NOT NULL,
            place_b TEXT NOT NULL,
            minutes INTEGER NOT NULL,
            telegram_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'answered',
            PRIMARY KEY (place_a, place_b)
        )
        """
    )

    # Places the family added from Telegram. Merged over places.PLACES at
    # startup by main.py - places.py never reads the database itself, which is
    # what keeps its matching testable without one.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_places (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            aliases TEXT NOT NULL,
            child TEXT,
            telegram_id TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )

    added = _add_missing_columns(conn)
    conn.commit()
    return added


# Columns added to tables that already existed in the wild. CREATE TABLE IF
# NOT EXISTS silently does nothing when the table is already there, so a
# database created before these columns existed would never gain them - and
# the first query naming one would fail at runtime, on the server, mid-message.
_ADDED_COLUMNS = {
    "usage_log": {
        "prompt_tokens": "INTEGER",
        "completion_tokens": "INTEGER",
        "model": "TEXT",
    },
    # travel_times shipped one commit before editing did, so a database that
    # already has the table will not gain this from CREATE TABLE IF NOT EXISTS.
    # SQLite requires a constant default on ALTER TABLE ADD COLUMN, which
    # 'answered' is - and is also the right reading of every row written
    # before editing existed: they all came from a bucket tap.
    "travel_times": {
        "source": "TEXT NOT NULL DEFAULT 'answered'",
    },
}


def _add_missing_columns(conn) -> list:
    """Bring an existing database up to the current schema. Idempotent, so it
    runs on every start; returns the columns it actually added."""
    added = []
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table doesn't exist yet; CREATE above already made it current
        for name, coltype in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
                added.append(f"{table}.{name}")
    return added


def missing_columns() -> list:
    """Columns the code needs that the database does not have.

    A schema the code has outgrown does not fail politely: the query is deep
    inside a handler, so safe_handler catches it and the family gets
    "Something went wrong on my end" for EVERY message, with nothing on screen
    saying why. Checked at startup instead, where it can be shouted about -
    the same reason calendar_tools.preflight() exists.

    Separate from _add_missing_columns on purpose. That one is the fix; this
    one is the proof it worked, and an empty list here is the only evidence
    that a deploy actually took.
    """
    conn = _connection()
    missing = []
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            missing.append(f"{table} (table absent)")
            continue
        missing += [f"{table}.{name}" for name in columns if name not in existing]
    return missing


def _encode(value, key=None):
    if key in _VERBATIM_KEYS:
        return value
    if isinstance(value, dict):
        return {k: _encode(v, k) for k, v in value.items()}
    if isinstance(value, list):
        # e.g. trade-off ranking's "options": [{"start": dt, "end": dt}, ...] -
        # each list element keeps its own dict keys for the date/datetime check.
        return [_encode(v, key) for v in value]
    if key in _DATETIME_KEYS and isinstance(value, datetime.datetime):
        return value.isoformat()
    if key in _DATE_KEYS and isinstance(value, datetime.date):
        return value.isoformat()
    return value


def _decode(value, key=None):
    """Mirror of _encode. Note the asymmetry it has to tolerate: _encode only
    converts values that are genuinely datetime objects, but a payload can
    also contain a string under one of these key names that was never a date
    at all - the model's own raw output uses "date" and returns "" when the
    user said "Tuesday" rather than a calendar date. So a value that doesn't
    parse is passed through unchanged instead of raising, which would
    otherwise strand the whole pending state and lose the user's request."""
    if key in _VERBATIM_KEYS:
        return value
    if isinstance(value, dict):
        return {k: _decode(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v, key) for v in value]
    if isinstance(value, str) and (key in _DATETIME_KEYS or key in _DATE_KEYS):
        parse = datetime.datetime.fromisoformat if key in _DATETIME_KEYS else datetime.date.fromisoformat
        try:
            return parse(value)
        except ValueError:
            return value
    return value


def save_pending(chat_id: int, kind: str, payload: dict) -> None:
    conn = _connection()
    conn.execute(
        """
        INSERT INTO pending_state (chat_id, kind, payload, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            kind = excluded.kind, payload = excluded.payload, created_at = excluded.created_at
        """,
        (chat_id, kind, json.dumps(_encode(payload)), datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    conn.commit()


def _is_expired(created_at: str, now: datetime.datetime) -> bool:
    try:
        created = datetime.datetime.fromisoformat(created_at)
    except ValueError:
        return True  # unparseable timestamp: treat as stale rather than trust it
    return (now - created) > datetime.timedelta(minutes=PENDING_TTL_MINUTES)


def pop_pending(chat_id: int, kind: str) -> dict | None:
    """Return and delete the pending payload for chat_id, but only if its
    kind matches (mirrors the old per-purpose dicts: reading pending_confirm
    never accidentally picked up an unrelated pending_amend_pick entry).
    Also enforces PENDING_TTL_MINUTES: an expired row is deleted and reported
    as absent, so a long-abandoned confirm card can never be acted on. The
    caller then shows the same "that request expired" message it already uses
    for a missing row.
    """
    conn = _connection()
    row = conn.execute(
        "SELECT kind, payload, created_at FROM pending_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row is None or row[0] != kind:
        return None
    conn.execute("DELETE FROM pending_state WHERE chat_id = ?", (chat_id,))
    conn.commit()
    if _is_expired(row[2], datetime.datetime.now(datetime.timezone.utc)):
        return None
    return _decode(json.loads(row[1]))


def peek_pending(chat_id: int, kind: str) -> dict | None:
    """The pending payload WITHOUT consuming it, same kind and TTL rules.

    Exists for the travel question, which sits on a live confirm card: answering
    "how long is that journey?" must leave the card confirmable. Popping and
    re-saving would work almost always, and would drop the pending write in the
    window between the two if anything in the middle raised - on the one code
    path where losing state means a confirm card that silently expires.
    """
    conn = _connection()
    row = conn.execute(
        "SELECT kind, payload, created_at FROM pending_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    if row is None or row[0] != kind:
        return None
    if _is_expired(row[2], datetime.datetime.now(datetime.timezone.utc)):
        return None
    return _decode(json.loads(row[1]))


def clear_pending(chat_id: int) -> None:
    conn = _connection()
    conn.execute("DELETE FROM pending_state WHERE chat_id = ?", (chat_id,))
    conn.commit()


def log_audit(
    telegram_id, role: str, action: str, summary: str, details: dict | None = None, status: str = "success"
) -> None:
    """Permanent record of every write attempt (not just successes) - this is
    the accountability trail, separate from operational debug logging."""
    conn = _connection()
    conn.execute(
        """
        INSERT INTO audit_log (timestamp, telegram_id, role, action, summary, details, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            str(telegram_id),
            role or "unknown",
            action,
            summary,
            json.dumps(_encode(details or {})),
            status,
        ),
    )
    conn.commit()


def log_usage(telegram_id, kind: str) -> int:
    """Record one metered LLM call (kind: 'text' or 'image') and return its row
    id. Written BEFORE the call, so a burst of concurrent messages can't slip
    past the rate limit while several are in flight; the token counts are only
    known afterwards and get filled in by record_usage_tokens."""
    conn = _connection()
    cursor = conn.execute(
        "INSERT INTO usage_log (timestamp, telegram_id, kind) VALUES (?, ?, ?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), str(telegram_id), kind),
    )
    conn.commit()
    return cursor.lastrowid


def record_usage_tokens(row_id, prompt_tokens, completion_tokens, model) -> None:
    """Fill in what a call actually cost, once the response has come back."""
    if not row_id:
        return
    conn = _connection()
    conn.execute(
        "UPDATE usage_log SET prompt_tokens = ?, completion_tokens = ?, model = ? WHERE id = ?",
        (prompt_tokens, completion_tokens, model, row_id),
    )
    conn.commit()


def usage_since(since: datetime.datetime) -> dict:
    """Totals for a window: calls per kind, and token sums. Tokens are NULL for
    calls that failed before a response arrived, so COALESCE keeps those rows
    counted as calls without corrupting the token total."""
    conn = _connection()
    row = conn.execute(
        """
        SELECT COUNT(*),
               COALESCE(SUM(prompt_tokens), 0),
               COALESCE(SUM(completion_tokens), 0)
        FROM usage_log WHERE timestamp >= ?
        """,
        (since.isoformat(),),
    ).fetchone()
    by_kind = dict(
        conn.execute(
            "SELECT kind, COUNT(*) FROM usage_log WHERE timestamp >= ? GROUP BY kind",
            (since.isoformat(),),
        ).fetchall()
    )
    return {
        "calls": row[0],
        "prompt_tokens": row[1],
        "completion_tokens": row[2],
        "total_tokens": row[1] + row[2],
        "by_kind": by_kind,
    }


def record_hint(telegram_id, kind: str, phrase: str, value: str) -> None:
    """Remember that `phrase` turned out to mean `value` (kind: 'category' or
    'gate'). One row per observation; frequency is what makes a hint
    trustworthy, so repeats are kept rather than deduplicated."""
    conn = _connection()
    conn.execute(
        "INSERT INTO learned_hints (timestamp, telegram_id, kind, phrase, value) VALUES (?, ?, ?, ?, ?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), str(telegram_id), kind, phrase, value),
    )
    conn.commit()


def top_hints(kind: str, limit: int = 20) -> list:
    """The most-confirmed phrase -> value pairs, as (phrase, value, count).

    Only the single most frequent value per phrase is returned: if "swimming"
    was filed under enrichment four times and others once, the prompt should
    be told enrichment, not handed a contradiction. The cap exists because
    these go into every prompt - unbounded growth would be both a cost and a
    prompt-stuffing vector."""
    conn = _connection()
    rows = conn.execute(
        """
        SELECT phrase, value, COUNT(*) AS n
        FROM learned_hints WHERE kind = ?
        GROUP BY phrase, value
        ORDER BY n DESC, phrase
        """,
        (kind,),
    ).fetchall()
    best = {}
    for phrase, value, count in rows:
        if phrase not in best:
            best[phrase] = (phrase, value, count)
    return sorted(best.values(), key=lambda r: (-r[2], r[0]))[:limit]


def clear_hints() -> int:
    conn = _connection()
    cursor = conn.execute("DELETE FROM learned_hints")
    conn.commit()
    return cursor.rowcount


# ----- travel times -----

SET_BY_HAND = "set"        # a deliberate /travel edit - beats places.py
ANSWERED_ON_CARD = "answered"  # a bucket tapped on a confirm card - loses to it


def set_travel_minutes(pair, minutes: int, telegram_id, source=ANSWERED_ON_CARD) -> None:
    """Record how long the journey between a pair of places takes.

    Upserts on the pair. A second answer is a correction, not a vote: the
    family re-answering "how long from school to Centre_1" means the first
    number was wrong, and averaging the two would produce a figure nobody gave.

    A bucket tap must NOT quietly downgrade a deliberate edit, so an
    'answered' write leaves an existing 'set' row alone. The reverse is fine -
    editing on purpose is exactly how you overrule an earlier tap.
    """
    place_a, place_b = pair
    conn = _connection()
    if source == ANSWERED_ON_CARD:
        existing = conn.execute(
            "SELECT source FROM travel_times WHERE place_a = ? AND place_b = ?",
            (place_a, place_b),
        ).fetchone()
        if existing and existing[0] == SET_BY_HAND:
            return
    conn.execute(
        """
        INSERT INTO travel_times (place_a, place_b, minutes, telegram_id, timestamp, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(place_a, place_b) DO UPDATE SET
            minutes = excluded.minutes,
            telegram_id = excluded.telegram_id,
            timestamp = excluded.timestamp,
            source = excluded.source
        """,
        (place_a, place_b, int(minutes), str(telegram_id),
         datetime.datetime.now(datetime.timezone.utc).isoformat(), source),
    )
    conn.commit()


def travel_times_by_source() -> tuple:
    """(overrides, answers), each {(place_a, place_b): minutes}.

    Two dicts rather than one because they rank on opposite sides of the
    seeded table in places.py, so the caller cannot merge them and still get
    the precedence right.

    Read whole rather than one pair at a time: a clash check compares every
    pair of a week's events, and a query per comparison would be dozens of
    round trips inside a Telegram handler that is already waiting on Google.
    """
    conn = _connection()
    rows = conn.execute("SELECT place_a, place_b, minutes, source FROM travel_times").fetchall()
    overrides, answers = {}, {}
    for a, b, minutes, source in rows:
        target = overrides if source == SET_BY_HAND else answers
        target[(a, b)] = minutes
    return overrides, answers


def all_travel_times() -> dict:
    """Every stored journey, ignoring where it came from. For listings and
    tests that do not care about precedence."""
    overrides, answers = travel_times_by_source()
    return {**answers, **overrides}


def forget_travel_minutes(pair) -> str:
    """Drop one stored journey. Returns what was dropped - 'set', 'answered'
    or '' - so the caller can say whether the places.py value is now back in
    force, rather than reporting a bare success that means two different
    things.
    """
    conn = _connection()
    row = conn.execute(
        "SELECT source FROM travel_times WHERE place_a = ? AND place_b = ?",
        (pair[0], pair[1]),
    ).fetchone()
    if not row:
        return ""
    conn.execute(
        "DELETE FROM travel_times WHERE place_a = ? AND place_b = ?", (pair[0], pair[1])
    )
    conn.commit()
    return row[0] or ANSWERED_ON_CARD


# ----- places the family added from Telegram -----

def add_user_place(key, label, aliases, child, telegram_id) -> None:
    """Aliases are stored newline-joined rather than as JSON: they are a flat
    list of short strings, and a newline cannot appear in one."""
    conn = _connection()
    conn.execute(
        """
        INSERT INTO user_places (key, label, aliases, child, telegram_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            label = excluded.label, aliases = excluded.aliases, child = excluded.child,
            telegram_id = excluded.telegram_id, timestamp = excluded.timestamp
        """,
        (key, label, "\n".join(aliases), child, str(telegram_id),
         datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    conn.commit()


def remove_user_place(key) -> int:
    """Undo an added place. Its journeys are left alone: they are keyed by
    place name, so re-adding the place under the same name finds them again,
    and deleting them would throw away numbers a human gave."""
    conn = _connection()
    cursor = conn.execute("DELETE FROM user_places WHERE key = ?", (key,))
    conn.commit()
    return cursor.rowcount


def all_user_places() -> dict:
    """{key: {"label", "aliases", "child"}} - the shape places.PLACES uses, so
    the two can be merged without translation."""
    conn = _connection()
    rows = conn.execute("SELECT key, label, aliases, child FROM user_places").fetchall()
    return {
        key: {"label": label,
              "aliases": [a for a in (aliases or "").split("\n") if a],
              **({"child": child} if child else {})}
        for key, label, aliases, child in rows
    }


def log_friction(telegram_id, kind: str, detail: str) -> None:
    """Record a moment the bot had to ask, guess or give up."""
    conn = _connection()
    conn.execute(
        "INSERT INTO friction_log (timestamp, telegram_id, kind, detail) VALUES (?, ?, ?, ?)",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(), str(telegram_id), kind, detail[:200]),
    )
    conn.commit()


def friction_since(since: datetime.datetime, limit: int = 15) -> list:
    """Most common friction points in a window, as (kind, detail, count)."""
    conn = _connection()
    return conn.execute(
        """
        SELECT kind, detail, COUNT(*) AS n
        FROM friction_log WHERE timestamp >= ?
        GROUP BY kind, detail
        ORDER BY n DESC, kind
        LIMIT ?
        """,
        (since.isoformat(), limit),
    ).fetchall()


def tokens_this_month() -> int:
    """Token total since the start of the current UTC month - the number the
    budget cap is enforced against."""
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return usage_since(start)["total_tokens"]


def count_recent_usage(telegram_id, window_minutes: int) -> int:
    conn = _connection()
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    ).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM usage_log WHERE telegram_id = ? AND timestamp >= ?",
        (str(telegram_id), cutoff),
    ).fetchone()
    return row[0]


def prune_usage(older_than_minutes: int) -> int:
    """Drop metering rows past the window. Unlike audit_log these are
    disposable, and on a 24/7 host an unbounded table is just slow growth."""
    conn = _connection()
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=older_than_minutes)
    ).isoformat()
    cursor = conn.execute("DELETE FROM usage_log WHERE timestamp < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


# Actions that count toward the per-user write rate limit. A confirmed
# multi-date batch (a term of weekly classes) logs its first event as "create"
# and the rest as "create_series" / "amend_series" - present in the audit
# trail, but NOT counted here, because the limit is meant to bound *unreviewed*
# writes and 22 events behind one ✅ tap is one reviewed decision.
WRITE_ACTIONS = ("create", "amend", "delete")


def count_recent_writes(telegram_id, window_minutes: int) -> int:
    """How many write attempts this user made inside the window - reuses the
    audit log as the source of truth, so rate limiting needs no extra table.
    Counts failed attempts too: someone hammering a failing write should be
    slowed down just as much as someone succeeding.

    The action filter is not redundant: writes happen to be the only thing
    logged here today, so counting every row gives the right answer by
    accident. Anything else ever written to audit_log would silently tighten
    the write limit for everyone."""
    conn = _connection()
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    ).isoformat()
    placeholders = ", ".join("?" * len(WRITE_ACTIONS))
    row = conn.execute(
        f"SELECT COUNT(*) FROM audit_log WHERE telegram_id = ? AND timestamp >= ? "
        f"AND action IN ({placeholders})",
        (str(telegram_id), cutoff, *WRITE_ACTIONS),
    ).fetchone()
    return row[0]


def count_recent_emails(window_minutes: int) -> int:
    """Emails sent by ANYONE inside the window.

    Deliberately not per-user, unlike count_recent_writes. A calendar write
    limit protects the family's own data from one runaway account; an email
    limit protects people OUTSIDE the family from the bot, and they do not
    care which account sent it. Counts attempts, not successes, so a failing
    loop is throttled too.
    """
    conn = _connection()
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=window_minutes)
    ).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE timestamp >= ? AND action = 'email'",
        (cutoff,),
    ).fetchone()
    return row[0]


def track_thread(thread_id: str, message_id: str, centre: str, address: str,
                 event_id: str | None, calendar_key: str | None, summary: str) -> None:
    """Remember a conversation the bot started.

    The point is attribution when a reply is forwarded back in: it lets the
    bot say "this is about Child_1's Saturday swimming" instead of re-parsing an
    email thread from scratch. Stored on send, because that is the only moment
    all of it is known for certain.
    """
    conn = _connection()
    conn.execute(
        "INSERT OR REPLACE INTO tracked_threads "
        "(thread_id, message_id, centre, address, event_id, calendar_key, summary, created_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')",
        (thread_id, message_id, centre, address, event_id, calendar_key, summary,
         datetime.datetime.now(datetime.timezone.utc).isoformat()),
    )
    conn.commit()


def open_threads(limit: int = 10) -> list[dict]:
    conn = _connection()
    rows = conn.execute(
        "SELECT thread_id, centre, summary, created_at FROM tracked_threads "
        "WHERE status = 'open' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    # Named explicitly: this connection has no row_factory, so rows are plain
    # tuples and dict(row) would raise.
    return [
        {"thread_id": r[0], "centre": r[1], "summary": r[2], "created_at": r[3]}
        for r in rows
    ]


def close_thread(thread_id: str) -> None:
    conn = _connection()
    conn.execute("UPDATE tracked_threads SET status = 'closed' WHERE thread_id = ?", (thread_id,))
    conn.commit()


def recent_audit(limit: int = 10) -> list[dict]:
    conn = _connection()
    rows = conn.execute(
        "SELECT timestamp, telegram_id, role, action, summary, status FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"timestamp": r[0], "telegram_id": r[1], "role": r[2], "action": r[3], "summary": r[4], "status": r[5]}
        for r in rows
    ]
