# Reflections

Notes on building this — what the bugs taught, which decisions I'd defend, and
which I got wrong. Written for my own future reference, and because the
reasoning is the part worth keeping. The code is replaceable.

---

## 1. Give the model as little autonomy as the problem allows

The obvious way to build this in 2026 is an agent: hand the model a set of tools
— `create_event`, `delete_event`, `send_email` — and let it decide what to call.
I deliberately didn't.

The model here has exactly **one** tool, and that tool is a *data schema*, not
an action. It fills in a form. Python reads the form, validates it, decides what
to do, and asks a human before doing it.

That isn't caution for its own sake. It buys three specific things:

- **A bounded failure mode.** Untrusted content enters this system by design —
  a screenshot of a school notice is a picture of text somebody else wrote.
  Because the model can only populate fields, the worst a hostile screenshot
  achieves is a wrong confirmation card that I decline. There is no prompt that
  turns into an action.
- **Debuggability.** When something goes wrong, there's one model call to
  inspect and a deterministic pipeline after it. In an agent loop, "why did it
  do that?" can mean replaying a multi-step trajectory.
- **Latency and cost I can predict.** One call per message, ~26 completion
  tokens, ~1.2 seconds.

**When I'd reach for a real agent instead:** when the *sequence* of steps
genuinely can't be known in advance — research, multi-source investigation, "try
this, and if it fails try something else". This problem isn't that. The set of
things a family does to a calendar started as four verbs. Encoding four verbs as
a schema and a `switch` was not a limitation I had to work around; it was a more
honest model of the problem.

*Revisited, three months in:* it is no longer four verbs. Travel times, series
changes, availability questions, ranked slots and email have each added fields,
and the schema is now around forty of them — near the point where one form is
doing the work of several. That is a real cost of this choice, and it is the
reason §15 exists. What has *not* changed is why I made it: the model still
cannot do anything, and every one of those features arrived as a new field and a
new branch in Python, never as a new power for the model.

The tell for over-engineering is: *if you can draw the flowchart, you don't need
an agent to discover it at runtime.*

## 2. Match the model to the shape of the task — then measure

The first build used a reasoning model. Every message took **~15 seconds**. I
assumed the API was slow, or the network, or Telegram.

It was none of those. The model was spending **~1,700 completion tokens
"thinking"** before emitting a tool call barely 30 tokens long. My family was
waiting fifteen seconds for deliberation about which field a date belonged in.

Turning the reasoning down fixed the clock and broke the output — at minimal
effort it lost the child, the time *and* the category. The actual fix was to
stop using a reasoning model at all. `gpt-4.1-mini` came in at **~1.2 seconds,
~26 completion tokens, and was more accurate**, including on a case the
reasoning model got wrong at every setting. It was also cheaper, because the
thinking tokens had dominated the bill.

The lesson isn't "small models are fine". It's that **filling a fixed schema is
extraction, not reasoning**, and I'd reached for the more capable-sounding tool
without asking what the task actually required. "Better model" was, here, 12x
slower and less accurate.

I'd also note how I found it: by timing it and reading the token counts, not by
reasoning about it. I'd been guessing for a while before I measured.

## 3. The user names the last thing they saw work

A bug report arrived as **"why isn't image recognition working?"**

Image recognition was working perfectly. The screenshot parsed; the event came
out correct. The bot then asked "Is this for child_1 or child_2?", the answer was typed,
and the event vanished.

The real fault was two steps downstream, in the mechanism for answering a
follow-up question — code with nothing to do with images. Taken literally, the
report would have sent me hunting through the vision call and the prompt, and I
would have found nothing, because nothing was wrong there.

What found it in one pass was **reading the transcript rather than the code**.
The bot's final reply was a *list of that child's existing events* — which is a
perfectly correct answer to "what's on for child_2?". Not a corrupted parse: a
**correct parse of the wrong input**. From there the only question is "how did
the word 'child_2' reach the model on its own?", which points straight at the
replay mechanism.

Two things I keep: **users report symptoms, not causes** — the component they
name is where they last saw sense. And **a coherent-but-unrelated answer is a
routing bug**, whereas garbage is a parsing bug. Those point in opposite
directions.

## 4. The expensive bugs don't throw exceptions

The underlying flaw in the above: answering a clarification worked by gluing the
user's reply onto the original message and re-parsing. Elegant — when there *is*
an original message. An uncaptioned screenshot has none, so what got replayed
was the bare word "child_2".

No crash. No error. A plausible-looking answer to a question nobody asked. An
exception would have pointed at the line; this needed a person to notice the
reply was *coherent but irrelevant*.

The fix generalises: **when an answer has to survive a round trip through the
model, don't send it through the model.** A button tap carries the parse forward
untouched and can't be misread.

The genuinely uncomfortable part is that this exact fix already existed
elsewhere in the codebase, for what looked like a different reason. I'd solved
the specific case and not extracted the rule.

## 5. Anything stubbing the database cannot catch a serialization bug

Three separate outages in this project traced to the same shape: state written
to SQLite as JSON came back as a *different type* than it went in.

The last one was the nastiest. The decoder converts strings under a key named
`date` into date objects — correct for the bot's resolved fields, wrong for the
model's raw output, which is re-parsed from scratch and rightly expects a
string. So `"2026-09-03"` came back as a `date`, something called `.strip()` on
it, and the whole handler died.

It hid for weeks because a *typed* message fills a `day` field ("tuesday"). Only
a **dated screenshot** fills `date` with a real ISO string. The path nobody
tested by hand was the path that broke.

And my test for that feature had **stubbed the database**, so it never touched
encode/decode at all. It passed while the live bot failed.

Two rules now: **test the join, not just the parts** — at least one test per
flow with no mock between the input and the output. And **round-tripping is a
contract**: if a value must come back identical, there needs to be a test
asserting exactly that, not a test of something nearby.

A related habit I've adopted: after writing a regression test, **break the fix
and confirm the test fails**. Twice I've written tests that couldn't have caught
the bug they were named after.

### Addendum: it was never really about the database

Months later I was asked to stop patching and find the pattern behind five more
bugs that had got past 361 passing tests. It wasn't "more tests". It was *what
kind*. Every component worked; every branch had been touched; every one of the
eight button types appeared in a test and had a handler. The failures were all in
**combinations** — a button crossed with an action, a pending-state kind crossed
with a key, a handler crossed with the *next* message. The suite tested a list of
parts and called it coverage; production runs the product of them.

The fix was a deliberately shallow, deliberately wide suite: every action,
arriving both from a button and from a message; every pending kind through a real
SQLite round trip; every button the code emits has a handler. Its assertions are
weak on purpose (*did it answer?*), because breadth is what the rest of the
suite lacks. The same pass found that tests reached the handlers with an `Update`
while production sometimes arrived with a `CallbackQuery` that shares just enough
shape to work by accident — so I erased the difference at the two points where a
button re-enters dispatch, instead of relying on everyone to remember it.

And the model is the one thing the suite stubs, correctly, so a separate script
runs every phrasing that has ever broken against the real thing. It immediately
found two more bugs: a prompt that said to extract the child "if the message
names one of them", which invited the model to drop a name it didn't recognise
(the same input gave different answers on different runs), and a name matcher
that read "Joanna" as "Anna" because it matched substrings in both directions —
fixed twice, because the rule had been written in two places and the two copies
then disagreed about who a message was about.

## 6. Work out which error is more expensive, then bias toward the cheap one

School event titles carry a pickup gate — "child_1 3pm Gate 2 Assembly" — because
standing at the wrong gate is the concrete failure this whole thing exists to
prevent. Then I needed to suppress the gate on days with no school, where
printing one reads as an instruction to turn up.

The instinct when writing a matcher is to be generous with it. Here generosity
is the failure mode, because the two errors are wildly asymmetric:

- Suppressing a gate that *was* needed → a parent at the wrong gate. The exact
  thing the feature prevents.
- Showing a gate on a day off → mild noise on a calendar entry.

So the matching is deliberately **narrow**, and anything ambiguous keeps its
gate. `public holiday` matches; a bare `holiday` does not — because "Holiday
concert" is a real event with a real pickup.

I also stopped choosing *between* a deterministic matcher and the model, and
used both. A regex can't know "PH" means public holiday in Singapore; the model
can. The model isn't in the loop when I edit an event written months ago; the
regex is. They cover each other's blind spots.

And the precedence rule wrote itself once I named what each input *was*: a gate
a human typed is **evidence**; the model's flag and the word match are
**inferences**. Sort by how much of a guess something is, and the ordering is
obvious.

## 7. Say no to a security layer, but write down what would change your mind

I looked hard at LLM Guard and NeMo Guardrails and didn't use either. That
decision is recorded in [THREAT_MODEL.md](THREAT_MODEL.md) §4 with its
reasoning, because a "no" without reasoning becomes folklore.

The argument: name the specific attack a layer stops, then check whether
something cheaper already stops it. Here two structural properties already did —
the model can only emit fields from a closed schema, and no write happens
without a human tap. A prompt-injection scanner would be inspecting text that
can't reach an action anyway.

Writing the threat model also surfaced that the *real* residual risk is one no
scanner addresses. A crafted screenshot producing "child_1 3pm **Gate 7** Assembly"
— right shape, wrong gate — passes every filter, because the output is perfectly
valid. Only the intent behind the input was hostile. The control that defeats it
is the confirm card being short and readable enough that a human actually reads
it. **The most effective security control in this system turned out to be a UI
property.**

Most importantly, the decision has an **expiry condition**. It depends on facts
that will change: no outbound channel, a human at the moment of action, a closed
user set. The Gmail phase breaks all three at once. So the reversal conditions
are written down next to the decision, rather than leaving someone to inherit a
stale "we don't need that".

### Addendum: the expiry condition arrived

Writing the reversal conditions down in advance paid off sooner than I expected.
When email came up, the three things it changes were already named — untrusted
inbound, an outbound channel, a scheduled trigger — so the question was not
"is this safe?" but "which of these can I design away?". Two could. The bot
never reads a mailbox (a family member forwards the reply to Telegram), and
nothing runs on a timer (every send is a human tapping ✅ on the full text). The
third, the outbound channel itself, got a *rule* rather than a prompt: recipients
come only from a configured allowlist, so "reply instead to someone@elsewhere"
has nowhere to go, and a daily cap counted from the audit log means a loop or a
bad parse cannot turn the mailbox into a spammer. A prompt is advice; this needed
a rule.

## 8. Choose the unit that can't go stale

The bot has a monthly budget cap. The obvious unit is dollars — that's what I
actually care about.

I denominated it in **tokens** instead. Token counts come straight from the API
response. A dollar figure requires a hardcoded price, and a hardcoded price goes
stale silently the moment the vendor changes it or somebody switches models via
an environment variable.

Cost is still *displayed* in dollars, clearly labelled an estimate. It just
never gates anything. **The number that enforces a rule should be the one that
can't quietly become wrong.**

Same instinct elsewhere: the event title states the time the event actually has,
and an amend rebuilds it, because a title that says "5pm" on a 6pm event is
worse than no title at all.

## 9. Human-in-the-loop is a product decision before it's a safety one

"Confirm every write" reads like a safety constraint. In use, it turned out to
be the thing that makes the bot usable by people who don't trust software.

My family will forgive a wrong *proposal* instantly — it's one tap to decline.
They would not forgive a wrong *write*, because now the calendar is lying and
somebody has to find and fix it. The confirmation step converts every failure
from "silent corruption" into "visible, declined suggestion".

It's also what let me ship an imperfect parser. I never needed the extraction to
be right every time; I needed it to be right *often enough that confirming is
faster than typing*. That's a far lower bar, and it's the bar that made the
project finishable.

The corollary, which I underrated: **the confirm card is load-bearing, so its
wording is load-bearing.** A ❌ labelled "Cancel" on a delete card genuinely
reads as "yes, cancel the appointment". Every decline button now names its
outcome — "Don't delete anything". That's not polish; it's the difference
between a working safety mechanism and a decorative one.

## 10. What AI-assisted development was actually good at

This was built with heavy AI assistance, and I want to be precise about where
the leverage was, because "I used AI" isn't a claim about anything.

**Where it was genuinely fast:** boilerplate against unfamiliar APIs, exhaustive
test cases for logic I'd already specified, refactors with a clear shape, and —
biggest of all — *writing down reasoning at the moment of the decision*. Most of
the ADRs and threat model exist because capturing them cost minutes rather than
an evening. That documentation is now the most valuable artifact in the repo.

**Where it was actively risky:** every bug in this document survived tests that
had been written alongside the code. Confidently-wrong test coverage — tests
that pass without exercising the thing they're named after — is the specific
failure mode to watch for. Three times the mocks were the problem.

**What didn't transfer:** knowing which questions to ask. That the calendar
split has to be per-category came from knowing my parents help with school runs.
That a lone time means *pickup* came from how my family actually writes messages.
That the gate matters at all came from standing at the wrong one. None of that
is in any prompt.

The honest summary: it moved me faster along a path I still had to choose, and
it was most dangerous exactly where it was most confident. The discipline that
mattered was **reading every line before it shipped**, and being able to say why
each one was there — which is the standard this repo is trying to meet.

## 11. A rename is a free fuzz test

To publish this I had to replace every real name with a placeholder — `child_1`,
`parent_2`, `helper_1`, `centre_1`. I expected a find-and-replace. The suite
went from green to 39 failures, and the failures were instructive, because none
of them was the rename itself:

- **The bot escapes `_` for Telegram's legacy Markdown**, so a card that shows
  `child_1` actually contains `child\_1`. Thirty-odd assertions compared against
  the unescaped string. The code was right; my tests had quietly been written
  against names that contain no Markdown characters.
- **A name matcher split words on anything that wasn't a letter or digit**, so
  `child_2's` became `child` and `2` and stopped resolving. Real names never
  contain an underscore, so this had never been reachable.
- **Typo-folding is dangerous between similar names.** Carer names are free text,
  and a name one keystroke from a known carer is folded onto it, so a typo can't
  read as a second adult. `parent_1` and `parent_2` are one keystroke apart, so a
  typo between them would silently have been read as the wrong adult. The real
  names were far apart, so the rule had never met its own failure mode. It now
  folds only when exactly one carer is a single edit away.

The general lesson is about data, not code: **a test suite is coupled to the
shape of the data it was written with**, and that coupling is invisible until the
data changes. Changing the shape on purpose — renaming, localising, swapping the
fixtures for awkward ones — is one of the cheapest ways to find assumptions
nobody wrote down. The first attempt at anonymising taught a smaller version of
the same thing: it used `\b`, which doesn't treat `_` as a boundary, so display
names were renamed while calendar keys were not, and 25 tests went red. The
tests caught the sanitiser, too.

## 12. A default is a guess about usage — replace it when the usage disagrees

The first version read a lone stated time as an *end* — a pickup time — for every
kind of event, because "the family keys in pickup times" was the best guess
available before anyone used the bot. Months in, the family said it plainly:
"piano at 5pm" means the class *starts* at 5. The end-as-pickup reading was only
ever right for school.

There were already two overrides: wording in the message, and a config switch. A
third would have left the wrong assumption in place underneath them. The fix
moved the decision to the axis that actually determines it — the category — and
demoted the config switch to a fallback for calendars with no category.

Three things I'd keep:

- **Put a branching rule in one named predicate.** Title text, block placement on
  create, block placement on amend, and slot ranking all ask the same function,
  so they cannot disagree.
- **A decision that needs information the current layer lacks belongs in the
  layer that has it.** On an amend the parser has deliberately thrown the
  category away, so a category-dependent rule written there silently did nothing.
  It lives where the event has been found, is resolved *once*, and is handed
  down — because "compute the same thing in two places" is how a block and its
  title drift apart.
- **Check whether a rule's stated reason is wider than the rule.** Four days
  later, "enrichment events show both ends of a range" turned out to be wrong for
  every non-school category, because the reason written beside it — duration is
  what you plan an afternoon around — was never about enrichment. The fix was one
  condition, and everything downstream followed without being touched, which is
  the evidence that the earlier design was right.

## 13. An unknown is a question, not a number to fill in

I was asked whether the bot could tell if a child can actually *get* from one
place to the next. It couldn't, and the gap was precise: two events for the same
child had been deliberately ignored (school-then-class is the normal shape of a
weekday) — a good assumption only while the places are near each other.

The tempting answers were a routing API or asking the model to estimate. I
declined both. A routing API needs a key, billing, a network call on the write
path, and geocodable addresses these events don't have ("Gate 2", "Hall B"). A
model's guess at a distance is exactly the sort of arithmetic that decides
whether a child gets collected, and that should be inspectable, not inferred. So
travel time is a table a human wrote.

The rule that fell out is the part I'd defend: **the bot only ever warns using a
number a human gave.** When two events are too close and the journey is unknown,
it doesn't fall back to a default — it asks, on the confirm card, and remembers
the answer per pair of places. Precedence follows *intent*, not storage: a
deliberate edit beats the seeded table, which beats a bucket tapped on a card in
a hurry, because a considered statement must not be overwritten by a hasty one.

Two traps worth recording. Every number in the table is a *car* time and the
penalty for public transport is added later, so the editor must show and accept
the raw car figure or the penalty is counted twice. And multi-step flows must
keep their state in the button's own data, because pending state is one row per
chat and using it would clobber an open confirm card.

## 14. Say what is actually wrong

A screenshot of a party invitation got "Something went wrong on my end — please
try again." Twice, days apart. The cause took one call to find: the AI account had
run out of credit, so *every* message was failing, and the family was being told
to retry something that could never work. Nothing, anywhere, said why.

The generic reply was the real bug. It had special cases for a revoked token and
a full disk and flattened everything else into "try again" — which is untrue
whenever retrying can't help. Each failure now says what happened, whether trying
again will help, and who needs to act.

- **Two audiences.** The admin gets the technical version and the fix; everyone
  else gets plain words, and the admin is alerted separately — at most once an
  hour per kind, so an outage is one alert rather than one per message.
- **"Didn't save" only when proven.** An error that proves nothing happened (a
  refused credential) says so. A timeout does not, because the write may have
  landed before the error came back, and retrying a write that did land makes a
  duplicate. "May not have saved — check first" is the honest sentence.
- **Unknown failures carry a short reference** that appears in the log line, so
  "it said Ref K3F9Q" is one `grep` away from the traceback.
- **Tests build the real exception objects**, not mocks, because the
  classification keys off attributes like a status code, and a mock supplies
  whichever one the code happened to read.

## 15. Where I'd go next: the agent I decided against

§1 argues for a model that can only fill in a form. The honest follow-up is to
test that argument rather than defend it. The schema is near forty fields, the 80%
case is handled well, and the remaining 20% — requests that need to *look
something up before they can be understood* — is where a form is the wrong shape.

The proposal I've written down, and parked, is an agent with **read and propose
tools only**: list events, search, check conflicts, look up a travel time, and
`propose_*` — which renders the same confirm card and writes nothing. No write
tool is ever exposed to the model, so every human-in-the-loop guarantee survives
unchanged; what changes is that the model may call several read tools before it
decides what to put in front of a person.

The plan is to build it on a branch, run it alongside the current bot, and pick
the winner by using both rather than by arguing. I don't know which will win. It
is the right test of §1 precisely because that section could turn out to be wrong.

---

*Built July–October 2026. Ran in production for a household until the project was closed.*
