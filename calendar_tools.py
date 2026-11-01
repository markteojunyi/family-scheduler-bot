import os
import re
import datetime
import logging
import threading
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Least privilege: calendar.events covers everything the bot does (list/
# create/patch/delete events). The broader ".../auth/calendar" scope would
# also grant control over calendar settings, sharing, and ACLs - none of
# which this bot needs.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# gmail.send and nothing else. The bot drafts replies to enrichment centres,
# but replies come BACK by the family forwarding them to Telegram, so it never
# needs to read a mailbox - and a send-only scope means it structurally
# cannot, whatever a prompt injection might ask for. Do not widen this to
# gmail.readonly or gmail.modify without revisiting THREAT_MODEL.md §6.
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def email_configured() -> bool:
    """Whether the email feature is switched on, which is what decides if the
    Gmail scope is requested at all. Opt-in on purpose: adding a scope
    invalidates the existing consent, so a family not using email must never
    be forced through a re-authorisation for a feature they do not have."""
    return bool((os.environ.get("ENRICHMENT_EMAILS") or "").strip())


def scopes() -> list:
    return [CALENDAR_SCOPE] + ([GMAIL_SEND_SCOPE] if email_configured() else [])


# Every consumer calls scopes() rather than reading a constant: the list
# depends on configuration, and a value frozen at import time would be wrong
# for anything that sets ENRICHMENT_EMAILS afterwards - including every test.
TIMEZONE = ZoneInfo("Asia/Singapore")
TOKEN_PATH = "token.json"
SEARCH_DAYS_AHEAD = 365

logger = logging.getLogger(__name__)


class AuthRequiredError(RuntimeError):
    """Credentials are absent/expired/revoked and cannot be renewed without a
    human at a browser. Raised instead of silently launching an interactive
    OAuth flow, which on a headless VPS blocks forever with no error."""


# Two supported auth modes, chosen by whether GOOGLE_SERVICE_ACCOUNT_PATH is
# set. Service-account mode is the one to prefer for an unattended 24/7 host:
# it has no refresh token to expire (a user-OAuth app left in "Testing"
# publishing status has its refresh tokens revoked after 7 days) and needs no
# browser, ever. To use it, share the calendar with the service account's
# ...iam.gserviceaccount.com address granting "Make changes to events".
def _service_account_credentials(path: str):
    from google.oauth2 import service_account  # imported lazily: only needed in this mode

    return service_account.Credentials.from_service_account_file(path, scopes=scopes())


def _write_token(creds) -> None:
    """Persist refreshed credentials atomically. A partial write would leave
    an unparseable token.json and lock the bot out until a manual re-auth, so
    the new content lands in a temp file that then atomically replaces the
    old one. Mode 0600 because this file grants calendar write access."""
    tmp_path = f"{TOKEN_PATH}.tmp"
    with open(tmp_path, "w") as f:
        f.write(creds.to_json())
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass  # best-effort: Windows dev machines don't honor POSIX modes
    os.replace(tmp_path, TOKEN_PATH)


# Serializes the file-touching part of credential setup across
# asyncio.to_thread's worker pool. Without it, several threads can hit an
# expired token at once and each rewrite token.json concurrently.
#
# Deliberately NOT a shared Credentials object: googleapiclient's transport
# refreshes credentials on its own, inside request handling and outside this
# lock, so one instance shared across threads would still be mutated
# concurrently. Each thread instead gets its own instance (same reasoning as
# _service() below) and the atomic _write_token handles the file. Concurrent
# refreshes are harmless - an installed-app refresh token is reusable.
_creds_lock = threading.Lock()


def get_credentials(allow_interactive: bool = False):
    """Return usable Google credentials, or raise AuthRequiredError.

    `allow_interactive` gates the browser-based OAuth flow. It defaults to
    False so the running bot can never launch it: on a headless host
    `run_local_server()` waits forever for a callback that cannot arrive,
    hanging a worker thread with no exception and no log line. Only the
    explicit `python main.py --auth` bootstrap passes True.
    """
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    if sa_path:
        if not os.path.exists(sa_path):
            raise AuthRequiredError(
                f"GOOGLE_SERVICE_ACCOUNT_PATH points to a missing file: {sa_path}"
            )
        return _service_account_credentials(sa_path)

    with _creds_lock:
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, scopes())

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                # Most common cause by far: the OAuth consent screen is still
                # in "Testing", so Google expires refresh tokens after 7 days.
                raise AuthRequiredError(
                    "Google refresh token is expired or revoked. Re-authorize with "
                    "`python main.py --auth`. To stop this recurring every 7 days, set the "
                    "OAuth consent screen to 'In production', or switch to a service "
                    "account via GOOGLE_SERVICE_ACCOUNT_PATH."
                ) from exc
            _write_token(creds)
            return creds

        if not allow_interactive:
            raise AuthRequiredError(
                "No usable Google credentials and interactive OAuth is disabled. "
                "Run `python main.py --auth` on a machine with a browser, then copy "
                "token.json to this host."
            )

        creds_path = os.environ["GOOGLE_CREDENTIALS_PATH"]
        flow = InstalledAppFlow.from_client_secrets_file(creds_path, scopes())
        creds = flow.run_local_server(port=0)
        _write_token(creds)
        return creds


def preflight() -> None:
    """Prove credentials work, and that every configured calendar is actually
    reachable, before the bot starts serving. Without this a dead token
    surfaces only as a generic "something went wrong" on every message,
    forever, with nothing pointing at the real cause.

    Raises AuthRequiredError (auth is broken) or ValueError (a calendar ID is
    wrong or not shared with this identity)."""
    now = _now()
    for key, info in _calendars().items():
        try:
            _list(now, now + datetime.timedelta(days=1), key, max_results=1)
        except AuthRequiredError:
            raise
        except Exception as exc:
            raise ValueError(
                f"Calendar '{info['name']}' ({info['id']}) is not reachable: {exc}. "
                "Check the ID, and that it is shared with the identity the bot "
                "authenticates as."
            ) from exc



_local = threading.local()


def _service():
    # googleapiclient's http layer (httplib2) is not thread-safe, so each
    # thread in asyncio.to_thread's pool gets its own client instead of one
    # shared instance - same pattern as db.py's per-thread connection.
    if not hasattr(_local, "service"):
        _local.service = build("calendar", "v3", credentials=get_credentials())
    return _local.service


CATEGORIES = ("school", "enrichment", "others")


def _calendars() -> dict:
    """Named calendar registry (ADR-003, extended by ADR-004), rebuilt fresh
    from env vars on every call - cheap (a few dict lookups), and avoids an
    import-time ordering dependency on load_dotenv() having already run, same
    reasoning as the original single-calendar _calendar_id().

    Each child may either be split across per-category calendars
    (DAUGHTER_N_<CATEGORY>_CALENDAR_ID, key "<child>_<category>") or use a
    single flat calendar (DAUGHTER_N_CALENDAR_ID, key "<child>", category
    None). Falls back to the legacy CALENDAR_ID for the first child so an
    existing .env keeps working unchanged."""
    calendars = {}
    for slot, default_name in ((1, "Daughter 1"), (2, "Daughter 2")):
        name = os.environ.get(f"DAUGHTER_{slot}_NAME", "").strip() or default_name
        child = name.lower()
        categorized = False
        for category in CATEGORIES:
            cal_id = os.environ.get(f"DAUGHTER_{slot}_{category.upper()}_CALENDAR_ID")
            if cal_id:
                calendars[f"{child}_{category}"] = {
                    "name": name, "id": cal_id, "child": child, "category": category,
                }
                categorized = True
        if categorized:
            continue
        flat_id = os.environ.get(f"DAUGHTER_{slot}_CALENDAR_ID")
        if slot == 1:
            flat_id = flat_id or os.environ.get("CALENDAR_ID")
        if flat_id:
            calendars[child] = {"name": name, "id": flat_id, "child": child, "category": None}
    return calendars


def calendar_names() -> list:
    """Distinct child names across every configured calendar, in order - the
    axis a human picks between, so a child split across categories still
    appears once."""
    names = []
    for c in _calendars().values():
        if c["name"] not in names:
            names.append(c["name"])
    return names


def calendar_display_name(key) -> str:
    entry = _calendars().get(key)
    if not entry:
        return str(key)
    if entry["category"]:
        return f"{entry['name']} — {entry['category'].title()}"
    return entry["name"]


def category_for_key(key):
    """The category ("school"/"enrichment"/"others") a calendar key belongs
    to, or None for an uncategorized calendar. Used to decide school-only
    title details like the pickup gate."""
    entry = _calendars().get(key)
    return entry.get("category") if entry else None


def child_display_name(key):
    """Just the child's name for a calendar key ("Child_1"), without the category
    suffix calendar_display_name adds. Used to build event titles, where
    "Child_1 10am Assembly" is wanted rather than "Child_1 — School 10am Assembly".
    None when the key has no child behind it (legacy single-calendar setups)."""
    entry = _calendars().get(key)
    if not entry or not entry.get("child"):
        return None
    return entry["name"]


def resolve_child_name(name_or_none):
    """The single child a (fuzzy) name refers to, or None if it names zero or
    several. Used to word a follow-up question about an already-resolved
    child ("Which of Child_1's calendars...")."""
    if not name_or_none or not name_or_none.strip():
        return None
    needle = name_or_none.strip().lower()
    # Shares _names_match with resolve_calendar_key deliberately. These were
    # two copies of the same substring rule, and fixing only one left "Bchild_2"
    # resolving to Child_2 here while being refused there - the two answers then
    # disagree about who a message is about, which is worse than either rule.
    matched = {c["child"] for c in _calendars().values() if _names_match(needle, c["child"])}
    return next(iter(matched)) if len(matched) == 1 else None


def categories_for_child(child) -> list:
    return [c["category"] for c in _calendars().values() if c["child"] == child and c["category"]]


def calendar_keys_for_child(child) -> list:
    return [k for k, c in _calendars().items() if c["child"] == child]


def is_multi_calendar_configured() -> bool:
    return len(_calendars()) > 1


def has_categorized_calendars() -> bool:
    return any(c["category"] for c in _calendars().values())


def _names_match(needle: str, child: str) -> bool:
    """Does what the user typed name this child?

    Matching used to be plain substring in both directions, which quietly read
    "Bchild_2" as Child_2 - "child_2" is inside "bchild_2" - and filed or deleted against
    the wrong child with no question asked. The card names the child, so
    nothing was ever written blind, but a silent misread is exactly the class
    of thing that erodes trust in the whole bot.

    So containment is only honoured at WORD boundaries:
        "child_2"       -> child_2   (exact)
        "Child_2 Tan"   -> child_2   (a word of a longer name)
        "kat"           -> katherine (a prefix, so short forms still work)
        "Bchild_2"      -> no match, and the bot asks
    """
    needle = (needle or "").strip().lower()
    child = (child or "").strip().lower()
    if not needle or not child:
        return False
    if needle == child:
        return True
    # A word of what was typed IS the child's name ("child_2 tan", "child_2's").
    if child in re.split(r"[^a-z0-9_]+", needle):
        return True
    # What was typed is a short form of the child's name ("kat" -> "katherine").
    # Prefix only: a suffix match is what let "bchild_2" through.
    return len(needle) >= 3 and child.startswith(needle)


def resolve_calendar_key(name_or_none, category=None, require_child=True, require_category=True):
    """Resolve a (possibly None, possibly fuzzy) child name plus an optional
    category to a configured calendar key. Returns (key, error): error is
    None on success, else 'ambiguous' / 'ambiguous_category' (which axis
    still needs a human answer), 'unknown' (a name was given that matched
    nothing) or 'unconfigured'.

    The two axes are gated independently because the flows need different
    things. A create must land on exactly one calendar, so both are required.
    A delete/amend must know the child but can search across their
    categories, since the user shouldn't have to know which calendar an
    existing event lives on. A query requires neither. When an axis is left
    unresolved and not required, the surviving keys are returned as a list.
    """
    cals = _calendars()
    if not cals:
        return None, "unconfigured"
    if len(cals) == 1:
        return next(iter(cals)), None

    candidates = list(cals)
    if name_or_none and name_or_none.strip():
        needle = name_or_none.strip().lower()
        matched = {c for c in {cals[k]["child"] for k in candidates} if _names_match(needle, c)}
        if not matched:
            return None, "unknown"
        if len(matched) > 1:
            return None, "ambiguous"
        candidates = [k for k in candidates if cals[k]["child"] in matched]
    elif len({cals[k]["child"] for k in candidates}) > 1:
        if require_child:
            return None, "ambiguous"
        return None, None  # every configured calendar

    if category:
        candidates = [k for k in candidates if cals[k]["category"] == category]
        if not candidates:
            return None, "unknown"

    if len(candidates) == 1:
        return candidates[0], None
    if require_category:
        return None, "ambiguous_category"
    return candidates, None


def _calendar_id(calendar_key=None):
    cals = _calendars()
    if calendar_key and calendar_key in cals:
        return cals[calendar_key]["id"]
    if len(cals) == 1:
        return next(iter(cals.values()))["id"]
    if not cals:
        return os.environ["CALENDAR_ID"]  # preserves the original clear KeyError if truly unconfigured
    raise ValueError(f"calendar_key required: {len(cals)} calendars configured, none specified")


def _now():
    return datetime.datetime.now(TIMEZONE)


def event_start_date(event):
    """Date of an event, whether it's timed (dateTime) or all-day (date)."""
    start = event.get("start", {})
    if start.get("date"):
        return datetime.date.fromisoformat(start["date"])
    if start.get("dateTime"):
        return datetime.datetime.fromisoformat(start["dateTime"]).date()
    return None


def _list(time_min, time_max, calendar_key=None, max_results=250):
    result = (
        _service()
        .events()
        .list(
            calendarId=_calendar_id(calendar_key),
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def list_upcoming_events(calendar_key=None, n=10):
    now = _now()
    return _list(now, now + datetime.timedelta(days=SEARCH_DAYS_AHEAD), calendar_key, max_results=n)


def list_events_on(date, calendar_key=None):
    return list_events_range(date, date, calendar_key)


def list_events_range(start_date, end_date, calendar_key=None):
    start = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=TIMEZONE)
    end = datetime.datetime.combine(end_date, datetime.time.min, tzinfo=TIMEZONE) + datetime.timedelta(days=1)
    return _list(start, end, calendar_key)


def _event_sort_key(event):
    start = event.get("start", {})
    if start.get("dateTime"):
        return datetime.datetime.fromisoformat(start["dateTime"])
    if start.get("date"):
        return datetime.datetime.combine(datetime.date.fromisoformat(start["date"]), datetime.time.min, tzinfo=TIMEZONE)
    return datetime.datetime.max.replace(tzinfo=TIMEZONE)


def _selected_calendars(keys=None):
    cals = _calendars()
    if keys is None:
        return cals.items()
    return [(k, cals[k]) for k in keys if k in cals]


def list_events_range_all(start_date, end_date, keys=None):
    """Query flow, unresolved child (2+ calendars, none named): every
    configured calendar, each event tagged with which calendar it came from -
    matches CLAUDE.md Flow 4's 'summarises both calendars' example. Pass
    keys to narrow to a subset (e.g. one child's school/enrichment/others).

    Yields (calendar_key, event), not (display name, event). The key is the
    thing callers can actually compute from - child, category, an icon - and
    the display name is derivable from it, while the reverse needs parsing a
    string built for humans. Deferred twice as "task 25"; done here because a
    readable /upcoming needs the category, and the clash detector needs the
    same shape to ever run on query results for free."""
    combined = []
    for key, info in _selected_calendars(keys):
        for event in list_events_range(start_date, end_date, key):
            combined.append((key, event))
    combined.sort(key=lambda pair: _event_sort_key(pair[1]))
    return combined


def list_upcoming_events_all(n_per_calendar=10, keys=None):
    combined = []
    for key, info in _selected_calendars(keys):
        for event in list_upcoming_events(key, n=n_per_calendar):
            combined.append((key, event))
    combined.sort(key=lambda pair: _event_sort_key(pair[1]))
    return combined


def find_conflicts_for_child(calendar_key, start_dt, end_dt):
    """Conflicts across every calendar belonging to the same child as
    calendar_key. A child's school, enrichment and 'others' events live in
    separate calendars (ADR-004) but still compete for the same child's
    time, so checking only the calendar being written to would miss real
    clashes. Yields (calendar_key, event)."""
    cals = _calendars()
    entry = cals.get(calendar_key)
    keys = calendar_keys_for_child(entry["child"]) if entry else [calendar_key]
    for key in keys:
        for event in find_conflicts(start_dt, end_dt, key):
            yield key, event


def _overlaps(event, start_dt, end_dt) -> bool:
    existing_start = event["start"].get("dateTime")
    existing_end = event["end"].get("dateTime")
    if not existing_start or not existing_end:
        return False  # all-day events don't block timed ones
    return (datetime.datetime.fromisoformat(existing_start) < end_dt
            and datetime.datetime.fromisoformat(existing_end) > start_dt)


def find_conflicts(start_dt, end_dt, calendar_key=None):
    for event in list_events_on(start_dt.date(), calendar_key):
        if _overlaps(event, start_dt, end_dt):
            yield event


def find_conflicts_for_child_over(calendar_key, occurrences):
    """Conflicts for several proposed slots at once, across every calendar
    belonging to the same child. Fetches each calendar ONCE over the whole
    span and matches in memory: a term of weekly classes checked date-by-date
    would be ~90 API calls, which is slow enough to time out a Telegram
    handler. Returns {index into occurrences: [clashing events]}, containing
    only the indexes that actually clash."""
    timed = [(i, o) for i, o in enumerate(occurrences) if not o.get("all_day") and o.get("start")]
    if not timed:
        return {}

    span_start = min(o["start"] for _, o in timed).date()
    span_end = max(o["end"] for _, o in timed).date()
    entry = _calendars().get(calendar_key)
    keys = calendar_keys_for_child(entry["child"]) if entry else [calendar_key]

    existing = []
    for key in keys:
        existing.extend(list_events_range(span_start, span_end, key))

    conflicts = {}
    for index, occurrence in timed:
        clashes = [e for e in existing if _overlaps(e, occurrence["start"], occurrence["end"])]
        if clashes:
            conflicts[index] = clashes
    return conflicts


# Nearby candidate offsets for trade-off ranking (Flow 2, scoped to same-
# calendar availability - no per-family-member availability data exists in
# this POC). Closest-to-original first.
_ALTERNATIVE_OFFSETS_MINUTES = [60, -60, 120, -120, 24 * 60]
_EARLIEST_REASONABLE_HOUR = 7
_LATEST_REASONABLE_HOUR = 21


def suggest_alternatives(start_dt, end_dt, calendar_key=None, max_options=2):
    """Given a slot that conflicts, find up to max_options nearby free
    alternatives (same duration), closest-first. Returns a list of
    (start_dt, end_dt) tuples - may be shorter than max_options, even empty,
    if nothing nearby is free."""
    duration = end_dt - start_dt
    found = []
    for offset in _ALTERNATIVE_OFFSETS_MINUTES:
        candidate_start = start_dt + datetime.timedelta(minutes=offset)
        candidate_end = candidate_start + duration
        if not (_EARLIEST_REASONABLE_HOUR <= candidate_start.hour < _LATEST_REASONABLE_HOUR):
            continue
        if not list(find_conflicts_for_child(calendar_key, candidate_start, candidate_end)):
            found.append((candidate_start, candidate_end))
        if len(found) >= max_options:
            break
    return found


def _title_matches(needle, summary):
    n = needle.strip().lower()
    s = (summary or "").lower()
    if not n:
        return False
    if n in s:
        return True
    tokens = [t for t in re.split(r"\W+", n) if t]
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in s)
    # match if all words are present, or a clear majority (handles extra filler words)
    return hits == len(tokens) or hits >= max(1, round(len(tokens) * 0.6))


def title_matches(search_text, summary) -> bool:
    """Public form of the fuzzy title match, for callers narrowing a candidate
    list they already have rather than searching a calendar."""
    return _title_matches(search_text, summary)


def find_events_by_title(search_text, prefer_date=None, calendar_key=None):
    """Find events whose title matches. Never hard-filters by date - a date only
    narrows the result when it actually helps disambiguate, so a stray/guessed
    date can never hide a real event."""
    now = _now()
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = _list(time_min, time_min + datetime.timedelta(days=SEARCH_DAYS_AHEAD), calendar_key)

    matches = [e for e in events if _title_matches(search_text, e.get("summary", ""))]
    if prefer_date:
        same_day = [e for e in matches if event_start_date(e) == prefer_date]
        if same_day:
            return same_day
    return matches


def list_events_range_across(start_date, end_date, keys=None):
    """Every event in a date range across several calendars, as
    (calendar_key, event) pairs - the same shape as
    find_events_by_title_across, so delete/amend can use either as their
    candidate source. Used when the user referred to an event by WHEN rather
    than by name ("cancel Child_1's appointment today")."""
    if keys is None:
        keys = [None]
    found = []
    for key in keys:
        for event in list_events_range(start_date, end_date, key):
            found.append((key, event))
    found.sort(key=lambda pair: _event_sort_key(pair[1]))
    return found


def find_events_by_title_across(search_text, prefer_date=None, keys=None):
    """find_events_by_title over several calendars at once, returning
    (calendar_key, event) pairs so each match carries the calendar it was
    found on. Lets delete/amend search all of a child's categories without
    making the user say which one the event is on."""
    if keys is None:
        keys = [None]
    matches = []
    for key in keys:
        for event in find_events_by_title(search_text, prefer_date, key):
            matches.append((key, event))
    return matches


# ----- reminders -----
#
# Google expresses every reminder as "N minutes before the event STARTS", so
# a rule about the end time has to be converted using the event's duration.
MINUTES_BEFORE_END = 30      # the "leave now to collect her" nudge
DAY_BEFORE_MINUTES = 24 * 60
# All-day events are anchored to midnight, so a literal 1440 would buzz at
# 00:00 the night before. 360 = 18:00 the previous evening instead.
ALL_DAY_EVENING_BEFORE_MINUTES = 6 * 60


def _reminder_minutes(start_dt=None, end_dt=None, all_day=False) -> list:
    """Minutes-before-start for each reminder we want on an event."""
    if all_day:
        return [ALL_DAY_EVENING_BEFORE_MINUTES]
    duration = int((end_dt - start_dt).total_seconds() // 60)
    # 30 minutes before the end lands *inside* any event longer than 30
    # minutes, which Google cannot express (offsets can't be negative). Those
    # fall back to firing at the start - the earliest point still useful.
    before_end = max(0, MINUTES_BEFORE_END - duration)
    return sorted({before_end, DAY_BEFORE_MINUTES}, reverse=True)


def _reminders(start_dt=None, end_dt=None, all_day=False) -> dict:
    return {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": m}
            for m in _reminder_minutes(start_dt, end_dt, all_day)
        ],
    }


def create_event(title, start_dt=None, end_dt=None, all_day_date=None, location=None, notes=None, calendar_key=None):
    if all_day_date is not None:
        body = {
            "summary": title,
            "start": {"date": all_day_date.isoformat()},
            "end": {"date": (all_day_date + datetime.timedelta(days=1)).isoformat()},
            "reminders": _reminders(all_day=True),
        }
    else:
        body = {
            "summary": title,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(TIMEZONE)},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": str(TIMEZONE)},
            "reminders": _reminders(start_dt, end_dt),
        }
    if location:
        body["location"] = location
    if notes:
        body["description"] = notes
    return _service().events().insert(calendarId=_calendar_id(calendar_key), body=body).execute()


def get_event(event_id, calendar_key=None):
    return _service().events().get(calendarId=_calendar_id(calendar_key), eventId=event_id).execute()


def delete_event(event_id, calendar_key=None):
    _service().events().delete(calendarId=_calendar_id(calendar_key), eventId=event_id).execute()


def patch_event(event_id, title=None, start_dt=None, end_dt=None, all_day_date=None, location=None, notes=None, calendar_key=None):
    body = {}
    if title is not None:
        body["summary"] = title
    if all_day_date is not None:
        body["start"] = {"date": all_day_date.isoformat()}
        body["end"] = {"date": (all_day_date + datetime.timedelta(days=1)).isoformat()}
        body["reminders"] = _reminders(all_day=True)
    else:
        if start_dt is not None:
            body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": str(TIMEZONE)}
        if end_dt is not None:
            body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": str(TIMEZONE)}
        if start_dt is not None and end_dt is not None:
            # Recomputed, not left alone: the before-end reminder is stored as
            # an offset from the start, so a changed duration would quietly
            # leave it pointing at the wrong moment.
            body["reminders"] = _reminders(start_dt, end_dt)
    if location is not None:
        body["location"] = location
    if notes is not None:
        body["description"] = notes
    return _service().events().patch(calendarId=_calendar_id(calendar_key), eventId=event_id, body=body).execute()
