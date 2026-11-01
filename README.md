# Family Scheduler

A Telegram bot that turns "child_1 piano Tuesday 4pm" — or a photo of a school
notice — into the right entry on the right Google Calendar, and asks a human
before it writes anything.

Built for my own household: two children, six calendars, four adults with
different levels of access, and a school that communicates by app notification
with no API. It ran 24/7 on a Google Cloud free-tier VM from August 2026 until
the project was closed in October 2026.

> **A note on this copy.** It is an anonymised snapshot of a private repository.
> Every real name — the children, the adults who collect them, the grandparents,
> and the venues — has been replaced with a placeholder (`child_1`, `parent_2`,
> `helper_1`, `centre_1` …), and the private session log is not included. The
> code and its 793 tests are otherwise the real thing. See
> [REFLECTIONS.md §11](REFLECTIONS.md) for what anonymising it turned up.

```
Me:   [screenshot of a school notice]
Bot:  Is this for child_1 or child_2?   [ child_1 ] [ child_2 ]
Me:   → tap child_2
Bot:  Add "child_2 10.30am Gate 2 Teachers' Day Celebration"
      Thu 03 Sep, 10:15–10:30  [child_2 — School]?
      [ ✅ Confirm ] [ ✏️ Change something ]
      [ ❌ Don't add ]
```

---

## Why this exists

The problem was not "I need a calendar". It was that schedule changes arrive as
**push notifications in an app with no export**, get read once, and then live in
somebody's memory until the moment they matter. The failure mode is standing at
the wrong school gate at 10:30 on a Thursday.

Three constraints shaped everything:

1. **No integration is possible.** The school app has no API, no ICS export, no
   webhook. Screenshot ingestion isn't a shortcut — it's the only channel that
   exists ([ADR-004](ADR.md#adr-004-screenshot-ingestion-for-school-updates)).
2. **The users are my family, not engineers.** Grandparents included. The
   interface has to be a chat they already have open, and a button.
3. **A wrong write is worse than no write.** A missed pickup costs more than a
   confirmation tap ([ADR-005](ADR.md#adr-005-human-in-the-loop-for-all-writes)).

## What it does

| | |
|---|---|
| **Create** | Free text or a screenshot. Multiple dates and repeats in one message. No time given → an all-day event, rather than nagging for one. |
| **Delete / Amend** | Matched by *intent*, not literal words: "cancel her appointment today" finds the event on that day even though nothing is called "appointment". |
| **Query** | "what's on this weekend?", `/upcoming child_1 school` |
| **Conflicts** | Several candidate times are ranked (double-booked > nobody to collect > one carer needed twice > too tight to travel > clear). Ranking writes nothing; picking a row goes to the ordinary confirm card. |
| **Who collects** | A pickup roster, clash detection between children, and a confirm card that *asks* who is collecting on a Saturday rather than assuming. Carer names are free text, so typos are folded onto the real name before they can read as a second adult. |
| **Travel time** | Warns when one child's two events are too close to travel between — using only a number a human gave. Edited from Telegram in plain words ("school to centre_1 takes 25 minutes"), with an Undo. |
| **Series changes** | "Change child_2's ballet pickup to parent_1 for the next 4 months" — one card listing every affected date, one tap. |
| **Email** | Send-only, to an allowlist of addresses, with the full text shown first and a daily cap. The bot never reads a mailbox. |
| **Failures** | Every failure says what is actually wrong and whether retrying helps; the admin gets the technical detail, once an hour per kind. |
| **Permissions** | Telegram ID → `admin` / `editor` / `reader`. Unknown IDs are ignored silently — no reply at all, so the bot's existence isn't confirmed to strangers. |
| **Cost control** | Per-call token metering, a monthly budget, `/usage` |
| **Learning** | Confirmed writes teach it your vocabulary, so it asks fewer questions over time. `/hints` to inspect, `/hints reset` to undo. |

Every write requires an explicit button tap. There is no path to the calendar
that doesn't go through one.

### Six calendars, and why

Google Calendar grants sharing **per calendar, never per event**. The
grandparents help with school runs and should see school events; they should
not see medical appointments. That boundary cuts *within* a child, so no amount
of per-event tagging can express it — hence `{child} × {school, enrichment,
others}` ([ADR-009](ADR.md#adr-009-per-category-calendars-school--enrichment--others)).

The access rule is enforced by Google's own ACLs, not by application code. The
bot's permission table doesn't know calendars exist. Two independent systems,
neither able to silently undermine the other.

This is also why the bot **asks** which calendar an ambiguous event belongs to
instead of guessing. `others` is a choice a human makes, never a fallback for
"couldn't classify" — a wrong guess doesn't make a mess, it leaks a private
appointment to the grandparents.

---

## Architecture

The interesting decision here is how *little* autonomy the model has.

```
Telegram message
     │
 ┌───▼──────────────────────────────────────────────┐
 │ 1. ROUTING — deterministic, no model involved     │
 │    photo/document → handle_photo                  │
 │    text           → handle_text                   │
 │    button tap     → handle_callback               │
 └───┬──────────────────────────────────────────────┘
     │
 ┌───▼──────────────────────────────────────────────┐
 │ 2. THE MODEL — one call, one fixed schema         │
 │    Returns {action, title, date, child, ...}      │
 │    It fills in a form. It cannot call anything.   │
 └───┬──────────────────────────────────────────────┘
     │
 ┌───▼──────────────────────────────────────────────┐
 │ 3. PYTHON — validates, resolves, decides          │
 │    "every Tuesday for 6 weeks" → 6 real dates     │
 │    child + category → which calendar              │
 │    bounds, escaping, rate limits, budget          │
 └───┬──────────────────────────────────────────────┘
     │
 ┌───▼──────────────────────────────────────────────┐
 │ 4. HUMAN — taps ✅, and only then, the write       │
 └──────────────────────────────────────────────────┘
```

**The model is a parser, not a dispatcher.** It has exactly one tool, and that
tool is a *data schema* — not an action. It never chooses what code runs; that's
decided by which handler the message type matched (layer 1) and by a `switch` on
the `action` field (layer 3). The model's entire influence is over *values in a
closed set of fields*.

**Python re-derives anything that matters.** The model reports the *shape* of a
request ("weekly", 6 times) and Python walks the calendar, because LLMs are
unreliable at date arithmetic and a silently wrong date is expensive. Same for
resolving which calendar, bounds-checking, and escaping output.

The payoff is a security property that survives a bad day: a malicious
screenshot can't make the bot *do* anything. Its best case is a wrong
confirmation card, which a human declines. See [THREAT_MODEL.md](THREAT_MODEL.md)
— including [§4](THREAT_MODEL.md), on why no guardrail library is used and the
conditions that would reverse that.

### Files

| File | |
|---|---|
| `main.py` | Telegram handlers, confirm cards, commands, permission routing |
| `event_parser.py` | The model call, the schema, and all validation/resolution |
| `calendar_tools.py` | Google Calendar; the child × category registry |
| `roster.py` | Who collects whom, pickup clashes, carer-name folding, clash ranking |
| `places.py` | The family's places, journey times, and the precedence between sources |
| `gmail_tools.py` | Send-only email: allowlisted recipients, daily cap |
| `errors.py` | Turns any exception into what to say, and whether to alert the admin |
| `db.py` | SQLite: pending state, audit log, token metering, learned hints, travel table |
| `permissions.py` | Telegram ID → role → capabilities |
| `vision_tools.py` | Screenshot → the same schema as text |
| `scripts/smoke_parser.py` | Live phrasings that have broken before, run against the real model |

Flat, not packaged — deliberate for a project this size, and noted as a change
I'd make before a second developer joined.

---

## Running it

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.lock.txt
cp .env.example .env        # then fill it in
python main.py --auth       # one-time Google OAuth, needs a browser
python main.py
```

`.env.example` documents every variable, including the non-obvious ones
(`LONE_TIME_MEANS`, `MONTHLY_TOKEN_BUDGET`, and why `OPENAI_MODEL` must stay a
non-reasoning model). Deployment is systemd on an `e2-micro`; `Restart=on-failure`
plus `ProtectSystem=strict` with an explicit `ReadWritePaths`.

```bash
python -m pytest tests/ -q     # 793 tests
```

The tests are the honest measure of this project. They exist because live bugs
kept teaching the same lesson — most memorably that **anything stubbing the
database cannot catch a serialization bug**, and that a green suite can still
miss every *combination* of button and action. See [REFLECTIONS.md](REFLECTIONS.md).

Because the suite stubs the model on purpose, `scripts/smoke_parser.py` holds the
phrasings that have broken the real one, as cases with expected outcomes. Run it
after touching the prompt or the schema; it needs a live API key.

---

## Scope, honestly

**Built and running:** everything above.

**Email is partly built, on purpose.** [THREAT_MODEL.md §6](THREAT_MODEL.md)
names three things email changes at once: anyone can email you, the bot's output
leaves the system, and a poller fires on a timer instead of in response to a
person. The design that shipped removes two of them outright — there is no
mailbox read (a family member forwards a reply to Telegram) and no timer (every
send is a human tapping ✅ on the full text) — and puts a rule, not a prompt,
on the third: recipients come only from a configured allowlist. The Gmail
*Watcher* in [CLAUDE.md](CLAUDE.md) — polling threads and parsing replies — is
specified and **not built**.

`CLAUDE.md` is the original spec and the instruction file the AI-assisted build
was driven from. It's kept, including the parts that were never implemented,
because a spec that quietly rewrites itself to match what got built isn't a spec.

**Known limitations:** single-family scale by design (SQLite, one process,
polling). No CI. Travel times are a hand-kept table, not a routing API — on
purpose, and recorded in [ADR-011](ADR.md). Flat modules rather than a package,
which is a change I'd make before a second developer joined.

---

## Outcome

The project was closed on 30 October 2026 as a finished proof of concept.

| | |
|---|---|
| Duration | 25 Jul – 18 Oct 2026: 67 commits over 17 working days |
| Size | ~9,100 lines across 9 modules; 793 tests (about 12 s, none call a live model or Google API) |
| Live check | ~45 real phrasings run against the real model by `scripts/smoke_parser.py` |
| Running cost | VM $0 (Always Free tier); model spend well under USD 1 a month at family volume |
| Latency | ~1.2 s per message, down from ~15 s (see REFLECTIONS §2) |

It was designed as a three-agent system and ended as one guarded pipeline: a
single model call fills a fixed structure and plain Python does everything else.
That is the main thing the build taught me, and why REFLECTIONS §15 asks whether
that choice should now be tested against a tool-calling agent.

**Left undone, on purpose or by running out of reason to continue:** the Gmail
Watcher (replaced by send-only email), CI and linting, monitoring and backups,
and the agent comparison above.

## Reflections

[REFLECTIONS.md](REFLECTIONS.md) is the part I'd actually want read: what the
bugs taught, the decisions I'd defend, the ones I got wrong, and what AI-assisted
development is and isn't good at when the thing you're building will be used by
your own parents.

## Documents

| | |
|---|---|
| [ADR.md](ADR.md) | Every architectural decision and its reasoning, including one superseded and marked as such |
| [THREAT_MODEL.md](THREAT_MODEL.md) | Attack surfaces, defences, residual risk stated honestly |
| [REFLECTIONS.md](REFLECTIONS.md) | What I learned building it |
| [CLAUDE.md](CLAUDE.md) | The original spec |
