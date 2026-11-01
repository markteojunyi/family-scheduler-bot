# Family Schedule Manager

## Project Purpose
A multi-agent system that manages two daughters' school and enrichment schedules.
The Telegram bot is the single interface for the whole family. Google Calendar is
the source of truth. The system handles schedule ingestion, conflict detection,
trade-off suggestions, and enrichment centre email correspondence — all with a
human confirming every write action.

---

## Architecture: Three Agents

### 1. Interface Agent (always-on, Telegram-facing)
- Receives ALL inbound messages and images from family members
- Identifies sender by Telegram user ID → maps to permission level
- Routes: image → Ingest flow | text query → Query Handler | reply to confirm prompt → Orchestrator
- Sends all outbound messages (confirmation cards, query responses, trade-off options)
- Parents are silently blocked from write-triggering paths — no error, just read-only responses

### 2. Orchestrator Agent (event-triggered)
- Triggered by Ingest Agent (new event detected) or Interface Agent (user initiated change)
- Checks Google Calendar for conflicts on proposed date/time for the relevant child
- No conflict → sends Telegram confirmation card with ✅ Confirm / ❌ Cancel inline keyboard
- Conflict detected → ranks options by family availability, sends trade-off summary
- On user confirmation → writes to Google Calendar
- For enrichment reschedules → drafts reply email → sends draft to admin via Telegram for review → on approval sends via Gmail API → stores Gmail thread ID in tracked_threads

### 3. Gmail Watcher (scheduled, every 5 minutes)
- Only scans tracked Gmail threads (threads the bot initiated), not the full inbox
- Parses reply into one of three cases:
  - CONFIRMED: enrichment centre accepts proposed slot → send Telegram confirm card → on approval update Calendar
  - COUNTER-PROPOSAL: centre offers a different slot → re-run conflict check → send new trade-off options
  - REJECTION: no slots available → notify admin via Telegram, await instruction
- Marks thread as resolved once Calendar is updated

---

## Key Flows

### Flow 1: School Schedule Update (Screenshot)
```
User sends screenshot to Telegram bot
  → Interface Agent detects image, routes to Ingest
  → Claude vision API extracts: child, event name, date, time, location, recurring/one-off
  → Orchestrator checks relevant child's Google Calendar for conflicts
  → No conflict: Telegram card "Add [event] on [date] at [time] for [child]? ✅ ❌"
  → Conflict: Telegram message listing conflict + options
  → User confirms → Google Calendar write
```

### Flow 2: Enrichment Reschedule (Inbound)
```
User forwards enrichment centre message to Telegram bot
  → Interface Agent routes to Orchestrator
  → Orchestrator checks both daughters' calendars across candidate slots
  → Checks known availability of each family member
  → Returns ranked options:
      Option A — Thu 5pm: clear for everyone ✅
      Option B — Sat 10am: conflicts with Daughter 1 swimming ⚠️
      Option C — Wed 4pm: admin unavailable (work calendar) ⚠️
  → Admin selects option via Telegram inline keyboard
  → Orchestrator drafts reply email to enrichment centre
  → Sends draft to admin via Telegram for review
  → Admin approves → Gmail API sends email → thread ID stored
```

### Flow 3: Enrichment Centre Email Reply
```
Gmail Watcher polls tracked threads (every 5 min)
  → Detects reply from enrichment centre
  → Parses reply type: CONFIRMED / COUNTER-PROPOSAL / REJECTION
  → CONFIRMED: sends Telegram card → admin confirms → Calendar updated → thread closed
  → COUNTER-PROPOSAL: re-runs Flow 2 conflict check with new proposed slot
  → REJECTION: notifies admin via Telegram → awaits next instruction
```

### Flow 4: Family Query (Read-Only)
```
Any family member sends natural language query to Telegram bot
  → Interface Agent identifies sender → any role can query
  → Query Handler reads relevant Google Calendar(s)
  → Returns natural language response
  Examples:
    "Who is picking up Child_1 this Friday?" → reads Pickup field on Friday events
    "Does [child] have anything Saturday morning?" → checks Sat AM on child's calendar
    "What's on next week?" → summarises both calendars for the week
```

---

## Human-in-the-Loop Rules (NEVER VIOLATE THESE)
- **NEVER write to Google Calendar without an explicit Telegram confirmation from an authorised user**
- **NEVER send an email without showing the full draft to the admin first and receiving approval**
- **NEVER auto-resolve conflicts** — always surface options and wait for human choice
- **NEVER assume a confirmation** — inline keyboard ✅ must be tapped, not inferred from context
- If a user sends a free-text "yes" after a confirmation card, treat as confirmation only if it directly follows the card in the same thread

---

## Permission Model

```python
# permissions.py — roles are resolved from env vars at import time
PERMISSIONS = {
    "ADMIN_TELEGRAM_ID":   "admin",   # read, write, confirm, email approve, audit
    "WIFE_TELEGRAM_ID":    "editor",  # read, write, confirm — full calendar control
    "MOM_TELEGRAM_ID":     "reader",  # read-only queries
    "DAD_TELEGRAM_ID":     "reader",  # read-only queries
}

ROLES = {
    "admin":  ["read", "write", "confirm", "email_approve", "audit"],
    "editor": ["read", "write", "confirm"],
    "reader": ["read"],
}
```

**Capability meanings:** `write` = may *propose* a change (send a screenshot,
ask to add/move/delete); `confirm` = may tap ✅ to commit it. Both are needed
to complete a change end-to-end. `email_approve` and `audit` stay admin-only,
so the editor role cannot send mail on the family's behalf or read the
who-changed-what log.

**Decided 2026-07-28:** the editor role holds `write` as well as `confirm` —
an equal partner managing the children's schedules, not a second approver.
Roles remain per-person, so revoking write access later is a one-line change.

Replace placeholder strings with actual Telegram user IDs before first run.
Get a user's Telegram ID by messaging @userinfobot on Telegram.

---

## Google Calendar Structure

Six calendars — one per child, per category (ADR-009):

| Calendar | Parent_2 | Grandparents |
|---|---|---|
| `child_1-school`, `child_2-school` | read + edit | **read-only** |
| `child_1-enrichment`, `child_2-enrichment` | read + edit | no access |
| `child_1-others`, `child_2-others` | read + edit | no access |

Sharing is enforced by Google Calendar ACLs, not by `permissions.py` — the
grandparents' school-only access is exactly why the per-child calendars are
split by category, since Google grants access per calendar and never per
event.

**The category drives which calendar an event is written to.** When the
category isn't clear from the message, the bot asks before writing —
`others` is a choice a human makes deliberately (medical, birthdays, family
logistics), never a fallback for events the parser couldn't classify. Filing
a private appointment under `school` would expose it to the grandparents.

Category-inference vocabulary lives in `_CATEGORY_HINTS` (`event_parser.py`)
— tune it there as the family's real wording settles.

### Places and travel time (ADR-011)

`places.py` holds the family's real places and the minutes between them, in
the same "one obvious place to edit" spirit as `PICKUP_ROSTER`. An event
resolves to a place via `location` > `Centre:` > a known alias in the activity
(enrichment only) > the child's school, from the calendar it lives on.

**Seeded 2026-08-16** with the family's nine real places and all 36 journeys
between them: the two schools, `swimming` and `centre_1` (shared), three ballet
studios, `home` and `hospital_1`. School events resolve from their calendar; the rest
resolve by alias, so a **new** centre needs either an entry in `PLACES` or a
`location` on the event — otherwise it resolves to no place and is skipped.

A place may be scoped to one child (`"child": "child_2"`). That is what keeps the
three ballet studios apart: Child_2's bare "Ballet class" means Studio_1, her only
studio, while Child_1's resolves to **nothing**, because she has two told apart
by branch name alone. Nothing is the correct answer to an ambiguous place.

**Every number in the table is a CAR time.** `TRANSPORT` maps each carer to
car or public transport, and public adds 20 minutes — the child travels with
whoever collects her, so the mode follows the roster. Helper_1 is on public
transport and covers Child_1's weekday pickups, so most of Child_1's week is longer
than the table reads. Journeys of 10 min or under are exempt (same building).
Unlisted carers are assumed to be on public transport, erring toward warning.

A tight transition — one child, two events too close together to travel
between — is only ever warned about using **a number a human gave**. An
untimed pair produces a question on the confirm card instead, and the answer
is stored per pair of places and reused. Never fill an unknown journey with a
guessed default.

**Precedence (ADR-011): a deliberate `/travel` edit > `places.py` > a bucket
tapped on a confirm card.** Split by intent, not by storage: a considered
statement must not be overwritten by a hurried one. `db.set_travel_minutes`
enforces it on write as well as on lookup.

The table is edited from Telegram, not the terminal. **Plain words are the
main route** — "school to centre_1 takes 25 minutes", "centre_1 is 20 minutes
away", "centre_2 is a new place" — carried by a `travel` action on the
existing classifier, so there is no second parsing path and no extra model
call. Applied immediately with an ↩️ Undo rather than a confirm card: a travel
time only changes when the bot warns, unlike a calendar write or an email.
`/travel` (list + ✏️ Edit buttons) and `/travel set|forget|place` remain as
the exact fallback. Places added either way live in SQLite and are injected
via `places.set_user_places()`; **`places.py` never imports `db`**, which is
what keeps its matching rules testable without one.

**Classification is the risk, and the unit suite cannot check it** — parser
tests hand `_finalize` pre-baked dicts. "Swimming is at 4pm" is a create;
"swimming is 20 minutes from school" is travel. Paired cases live in
`scripts/smoke_parser.py`; **run it after touching the prompt or the schema.**
An ambiguous place ("school" — there are two) is asked about with buttons,
never resolved by picking whichever sorts first.

Two traps when touching this: the editor must show and accept the raw **car**
time (`places.stored_minutes`), never the transport-adjusted one, or the bus
penalty gets counted twice. And multi-step flows must keep their state in
`callback_data` — `pending_state` is one row per chat, so using it would
clobber an open confirm card.

### Who collects on a Saturday

`PICKUP_ROSTER` (`roster.py`) has one carer per day type. Saturday used to be
Parent_2 alone; **Parent_1 is now available too, and who collects varies week to
week** — so the confirm card for a single-date Saturday event that nobody was
named for **asks** (`roster.carer_choice_needed`, buttons from
`roster.SATURDAY_CARERS`) rather than assuming. The roster entry stays as the
fallback for `/pickups` and a dismissed card. The answer sets
`result["pickup"]`, which flows into the clash check (as a `Pickup:` line on
the proposal), the written description, and the co-parent notification — the
clash detector can only tell "one adult does both children" from "one each"
once a real name is on the event. A carer the message already named
("Grandparent_1's getting her") skips the question. **Not asked** on a multi-date
card (a re-render would drop the per-date annotations) or an amend — those
fall back to the roster.

Carers are free text, so `_dispatch` folds a name onto its canonical
spelling before it reaches a card or the calendar — `roster.canonical_carer`
maps "parent_1"/"PARENT_1"/"parnt_1" → `Parent_1` against `roster.KNOWN_CARERS`, but only on
an exact case-insensitive match or a single-character typo, so a genuinely new
carer ("Other_carer is getting her Thursdays") passes through untouched. This
matters because `roster._same_person` compares exactly — an un-folded typo
would read as a second adult and a real pickup clash would go unwarned. A
name the bot doesn't recognise is shown on the confirm card with a quiet
"new name — tap ✏️ if that's a typo" rather than blocked.

### Event titles and the clock they carry

Titles are `[Child] [Time] [Gate N] [Activity]`, and **the time in the title is
the record** - `/upcoming` reads the clock back out of it rather than off the
event, so a line can never state a different time from the title beside it.
That round trip is why `format_title` and `split_title` must stay exact
inverses: a clock `_CLOCK_RE` cannot parse is left on the front of the
*activity*, and the next amend stamps a second one on top of it.

Which end of the block a stated time anchors, and what the title shows,
depends on the category - the same way the pickup gate does (`gate_for` is
school-only):

| Message | School | Enrichment | Others |
|---|---|---|---|
| "piano 5pm" (one time) | ends 5pm → `5pm Piano` | **starts 5pm** → `5pm Piano` | **starts 5pm** → `5pm Piano` |
| "piano 4 to 5pm" (a range) | `5pm Piano` | **`4-5pm Piano`** | **`4-5pm Piano`** |

**Only a school event reads a lone time as a pickup** - a dismissal time,
when the child is collected - and draws a 15-minute block *ending* then. It is
the one category whose title also carries a gate, for the same reason.
Everywhere else a stated time is when the thing *happens* ("piano at 5",
"dentist at 3"), so the block *begins* then. Per-message wording
(`time_is_start`: "drop off at 8", "from 9am") always wins over the category.
`event_parser.lone_time_defaults_to_start` is the one place that rule lives;
`LONE_TIME_MEANS_START` (`.env`) is only the fallback for an uncategorised
calendar.

**An end time is never invented**: the range title only ever shows a range the
message actually gave, and a lone time only ever shows the one number. A
non-school range title states both ends because duration is what you plan an
afternoon around when a class runs 4 to 5 or an appointment holds 2 to 3 —
the mirror of the lone-time rule above, and school is the exception to both
for the same reason. `event_parser.wants_range_title` is where it lives.

`/upcoming` leads each line with the *start* of a range so its time column
stays aligned; only the title carries both ends.

On an **amend** the category is not known at parse time - `_finalize` discards
the model's guess and searches all of the child's calendars - so both the
range decision *and* which end a lone new time anchors are made in
`main._anchor_for`, after the event has been found. Do not move either into
the parser: `calendar_key` is a list there and it would silently do nothing.
`_apply_changes` resolves the anchor once and hands it to `_retitle`, so the
block placement and the rebuilt title can never disagree.

### Changing a whole run of events

"Change Child_2's ballet pickup to Parent_1 **for the next 4 months**" applies one
change to every matching event in a window, not just the next one. The model
sets `apply_to_series` and reports the horizon phrase verbatim in
`series_horizon` ("4 months", "until 20 Dec") - it never computes the date.
`event_parser._series_window` does that (weeks/months arithmetic or an until-
date), clamped to `MAX_SERIES_DAYS`. A horizon it can't read, or a series
amend that named no event, is **asked about** - the reply is glued to the
original request and re-parsed.

`main._present_amend_series` lists every affected date on one card;
`_amend_many` patches them all on one ✅, re-titling each event by its own
category and not aborting on the first failure (mirrors `_create_many`). The
series card has **no per-date clash warnings** yet - the human sees the full
list instead, and `/pickups` still catches a resulting clash after the fact.
Recurring *delete* and category changes are not built.

### Availability questions

"Is Child_1 free Saturday morning?" / "does Child_2 have anything Friday afternoon?"
is a **yes/no**, not a listing (`asks_free` on the query schema). `part_of_day`
(morning / afternoon / evening) and `at_time` narrow it to a clock span
(`event_parser._query_time_window`, `_DAY_PARTS`); `_handle_query` filters the
day's events to that span and answers "✅ Child_1 is free …" or "⚠️ Child_1 isn't
free … —" with what overlaps. An all-day event always counts as overlapping —
the safe answer to "free then?" errs toward *check*.

### Resuming a stalled parse

When a parse stalls on a missing detail, the `needs_clarification` result now
carries the model's `raw` dict forward. A **typed** answer ("tomorrow 4pm",
"the piano one") is resumed against it by `event_parser.resume_with_answer`:
the model sees only the answer plus the locked fields, never the original
wording again, so it cannot silently re-classify the request (the bug behind
PROGRESS §20/§31) and there is no glue-punctuation seam. The `✏️ Change
something` path still uses the `CORRECTION:` glue — that one *is* re-opening a
decision, deliberately. Button resumes (`resume_with_child` /
`resume_with_category`) are still pure Python, no model call.

### Event Schema
Every calendar event must include these fields in the description:
```
Pickup: [Parent_1 / Parent_2 / Grandparent_1 / Grandparent_2 / Self]
Type: [School / Enrichment / Medical / Other]
Centre: [Name of school or enrichment centre]
Notes: [Any additional info]
```

---

## Tech Stack
- **Language:** Python 3.11+
- **Telegram:** python-telegram-bot (v20+, async)
- **Google Calendar:** google-api-python-client + google-auth-oauthlib
- **Gmail:** gmail API via google-api-python-client
- **AI:** anthropic SDK (claude-sonnet-4-6) — vision for screenshots, reasoning for trade-offs
- **Scheduling:** APScheduler (Gmail Watcher every 5 min)
- **Storage:** SQLite (tracked_threads, pending confirmations) via sqlite3
- **Config:** python-dotenv

---

## Environment Variables
```
TELEGRAM_BOT_TOKEN=
ANTHROPIC_API_KEY=
GOOGLE_CREDENTIALS_PATH=credentials.json
GMAIL_USER=your@gmail.com
DAUGHTER_1_CALENDAR_ID=
DAUGHTER_2_CALENDAR_ID=
ADMIN_TELEGRAM_ID=
WIFE_TELEGRAM_ID=
MOM_TELEGRAM_ID=
DAD_TELEGRAM_ID=
```

---

## Documentation map

Five living documents; anything else is history. **`PROGRESS.md` is the entry
point** — current state, how to operate the deployed bot, and the backlog.

| File | What it holds |
|---|---|
| `PROGRESS.md` | **Start here.** State, operations, session log, backlog |
| `ADR.md` | Architectural decisions and their reasoning |
| `THREAT_MODEL.md` | Attack surfaces, defences, and why no guardrail library (§4). **READ §6 BEFORE the Gmail phase.** |
| `learningAboutFamilyScheduler.md` | Teaching log — the concepts behind each fix |
| `docs/archive-2026-07.md` | July history + the retired readiness audit and go-live checklist. Not instructions. |

## Project Structure
```
family-scheduler/
├── CLAUDE.md
├── PROGRESS.md
├── ADR.md
├── THREAT_MODEL.md
├── learningAboutFamilyScheduler.md
├── docs/
│   └── archive-2026-07.md      # retired docs + July session history
├── .env
├── .env.example
├── requirements.txt
├── requirements.lock.txt       # pinned runtime versions - install THIS on a server
├── requirements-dev.txt        # pytest only; never installed in production
├── main.py                     # Entry point, starts bot + scheduler
├── agents/
│   ├── interface_agent.py      # Telegram handlers, permission routing
│   ├── orchestrator_agent.py   # Conflict detection, trade-offs, email drafts
│   └── gmail_watcher.py        # Polls tracked threads, routes replies
├── tools/
│   ├── calendar_tools.py       # Google Calendar read/write helpers
│   ├── gmail_tools.py          # Gmail send, thread tracking, reply parsing
│   └── vision_tools.py         # Screenshot → structured event via Claude API
├── models/
│   └── schemas.py              # Pydantic models: Event, ScheduleChange, TradeOffOption, TrackedThread
├── config/
│   └── permissions.py          # Telegram ID → role mapping
├── db/
│   └── database.py             # SQLite setup, tracked_threads table
└── tests/
    ├── test_vision.py
    ├── test_calendar.py
    └── test_permissions.py
```

---

## Build Sequence — Follow This Order

### Phase 1 (Start here)
- Telegram bot skeleton with permission routing
- Google Calendar read queries (both calendars)
- Natural language query handler (Flow 4)
- **Deliverable:** Parents can query children's calendars via Telegram

### Phase 2
- Claude vision integration (screenshot → structured event)
- Orchestrator: conflict detection
- Telegram confirmation cards with inline keyboard
- Google Calendar write (post-confirmation only)
- **Deliverable:** Admin can update calendars via screenshot, with confirmation

### Phase 3
- Gmail API integration
- Email draft generation via Claude
- Telegram draft review + approval flow
- Thread ID storage in SQLite
- **Deliverable:** Bot can draft and send enrichment reschedule emails

### Phase 4
- Gmail Watcher (APScheduler, every 5 min)
- Reply parsing: CONFIRMED / COUNTER-PROPOSAL / REJECTION
- Loop back into conflict detection for counter-proposals
- **Deliverable:** Full closed loop — email reply triggers calendar update

### Phase 5
- Second daughter's calendar fully integrated
- Full regression test across all flows
- **Deliverable:** Production-ready for both children

---

### When something fails

`errors.diagnose(exc)` (`errors.py`) turns any exception into what to say.
**Never reply with a fixed "something went wrong" string** - that was the
reply to everything until 2026-09-25, when an empty OpenAI account made every
message fail while the family was told to try again. Each diagnosis says
whether retrying helps; the admin gets the technical detail and the fix,
everyone else plain words, and failures that need him (empty account, revoked
token, full disk, missing calendar, unknown bugs) alert him - once an hour per
kind. Write and send paths word it with `errors.failure_reply`: "didn't save"
only when the error **proves** nothing happened, otherwise "may not have
saved - check first", because retrying a write that landed makes a duplicate.

A new failure mode gets a branch in `errors.py` and a test in
`tests/test_errors.py` built from the **real** exception class, not a mock.

## What NOT to Do
- Do not poll the school app — it has no API. Screenshot ingestion is the only ingest path for school changes
- Do not use WhatsApp — use Telegram only
- Do not write to Calendar anywhere except in orchestrator_agent.py after confirmation
- Do not send email anywhere except in orchestrator_agent.py after admin approval
- Do not store credentials in code — always use .env
- Do not process messages from unknown Telegram IDs — log and ignore silently

### Ranking candidate times (Flow 2)

Give several slots and the bot ranks them: `slot_options` on the create schema
carries alternatives to choose ONE of, and is the deliberate opposite of
`dates`, which adds the event on ALL of them. Reading a choice as a list
creates real classes nobody asked for, so the smoke script checks both.

Order is by how hard the problem is to fix (`roster.CLASH_SEVERITY`):
double-booked > nobody rostered > carer needed twice > too tight to travel >
clear; ties break on fewest problems, then earliest.

**Ranking writes nothing.** Picking a row goes to the ordinary confirm card,
so the committing tap stays where it always was. When adding to that path,
remember the chosen slot must be **retitled** — titles carry the time — and
must carry its `occurrences` through, or the confirm card loses its clash
warning.
