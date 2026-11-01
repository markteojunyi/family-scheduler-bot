# Threat Model — Family Scheduler

Written 2026-08-03, prompted by the question "should we add LLM Guard or
NVIDIA NeMo Guardrails?". The short answer is no, and this document exists to
record *why*, so the decision can be revisited on evidence rather than
re-argued from scratch — particularly when Gmail (Phase 3/4) changes the
picture.

Scope: the deployed Telegram bot writing to six Google Calendars. Gmail is
**not built**; its threat surface is sketched at the end because it is
materially different and must be designed for before that phase starts.

---

## 1. What an attacker would actually want

| Goal | Reachable today? |
|---|---|
| Write/delete events on the family calendars | Only via a proposal a human approves |
| Read the children's schedules | Only from an authorised Telegram account |
| Exfiltrate data out of the system | **No outbound channel exists yet** |
| Run code / commands on the VM | No path — the model's output is a fixed JSON schema |
| Run up API spend | Bounded by the LLM rate limit |
| Expose private events to the grandparents | Via mis-categorisation — see §5 |

The absence of an outbound channel is the single most important property of
the current design. Nothing the model produces leaves the system except as a
Telegram message back to the same authorised user. **Gmail removes this
property**, which is why it is gated.

---

## 2. Where untrusted content actually enters

Only three doors, and they are not equally trusted:

1. **Telegram messages from known IDs** — four accounts, resolved by numeric
   ID. Unknown IDs are logged and ignored silently, so this is not an
   open-internet surface. Trust: high (people we know), but see §5 — the
   *content* they forward is not necessarily theirs.
2. **Screenshots** of school notices. Trust: **low**. The image is produced by
   a third party and read by a vision model. This is the primary injection
   surface today.
3. **Forwarded messages** from enrichment centres. Trust: **low**, same
   reasoning.

So the realistic phrasing is: *authorised people relaying untrusted content*.
The account is trusted; the payload is not.

---

## 3. The defences that actually matter

Ordered by how much load they carry. Note that the top three are structural —
they hold regardless of what the model does.

### 3.1 The model cannot emit an arbitrary action
`tool_choice` forces exactly one function, and `INTERPRET_TOOL` defines a
closed field list with `required: ["action"]`. There is no output shape in
which the model returns a shell command, a loop, a second event, or a
different recipient. A perfect injection still yields *one* structured
proposal for *one* event.

### 3.2 No write happens without a human tapping ✅
Every create/amend/delete goes through a confirmation card (ADR-005), and
`db.pop_pending` enforces a 30-minute TTL so a stale card cannot be tapped
into consent for something long forgotten. **The maximum damage from a
successful prompt injection is: a suspicious proposal appears on the admin's
phone and is rejected.**

### 3.3 Identity and capability gates
Unknown Telegram IDs get no reply at all — not even a refusal, which would
confirm the bot exists. Writes require the `write` capability and committing
requires `confirm`; `email_approve` and `audit` are admin-only.

### 3.4 Values are re-derived in Python, never trusted from the model
Dates are computed in Python from a raw reference ("sunday"), never by the
LLM. Field text is capped at 200 chars, dates bounded to ±2 years, relative
shifts to ±1 week, callback indices bounds-checked against the list actually
shown. A bad parse and a hostile parse are handled by the same code, which is
the point.

### 3.5 Output is escaped and metered
`_escape_markdown` on everything model-derived, so a crafted title cannot
inject Telegram formatting or fake UI. Secrets are redacted from logs by
`JsonFormatter`. LLM calls are capped at 30/user/10min and photo downloads at
1.5 MB (8 MB hard reject).

### 3.6 Prompt hardening — the weakest layer, and known to be
`_HARDENING` tells the model that message and image content is data to
extract, never instructions to follow. It is genuine defence-in-depth and it
is *not* a guarantee: a sufficiently clever payload can talk a model out of a
system prompt. It is listed last deliberately. If it were the only defence,
this system would not be safe.

---

## 4. Why not LLM Guard / NeMo Guardrails / a moderation pass

Not rejected on principle — rejected because the cost/benefit does not hold
*for this system*:

1. **They defend a gap that is already closed structurally.** Their value is
   catching malicious instructions before a model acts on them. Here the model
   cannot act at all: it fills a fixed schema, and a human commits the result.
   A scanner would be a second opinion on a decision that is already
   human-approved.
2. **They cost a model call per message**, on top of the parse — roughly
   doubling latency and spend on a system whose only uncapped cost was
   deliberately capped in the same session.
3. **They are heavyweight for a 1 GB e2-micro.** NeMo Guardrails in particular
   pulls a substantial dependency tree; some LLM Guard scanners load local
   transformer models, which this VM cannot comfortably host alongside the bot.
4. **False positives are a real usability tax.** A parent writing "cancel
   everything on Friday" is a legitimate, common request that reads like an
   attack to a naive classifier. Blocking it teaches the family the bot is
   unreliable — the worst outcome for a tool that must be trusted.
5. **They do not address the actual residual risk** (§5), which is a *valid*
   event with wrong details.

**What would change this decision:** the Gmail phase (§6), an outbound channel
of any kind, removing the human confirmation step, or opening the bot to
non-family users. If any of those happen, re-open this section.

---

## 5. The residual risk, stated honestly

The realistic attack is **not** "make the bot execute something". It is
**social engineering the human through a valid-looking proposal**.

A crafted school notice screenshot yielding `Child_1 3pm Gate 7 Assembly` — with
a plausible but wrong gate or time — produces a perfectly well-formed event.
Every defence above passes it, because nothing about it is malformed. No
scanner catches it either, since the *output* is legitimate; only the
*intent* behind the input was not.

The mitigations are therefore human-facing, not technical:

- The confirm card shows the exact string that will be written, so what is
  approved is what is stored (this is why the title is built at finalize time,
  not at write time).
- Keep confirm cards short and readable. A card nobody reads is a rubber stamp.
- The audit log records who approved what, so a bad write is traceable
  afterwards.

**A second, quieter risk: mis-categorisation.** Filing a medical appointment
under `school` would expose it to the grandparents, since Google shares per
calendar and never per event. This is why the bot **asks** rather than guessing
the category, and why `others` is never a silent fallback. That behaviour is a
privacy control, not a UX nicety — it should not be "simplified" later.

---

## 6. Gmail (Phase 3/4) — why it is gated

> **Status update.** A send-only slice has since shipped (`gmail_tools.py`). It
> removes points 1 and 3 below by construction — the bot never reads a mailbox
> (a family member forwards a reply to Telegram) and nothing runs on a timer, so
> every send is a human tapping ✅ on the full text — and puts a rule, not a
> prompt, on point 2: recipients come only from the `ENRICHMENT_EMAILS`
> allowlist, with a hard daily cap counted from the audit log. The Watcher
> described in CLAUDE.md is still unbuilt, and the section below still governs
> it.

Email changes three things at once, which is why the defences must be
*designed before the flow is built* (a rule inherited from the readiness
audit's §2.5, now in `docs/archive-2026-07.md`; the live backlog entry is
in `PROGRESS.md`):

1. **Fully untrusted input.** Anyone can email the inbox. The "authorised
   people relaying untrusted content" framing of §2 no longer holds.
2. **An outbound channel appears.** The bot drafts replies, so for the first
   time injected content could influence something that *leaves* the system —
   the exfiltration path that does not exist today.
3. **A scheduled trigger.** The Gmail Watcher acts on a timer rather than in
   response to a human, weakening the "a person is always in the loop at the
   moment of action" property.

Minimum bar before that phase ships (beyond what CLAUDE.md already specifies
about only reading bot-initiated threads):

- Recipient addresses come **only** from the stored thread, never from email
  content. An injected "reply instead to X" must be structurally impossible.
- Email body is extracted into a closed schema, exactly as message text is.
- Every resulting calendar change and every outbound email keeps its human
  approval step — no auto-send, no auto-resolve.
- **This is the point at which input scanning earns its cost.** Re-read §4.

---

## 7. Known gaps, accepted for now

- No scanning of input or output (§4 — deliberate).
- `_HARDENING` is unverified against real adversarial payloads; nobody has
  actually tried to break it with a crafted screenshot. Worth doing as an
  exercise, and the confirm card means the test is safe to run.
- Rate limits are per-user, so several compromised accounts would multiply
  the budget. Acceptable at a family of four.
- No alerting: a rejected proposal or a rate-limit trip is logged but nobody
  is told. A repeated pattern would go unnoticed.
