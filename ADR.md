# Architectural Decision Records (ADR)

These records explain why each major decision was made. They are not instructions
for Claude Code — see CLAUDE.md for that. These exist so future contributors
(or your future self) understand the reasoning behind the architecture.

---

## ADR-001: Telegram over WhatsApp

**Decision:** Use Telegram as the family messaging interface.

**Why not WhatsApp:**
The official WhatsApp Business API requires Meta Business account verification,
takes days to weeks to approve, and third-party providers that simplify the
integration (WATI, Twilio) cost $49–99/month. Unofficial bridges (Baileys,
whatsmeow) violate WhatsApp's Terms of Service and risk the phone number being
permanently banned — unacceptable for a tool the family depends on daily.
WhatsApp does not natively support bot creation without going through this process.

**Why Telegram:**
Telegram bot creation takes 5 minutes via @BotFather with no approval process,
no cost, and no per-message fees. It natively supports inline keyboards (perfect
for ✅ Confirm / ❌ Cancel flows), image ingestion, group chats, and user ID-based
permission systems. It is widely used in Singapore. The one trade-off — family
members need to install Telegram — is a one-time friction that is worth the
long-term reliability.

---

## ADR-002: Multi-Agent over Single Agent

**Decision:** Three specialised agents (Interface, Orchestrator, Gmail Watcher)
rather than one agent doing everything.

**Why not a single agent:**
Three fundamentally different loops exist in this system:
- Monitoring (time-triggered, runs every 5 min, low urgency)
- Decision support (event-triggered, minutes-to-hours latency)
- Query/response (human-triggered, must respond in seconds)

Running all three in one agent means the slow monitoring loop competes with the
fast query loop. A single agent trying to monitor for Gmail replies AND stay
responsive to Telegram queries simultaneously creates scheduling conflicts within
the agent itself. The context also becomes polluted — one agent holding school
calendar state, Gmail thread state, and family query context at the same time
is harder to debug and maintain.

**Why three agents:**
Each agent has a single clear responsibility, a single trigger type, and a single
output destination. When the school switches apps (inevitable), only the Interface
Agent needs updating. When Gmail API changes, only the Watcher is affected. Each
can be tested and deployed independently.

---

## ADR-003: Two Google Calendars (One Per Child)

> **Extended by [ADR-009](#adr-009-per-category-calendars-school--enrichment--others).**
> The per-child split below still holds; each child's calendar was later split
> again by category so sharing can differ within a child.

**Decision:** Maintain a separate Google Calendar for each daughter rather than
one shared family calendar.

**Why not one calendar:**
A single calendar requires the agent to filter every query by child name, which
adds complexity and room for error. Conflict detection becomes ambiguous — if
both daughters have events, determining which conflict belongs to which child
requires extra logic. Sharing permissions also becomes less flexible: if one
grandparent only helps with one child, there is no clean way to model that with
a single calendar.

**Why two calendars:**
Querying is clean and unambiguous ("what does Child_1 have?" queries Child_1's calendar
directly). Conflict detection is per-child. Sharing is independently controllable
per child. As the second daughter's schedule grows more complex, her calendar
scales independently without affecting the first daughter's data.

---

## ADR-004: Screenshot Ingestion for School Updates

**Decision:** School schedule changes are ingested via screenshot sent to the
Telegram bot, parsed by Claude's vision API.

**Why not a direct app integration:**
The school app has no public API, no ICS/webcal export, and no webhook capability.
A screen-scraping approach was considered but is fragile (app UI changes break
the scraper) and likely violates the app's terms of service.

**Why screenshot ingestion:**
The parent already sees the notification on their phone. Sending a screenshot to
Telegram is one extra tap. Claude's multimodal vision capability can reliably
extract structured data (child, event, date, time, location) from a screenshot
without any additional OCR tooling. The approach is robust to app UI changes
because it reads the content semantically, not by pixel position. The trade-off
is that this flow requires a human to initiate it — there is no automatic
detection of new school notifications. This is acceptable because school
notifications are infrequent and the parent is already reading them anyway.

---

## ADR-005: Human-in-the-Loop for All Writes

**Decision:** No write to Google Calendar or send of any email without explicit
human confirmation via Telegram.

**Why not auto-write for simple cases:**
Even "simple" cases (no conflict detected, one clear slot) can have context the
system doesn't have — a family trip, a birthday, a known but unrecorded
commitment. An auto-write that turns out to be wrong is harder to undo than a
confirmation step that takes one tap. The cost of the confirmation step is low;
the cost of a wrong calendar write (missed pickup, double-booked child) is high.

**Why not auto-send for confirmed enrichment reschedules:**
Email sent to an enrichment centre on behalf of the family represents a real
commitment. The draft review step ensures the tone is right, the details are
correct, and the admin is aware the email was sent. One additional approval tap
is a small price for this level of assurance.

---

## ADR-006: Gmail Watcher as a Separate Scheduled Process

**Decision:** Gmail reply monitoring runs as a separate APScheduler job every
5 minutes, not as part of the main Telegram bot process.

**Why not check Gmail inside the bot:**
The Telegram bot is event-driven — it reacts to incoming messages. Gmail replies
are time-driven — they arrive asynchronously and need to be polled. Mixing a
polling loop into an event-driven process creates timing complexity and makes
the bot's response latency unpredictable.

**Why only track specific threads:**
Scanning the full Gmail inbox every 5 minutes is noisy, slow, and risks
accidentally processing unrelated emails. Storing the thread ID of every email
the bot sends means the watcher only looks at threads it knows about. This is
precise, fast, and auditable.

**Why 5-minute polling interval:**
Enrichment centres are not expected to reply within seconds. A 5-minute interval
is responsive enough (family will see the reply within 5 minutes of it arriving)
without hammering the Gmail API or consuming unnecessary compute. This can be
adjusted if needed.

---

## ADR-007: SQLite for Thread Tracking and Pending Confirmations

**Decision:** Use SQLite (via Python's built-in sqlite3) to persist tracked Gmail
threads and pending Telegram confirmations.

**Why not in-memory storage:**
The bot will restart periodically (deployments, crashes, server restarts). In-memory
storage means all tracked threads and pending confirmation states are lost on
restart. A user who sent a screenshot but hasn't confirmed yet would lose their
pending action silently.

**Why not a full database (Postgres, MySQL):**
This is a family tool with a handful of users and a very small data volume. SQLite
runs in-process with no separate server, no connection management, and no
infrastructure overhead. It is more than sufficient for this use case and keeps
the deployment simple.

---

## ADR-008: Claude claude-sonnet-4-6 for All AI Calls

> ⚠️ **Superseded by ADR-010.** The build uses OpenAI `gpt-4.1-mini`. The
> reasoning below is kept because the *shape* of the argument survived the
> change of vendor — and because the part it got wrong is instructive.

**Decision:** Use claude-sonnet-4-6 for screenshot parsing, conflict reasoning,
trade-off generation, and email drafting.

**Why Sonnet over Haiku:**
The tasks in this system (vision parsing of screenshots, nuanced trade-off ranking,
professional email drafting) require a capable model. Haiku is faster and cheaper
but less reliable on multi-step reasoning and vision tasks. Given the low volume
of calls (this is a family tool, not a high-throughput API), the cost difference
is negligible.

**Why Sonnet over Opus:**
Opus is more capable but significantly more expensive and slower. The tasks here
do not require frontier-level reasoning — they are well-defined, structured, and
bounded. Sonnet is the right balance of capability and cost for this use case.

---

## ADR-009: Per-Category Calendars (School / Enrichment / Others)

**Decision:** Split each child's calendar three ways by category, giving six
calendars in total: `child_1-school`, `child_1-enrichment`, `child_1-others`,
`child_2-school`, `child_2-enrichment`, `child_2-others`. Extends ADR-003, which
established the per-child split.

**Why:**
Google Calendar sharing is granted per calendar, never per event. The family
needs the grandparents to see school events — the ones they actually help with
— without seeing enrichment schedules or personal appointments. That access
boundary cuts *within* a child, so no amount of per-event tagging on a single
per-child calendar can express it. Splitting by category is the only way to
make Google's own sharing model enforce the rule:

| Calendar | Parent_2 | Grandparents |
|---|---|---|
| `child_1-school`, `child_2-school` | read + edit | read-only |
| `child_1-enrichment`, `child_2-enrichment` | read + edit | no access |
| `child_1-others`, `child_2-others` | read + edit | no access |

This is enforced entirely by Google Calendar ACLs. `permissions.py` stays
purely Telegram-ID → role and knows nothing about calendars, so the two
systems remain independent.

**Why "others" is never a silent default:**
The bot asks which calendar to use whenever the category is unclear, rather
than guessing. `others` is a destination a human picks deliberately (medical,
birthdays, family logistics), not a bucket for events the parser could not
classify. A wrong guess here is not a tidiness problem — filing a private
appointment under `school` exposes it to the grandparents. This follows the
same principle as CLAUDE.md's rule against auto-resolving conflicts: surface
the choice, wait for the human.

**Consequences:**
- Conflict detection must span all of a child's calendars
  (`find_conflicts_for_child`), since a school event and an enrichment class
  sit on different calendars but still compete for the same child's time.
- Delete and amend search across a child's calendars rather than asking which
  one an event lives on — the user shouldn't need to know where the bot filed
  something in order to change it.
- Category inference vocabulary lives in one place (`_CATEGORY_HINTS` in
  `event_parser.py`) so it can be tuned as the family's real wording emerges.
- ADR-003's flat per-child config and the original single `CALENDAR_ID` both
  still work unchanged; categories are opt-in per child.

---

## ADR-010: OpenAI `gpt-4.1-mini`, and *not* a reasoning model

**Supersedes ADR-008.** Decided 2026-08-04, after measurement.

**Decision:** Use OpenAI `gpt-4.1-mini` for both text and vision extraction,
overridable per-environment via `OPENAI_MODEL`. Explicitly **not** a reasoning
model.

**Why the vendor changed:** ADR-008 was written before any code existed. The
build ended up on the OpenAI SDK, and this ADR is recorded to stop the
documentation claiming otherwise. Nothing in the architecture depends on the
vendor: the model is called in exactly one place (`event_parser._call_model`),
forced to fill one fixed schema, and every value it returns is re-derived or
validated in Python. Swapping it back is a contained change.

**Why not a reasoning model — the part worth keeping.** The first build used
`gpt-5-nano`. It spent **~1,700 completion tokens "thinking"** before emitting
a tool call barely 30 tokens long, costing **~15 seconds of wall clock on every
message**. Turning the reasoning down fixed the clock and broke the output:

| `reasoning_effort` | Latency | Completion tokens | Quality |
|---|---|---|---|
| default | 19.3s | 1,761 | good |
| `low` | 6.2s | 548 | dropped the new time on an amend |
| `minimal` | 1.7s | 43 | lost the child, the time *and* the category |

`gpt-4.1-mini` on the same ten messages: **~1.2s, ~26 completion tokens, and
more accurate than any `gpt-5-nano` setting** — including on the amend case the
reasoning model failed. It was also ~18% cheaper per message despite a higher
per-token price, because the thinking tokens had dominated the bill.

**The generalisable lesson:** filling a fixed schema is *extraction*, not
reasoning. Paying a reasoning model to deliberate about which field a date goes
in buys nothing, and the user waits through all of it. Match the model to the
shape of the task, and measure rather than assume — "better model" was, here,
12x slower and less accurate.

**Consequences:**
- `OPENAI_MODEL` must stay a non-reasoning model; `.env.example` says so, and
  says to re-measure both latency *and* parse quality if it changes.
- `PRICE_PER_MILLION_TOKENS` in `main.py` is model-specific and must be updated
  alongside it. It is display-only — the budget cap counts **tokens**, so a
  stale price can never misfire the cutoff (see ADR-007's storage note).

---

## ADR-011: Travel time from a hand-written table, asked for and remembered

Decided 2026-08-16, on request: judge a proposed event against the time needed
to move between places.

**Decision:** Model travel time as a **hand-maintained table of places and
minutes** (`places.py`), extended at runtime by **asking the family and
storing the answer** (SQLite `travel_times`). No routing API, no LLM estimate.

**Why not the Google Routes API.** It is traffic-aware and would be more
accurate in principle, but it needs an API key and billing, a network call on
the confirm path, and a new failure mode on the one path the family depends
on. Decisively, it needs geocodable addresses and these events do not carry
them — school events say "Gate 2", enrichment says "Piano". A places table
would still be required underneath it just to know what to geocode, which
makes routing a later upgrade *on top of* this decision rather than a
competing one. The table stores place labels in a shape that leaves that door
open.

**Why not ask the model.** `roster.py` opens by stating that arithmetic
deciding whether a child gets collected must be inspectable and testable
rather than inferred. A model's guess at a distance is precisely the inferred
number that rule exists to keep out, and it would be non-deterministic across
identical cards.

**Why asked-and-remembered rather than a default guess.** Three options were
weighed for an untimed pair: stay silent, warn that it cannot tell, or assume
a default. Silence misses the first occurrence — the one that matters. A
warning on every unmapped pair is noise until the table fills, and a muted
warning system is worse than none. A default invents a number, which is the
thing this ADR refuses to do. Asking is the only option that is both honest
and self-limiting: each answer permanently removes one future question.

**Consequences:**
- A `TIGHT_TRANSITION` warning only ever rests on a number a human gave.
  Unknown pairs produce a question, never a warning.
- A seeded entry in `places.py` **beats** a learned one, so hand-editing the
  file always takes effect. `/travel` shows which is which; `/travel forget`
  reverses a wrong tap. Inspectable and reversible, per `THREAT_MODEL.md` §7.
- The bucket keyboard sits **below** Confirm and the pending state is peeked,
  not popped: a question about a route can never consume the write the card is
  offering. This is the same human-in-the-loop rule as everywhere else — the
  bot may ask, but it may not stand in the way of, or substitute for, a tap.
- The same-person pickup buffer became place-aware, which can make the bot
  **quieter** than the blanket `PICKUP_BUFFER_MINUTES`. Two children collected
  20 minutes apart at one centre is genuinely fine; warning about it was the
  false positive that teaches people to ignore warnings. Untimed pairs keep
  the flat buffer, so nothing goes quiet merely for being unmapped.
- The confirm card now reads the same child's calendars as well as the other
  children's (6 reads instead of 3 on a two-child setup). Amends must therefore
  exclude the event being moved **by id**, or a proposal is compared against
  the booking it replaces.
- Learned travel times are **not** fed into any model prompt, unlike
  `learned_hints`. They are read only by arithmetic, so they are not a path
  from an untrusted screenshot into the model.

**Amended 2026-08-16, same day:** the table was seeded with the family's nine
real places and all 36 journeys between them, and two things were learned in
the process.

*Symmetry is a decision, not a convenience.* The family confirmed the return
leg takes the same time, so one entry per pair is correct rather than merely
cheaper. Storage sorts the pair; the QUESTION is phrased in the direction the
journey is actually made, because "Centre_1 to school" describes a day running
backwards.

*Transport mode is per carer, not global.* Every stored number is a CAR time;
public transport adds a flat 20 minutes. Keying that by carer follows from
something the system already knows — the child travels with whoever collects
her, and the roster names that person on every event. A global switch would be
wrong on exactly the day it matters most: the day two carers with different
modes are both doing runs. An unlisted carer is assumed to be on public
transport, so an unfamiliar person makes the bot more likely to warn, not
less. Journeys of 10 minutes or under are exempt, being walks within one
location where the mode does not apply.

*Places can be scoped to a child.* Three ballet studios all answer to the word
"ballet". Scoping resolves a bare "Ballet class" to Child_2's only studio while
leaving Child_1's — she has two, told apart by branch name alone — resolving to
nothing. Nothing is the right answer to an ambiguous place: no place means no
travel check, and this ADR's whole premise is that the bot never warns on a
guess.

**Amended again 2026-08-16:** the table became editable from Telegram, which
required inverting part of this ADR.

*Three levels of precedence, not two.* This ADR originally said a hand-written
entry in `places.py` beats a stored one, so that editing the file takes effect.
That rule made the file the only editing surface — and with all 36 journeys
seeded, an edit made from a phone would have been stored and then ignored. The
rule is now split by INTENT rather than by location:

    a deliberate /travel edit   >   places.py   >   a bucket tapped on a card

The original reasoning survives intact, because it was never really about
files versus databases: it was that a considered statement should not be
overwritten by a hurried one. A bucket answered mid-write still loses to the
file. The `travel_times.source` column carries the distinction, and
`set_travel_minutes` refuses to downgrade a `'set'` row to an `'answered'` one,
so the guarantee holds at the write and not only at the lookup.

*State for a multi-step flow goes in `callback_data`, not `pending_state`.*
`pending_state` is keyed by chat id alone — one pending item per chat — so a
picker that used it would clobber an open confirm card and turn a pending
calendar write into "that request expired". Two small integer indexes fit
easily inside Telegram's 64-byte callback payload, and are validated on
arrival like every other index this bot accepts from a client.

*`stored_minutes` is separate from `travel_minutes`.* The editor must display
and accept the raw CAR time; the checker applies the per-carer transport
penalty on top. Showing the adjusted figure in a question that asks "how long
by car?" would invite an answer with the penalty already in it, which would
then be added again on every lookup.

**Amended 2026-08-17:** travel edits also work in plain words, via a `travel`
action on the existing classifier rather than a second parsing path.

*Applied, not confirmed.* Every other write in this system waits for a tap,
and this one deliberately does not. The distinction is what the change can
reach: a calendar write moves an event the grandparents can see, and an email
leaves the house, whereas a travel time only adjusts when the bot warns. The
reply names both places and the number, so a misparse is visible immediately,
and an ↩️ Undo makes it one tap to reverse. A confirm card in front of every
correction was exactly the friction that would leave the table un-maintained.

*Classification is the whole risk, and it is checked against the real model.*
"Swimming is at 4pm" and "swimming is 20 minutes from school" differ only in
sentence shape. The unit suite cannot speak for this - it hands `_finalize`
pre-baked dicts - so the guarantee lives in `scripts/smoke_parser.py` as
paired cases in both directions. The prompt is also given the family's real
place names, which is what makes "Centre_1" read as a location rather than an
activity to schedule.

*An ambiguous place is asked about, never resolved by ordering.* "School"
names two different places in this family, at different distances. Place
matching returns candidates rather than a best guess: one resolves, several
ask with buttons. Silently picking the first would produce a confident,
wrong safety number - the exact failure this whole feature exists to prevent.
