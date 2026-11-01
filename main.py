import asyncio
import functools
import json
import logging
import os
import datetime
import re

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

load_dotenv()

import calendar_tools
import db
import errors
import event_parser
import gmail_tools
import permissions
import places
import roster
from event_parser import interpret
from vision_tools import parse_event_from_image

# Attributes every stdlib LogRecord already has - anything else on a record
# came from a caller's `extra={...}` and should be surfaced as its own JSON
# field instead of getting silently dropped.
_STANDARD_LOG_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


# Env vars whose values must never appear in a log line. Exception tracebacks
# are the real leak risk: HTTP client errors can embed request URLs, and the
# Telegram Bot API URL contains the bot token verbatim.
_SECRET_ENV_VARS = ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"]


def _redact_secrets(text: str) -> str:
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        if value:
            text = text.replace(value, f"***{var}***")
    return text


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, so operational logs can be
    filtered/searched by tooling instead of only grepped as free text.
    Known secret values are redacted from every line, whatever their source."""

    def format(self, record):
        payload = {
            "timestamp": datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_LOG_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return _redact_secrets(json.dumps(payload, default=str))


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
logger = logging.getLogger(__name__)

# basicConfig sets the ROOT level, so every library inherits INFO - and httpx
# logs a line per request. Long-polling asks Telegram for updates every ~10
# seconds forever, so an idle bot wrote ~8,600 JSON log lines a day, all of
# them saying nothing happened. On a 10GB VM that is a slow disk-fill, and a
# full disk stops SQLite writing, which stops EVERY message with "Something
# went wrong on my end" (2026-08-09). Warnings and errors still come through.
for _noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

READ_ONLY_REPLY = "You have read-only access — you can ask what's on, but not change the calendar."

# The single source of truth for what commands exist: used to build /help, and
# registered with Telegram so typing "/" shows a menu. Adding a command here
# is all it takes for it to appear in both.
# (name, description, admin_only)
COMMANDS = [
    ("help", "Show what I can do", False),
    ("upcoming", "The next few events — add a name to narrow it", False),
    ("pickups", "Who's collecting whom this week", False),
    ("usage", "Token usage and budget", True),
    ("audit", "Recent changes and who made them", True),
    ("hints", "What I've learned from your choices", True),
    ("travel", "How long I think each journey takes", True),
    ("digest", "What confused me lately", True),
]

_PLAIN_WORDS_HELP = (
    "I manage your family calendar. Just tell me in plain words:\n\n"
    "• *Add:* \"swimming Saturday 4-5pm\" or \"mom visiting Sunday\"\n"
    "• *Several dates:* \"end school 17, 24 and 31 Aug\" or \"piano every Tuesday 4pm for 6 weeks\"\n"
    "• *Ask:* \"what's on tomorrow?\"\n"
    "• *Change:* \"move piano to 6pm\"\n"
    "• *Remove:* \"cancel the dentist appointment\" — or just \"cancel Child_1's thing today\"\n"
    "• Or send a *screenshot* of a schedule to add it."
)


def help_text_for(telegram_id) -> str:
    """Commands the caller can actually use. Admin-only ones are hidden from
    everyone else rather than shown-and-refused - listing a command that will
    silently ignore you is worse than not listing it."""
    is_admin = permissions.can(telegram_id, "audit")
    lines = [_PLAIN_WORDS_HELP, "", "*Commands:*"]
    lines += [
        f"/{name} — {description}"
        for name, description, admin_only in COMMANDS
        if is_admin or not admin_only
    ]
    if calendar_tools.is_multi_calendar_configured():
        names = sorted(set(calendar_tools.calendar_names()))
        if names:
            example = names[0]
            lines.append("")
            lines.append(
                f"_e.g._ `/upcoming {example.lower()}` for just {example}, "
                f"or `/upcoming {example.lower()} school` for one calendar."
            )
    if is_admin:
        # Last, and after the /upcoming example, so that example stays next to
        # the command list it belongs to. The travel subcommands are the only
        # typed syntax in the bot - every other path is plain words or a
        # button - so they are the only thing here anyone has to remember.
        lines += ["", travel_help_text()]
    return "\n".join(lines)

RATE_LIMIT_REPLY = "You've made a lot of changes in a short time — please wait a few minutes and try again."
EXPIRED_REPLY = "That request expired — please try again."
# What a failure says is decided in errors.py, not here: see errors.diagnose.

# Generous for a family (nobody legitimately edits the calendar 15 times in
# 10 minutes), but a hard stop against a bad parse, a compromised account,
# or an injection attempt spamming dozens of writes.
WRITE_RATE_LIMIT = 15
WRITE_RATE_WINDOW_MINUTES = 10

# Every message and photo costs an LLM call, whether or not it leads to a
# write - so the write limit above does nothing to bound spend. This is the
# only uncapped cost on a 24/7 host: a compromised authorised account, or a
# loop of retries, could run up the bill unattended. Higher than the write
# limit because reading and asking are cheap and common.
LLM_RATE_LIMIT = 30
LLM_RATE_WINDOW_MINUTES = 10
BUSY_REPLY = "I've handled a lot of requests in the last few minutes — please give me a moment and try again."

# How long metering rows are kept. Long enough for /usage's 30-day view and
# the month-to-date budget, not so long the table grows unbounded on a 1 GB VM.
USAGE_RETENTION_DAYS = 90

# The ceiling for a CALENDAR MONTH (resets at 00:00 UTC on the 1st - i.e.
# 8am Singapore, which is why the number appears to reset mid-morning).
#
# Denominated in TOKENS rather than dollars on purpose: token counts come
# straight from the API response, so the cutoff cannot misfire because a
# hardcoded price went stale.
#
# 3.5M ~= 2,300 messages/month at the ~1,500 tokens/message measured after the
# 2026-08-04 model switch (a flat ~1,480 of prompt and only ~26 of output;
# screenshots cost more). That is roughly 75/day across the family - it was
# ~35/day before the switch, since gpt-5-nano's reasoning tokens more than
# doubled the per-message cost. Chosen as a runaway detector rather than a cost
# control - at these prices a month costs well under a dollar - so it should
# sit far above normal use and still catch a loop within the hour. Raise it
# from the VM's .env without touching code.
MONTHLY_TOKEN_BUDGET = int(os.environ.get("MONTHLY_TOKEN_BUDGET") or 3_500_000)
BUDGET_WARN_FRACTION = 0.8
BUDGET_EXCEEDED_REPLY = (
    "I've hit my usage limit for this month, so I can't read new messages right now. "
    "Parent_1 has been told and can lift it."
)

# Display only - NEVER used for the cutoff. ⚠️ These are USD per 1M tokens for
# the model in event_parser.MODEL and are NOT verified against OpenAI's current
# pricing; check before trusting any figure /usage prints. Being wrong here
# makes a number look off, nothing more, which is exactly why the budget counts
# tokens instead. Update these if OPENAI_MODEL is changed.
PRICE_PER_MILLION_TOKENS = {"input": 0.40, "output": 1.60}

# Telegram offers each photo in several resolutions. The largest is often
# 4000px, which a vision model gains nothing from while costing more and
# taking longer to upload. Prefer the biggest variant under this size.
MAX_PHOTO_BYTES = 1_500_000
# Nothing above this is downloaded at all, whatever variants exist.
HARD_PHOTO_LIMIT_BYTES = 8_000_000


# Parent_1 hears about a problem that needs him at most this often per kind, so a
# family member trying five times in a row produces one alert, not five.
ADMIN_ALERT_COOLDOWN = datetime.timedelta(hours=1)
_last_admin_alert = {}

_ROLE_WORDS = {"editor": "the editor", "reader": "a reader"}


def _is_admin(user_id) -> bool:
    return user_id is not None and permissions.get_role(user_id) == "admin"


def _error_reply_for(exc, user_id=None, ref="") -> str:
    """What to tell the person whose request failed. The admin gets the
    technical detail and the fix; everyone else gets plain words and whether
    retrying will help. See errors.py for why this is not one fixed string."""
    diagnosis = errors.diagnose(exc)
    admin = _is_admin(user_id)
    text = diagnosis.for_admin() if admin else diagnosis.reply
    return errors.with_ref(text, diagnosis, ref, admin)


async def _alert_admin(bot, diagnosis, ref, where, user_id) -> None:
    """Tell Parent_1 about a failure that needs him, when it happened to someone
    else - he already saw the detailed version if it happened to him.

    Plain text, never Markdown: the detail includes raw exception text, and a
    stray underscore in it would make the alert itself fail to send."""
    if not diagnosis.alert or _is_admin(user_id):
        return
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
    if not admin_id:
        return
    key = f"{diagnosis.kind}:{diagnosis.admin_reply[:40]}"
    now = datetime.datetime.now(datetime.timezone.utc)
    last = _last_admin_alert.get(key)
    if last is not None and now - last < ADMIN_ALERT_COOLDOWN:
        return
    _last_admin_alert[key] = now
    who = _ROLE_WORDS.get(permissions.get_role(user_id) or "", "someone")
    text = (f"⚠️ Something just failed for {who} ({where}).\n\n"
            + errors.with_ref(diagnosis.for_admin(), diagnosis, ref, True))
    try:
        await bot.send_message(chat_id=int(admin_id), text=text)
    except Exception:
        logger.exception("Could not alert the admin", extra={"error_kind": diagnosis.kind})


async def _explain_failure(bot, exc, user_id, where, lead_not_done=None, lead_unsure=None):
    """Log a failure with a reference, alert Parent_1 if it needs him, and return
    the text to show. With leads, it is worded as a failed write or send:
    "didn't" only when the error proves nothing happened, otherwise "may not
    have", because retrying a write that DID land makes a duplicate."""
    diagnosis = errors.diagnose(exc)
    ref = errors.new_ref()
    logger.error(
        "Request failed", exc_info=exc,
        extra={"where": where, "user_id": user_id, "ref": ref, "error_kind": diagnosis.kind},
    )
    admin = _is_admin(user_id)
    if lead_not_done:
        text = errors.failure_reply(lead_not_done, lead_unsure, diagnosis, for_admin=admin)
    else:
        text = diagnosis.for_admin() if admin else diagnosis.reply
    await _alert_admin(bot, diagnosis, ref, where, user_id)
    return errors.with_ref(text, diagnosis, ref, admin)


def safe_handler(func):
    """Catch any unhandled exception from a top-level handler, log it with
    context, and tell the user something true instead of leaving them with
    silence - or with "please try again" when trying again cannot work."""

    @functools.wraps(func)
    async def wrapper(update, context):
        try:
            await func(update, context)
        except Exception as exc:
            chat = update.effective_chat
            user = update.effective_user
            user_id = user.id if user else None
            text = await _explain_failure(context.bot, exc, user_id, func.__name__)
            if chat is not None:
                try:
                    await context.bot.send_message(chat.id, text)
                except Exception:
                    logger.exception("Also failed to notify user of the error", extra={"chat_id": chat.id})

    return wrapper


# Pending state (clarification text / confirm card / amend picker) now lives
# in SQLite via db.py, not in-memory dicts - it survives a bot restart.


# ----- formatting helpers -----

# Telegram's legacy parse_mode="Markdown" treats these 4 characters as
# formatting commands. Model/user-derived text (event titles, locations)
# must never reach a Markdown-rendered message unescaped, or it can break
# or hijack the confirm card's formatting - undermining the human-confirms-
# every-write safety model this whole project depends on.
_MARKDOWN_SPECIAL_CHARS = "_*`["


def _escape_markdown(text: str) -> str:
    for ch in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(ch, "\\" + ch)
    return text


def _format_when(event: dict) -> str:
    start = event.get("start", {})
    if start.get("date"):
        d = datetime.date.fromisoformat(start["date"])
        return f"{d.strftime('%a %d %b')} (all day)"
    start_dt = datetime.datetime.fromisoformat(start["dateTime"])
    end_dt = datetime.datetime.fromisoformat(event["end"]["dateTime"])
    return f"{start_dt.strftime('%a %d %b, %H:%M')}-{end_dt.strftime('%H:%M')}"


def _event_label(event: dict) -> str:
    return f"{event.get('summary', '(no title)')} — {_format_when(event)}"[:60]


def _proposed_when(fields: dict) -> str:
    if fields.get("all_day"):
        return f"{fields['date'].strftime('%a %d %b')} (all day)"
    return f"{fields['start'].strftime('%a %d %b, %H:%M')}-{fields['end'].strftime('%H:%M')}"


# What the "no" button says, per action. A bare "Cancel" is genuinely
# ambiguous when the request WAS a cancellation: on a delete card, "Cancel"
# reads as "yes, cancel the appointment" when it means "abandon this request".
# Naming the actual outcome removes the guesswork.
#
# The ✅/❌ pair carries the go/no-go, since Telegram gives no way to colour a
# button - the Bot API has no styling for them at all, they just follow the
# user's theme, so the tick and cross are the only colour available on a
# keyboard. That is exactly why the wording still has to stand on its own:
# "❌ Cancel" on a delete card reads as "yes, cancel the appointment".
_DECLINE_LABEL = {
    "create": "❌ Don't add",
    "amend": "❌ Don't change",
    "delete": "❌ Don't delete anything",
}


def _confirm_keyboard(kind="create", journey=None, carers=None):
    """Confirm / edit / decline. The edit button exists so a nearly-right
    proposal can be corrected in place - before it, the only way to fix a
    wrong time was to cancel and retype the whole request.

    `journey` adds a row of travel-time buckets BELOW the decision buttons.
    `carers` adds a row of pickup-name buttons for a Saturday event nobody has
    been named for. Both go below the decision buttons and never replace them:
    a question the bot has must not stand between the family and a write they
    came here to make. Answering either re-renders this same card with the
    answer applied; ignoring it leaves the card exactly as confirmable.
    Only one of the two is ever shown at once (see _show_create_confirm).
    """
    rows = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm"),
            InlineKeyboardButton("✏️ Change something", callback_data="edit"),
        ],
        [InlineKeyboardButton(_DECLINE_LABEL.get(kind, "❌ Cancel"), callback_data="cancel")],
    ]
    if carers:
        rows.append([
            InlineKeyboardButton(name, callback_data=f"pk:{i}")
            for i, name in enumerate(carers)
        ])
    if journey:
        rows.append([
            InlineKeyboardButton(label, callback_data=f"trav:{i}")
            for i, (label, _) in enumerate(places.TRAVEL_BUCKETS)
        ])
    return InlineKeyboardMarkup(rows)


def _journey_question(journey) -> str:
    """The one-line ask that goes with the bucket row.

    Phrased as a question about the ROUTE, not about this event: the answer is
    stored against the pair of places and will be reused for every future event
    that makes the same trip, so it should not read as being about Tuesday.

    "By car" is not decoration. Every stored number is a car time, and the
    public-transport penalty is added on top per carer - so an answer given
    with a bus journey in mind would be counted twice.
    """
    a, b = journey
    return (
        f"🚗 How long does {_escape_markdown(places.label_for(a))} → "
        f"{_escape_markdown(places.label_for(b))} take *by car*? "
        "_I'll remember it, and add time for whoever travels by bus._"
    )


# ----- command handlers -----

@safe_handler
async def start(update, context):
    telegram_id = update.effective_user.id
    if permissions.get_role(telegram_id) is None:
        logger.info("Ignoring command from unknown Telegram ID", extra={"handler": "start", "telegram_id": telegram_id})
        return
    await update.message.reply_text(help_text_for(telegram_id), parse_mode="Markdown")


def _resolve_scope(args):
    """Turn `/upcoming [child] [category]` arguments into calendar keys.

    Returns (keys, label, error). keys=None means "everything", which is what
    no arguments should do - narrowing is opt-in so the plain command keeps
    behaving as it always has.
    """
    if not args:
        return None, None, None

    child = calendar_tools.resolve_child_name(args[0])
    if child is None:
        names = ", ".join(sorted(set(calendar_tools.calendar_names())))
        return None, None, f"I don't know \"{args[0]}\". Try one of: {names}."

    keys = calendar_tools.calendar_keys_for_child(child)
    label = child.title()

    if len(args) > 1:
        wanted = args[1].strip().lower()
        narrowed = [k for k in keys if calendar_tools.category_for_key(k) == wanted]
        if not narrowed:
            available = calendar_tools.categories_for_child(child)
            if not available:
                return None, None, f"{label} doesn't have separate calendars by category."
            return None, None, (
                f"I don't know \"{args[1]}\" for {label}. Try: {', '.join(available)}."
            )
        keys, label = narrowed, f"{label} — {wanted}"

    return keys, label, None


@safe_handler
async def upcoming(update, context):
    if permissions.get_role(update.effective_user.id) is None:
        logger.info("Ignoring command from unknown Telegram ID", extra={"handler": "upcoming", "telegram_id": update.effective_user.id})
        return

    keys, label, error = _resolve_scope(context.args or [])
    if error:
        await update.message.reply_text(error)
        return

    if calendar_tools.is_multi_calendar_configured():
        events = await asyncio.to_thread(calendar_tools.list_upcoming_events_all, 10, keys)
    else:
        events = await asyncio.to_thread(calendar_tools.list_upcoming_events)

    empty = f"Nothing coming up for {label}." if label else "Nothing coming up."

    # A bare /upcoming asks for everyone, so it had no name to put in the
    # header - but when everything that came back belongs to one child, the
    # header should say so. It only does that alongside naming who has
    # nothing: "Coming up - Child_1" on its own reads as a filter, and Child_2 would
    # look excluded when she is simply free.
    footer = None
    if not label and events and isinstance(events[0], tuple):
        shown = _children_in(events)
        if len(shown) == 1:
            label = shown[0]
            silent = [n for n in calendar_tools.calendar_names() if n not in shown]
            if silent:
                footer = f"Nothing coming up for {', '.join(silent)}."

    await _reply_event_list(
        update,
        events,
        empty,
        header=f"Coming up — {label}" if label else "Coming up",
        footer=footer,
    )


PICKUPS_DAYS_AHEAD = 7


@safe_handler
async def pickups(update, context):
    """Who is collecting whom, and anything that needs sorting out.

    The deliberate check. Clash warnings otherwise only appear on a confirm
    card, which means they only fire for changes made THROUGH the bot - an
    event added straight into Google Calendar (which both parents can do)
    would never be checked. This is how those get seen.
    """
    telegram_id = update.effective_user.id
    if permissions.get_role(telegram_id) is None:
        logger.info("Ignoring command from unknown Telegram ID", extra={"handler": "pickups", "telegram_id": telegram_id})
        return

    today = datetime.datetime.now(calendar_tools.TIMEZONE).date()
    end = today + datetime.timedelta(days=PICKUPS_DAYS_AHEAD)
    keys = list(calendar_tools._calendars())
    events = await asyncio.to_thread(calendar_tools.list_events_range_across, today, end, keys)
    found = roster.pickups_from_events(events)

    if not found:
        await update.message.reply_text(f"No pickups needed in the next {PICKUPS_DAYS_AHEAD} days.")
        return

    lines = [f"*Pickups to {end.strftime('%a %d %b')}*"]
    current_day = None
    for pickup in found:
        day = pickup.when.date()
        if day != current_day:
            lines.append(f"\n_{day.strftime('%a %d %b')}_")
            current_day = day
        who = pickup.person or "⚠️ nobody rostered"
        activity, _ = event_parser.split_title(pickup.summary, pickup.child)
        lines.append(
            f"• {roster._clock(pickup.when)} — {_escape_markdown(pickup.child)}: "
            f"{_escape_markdown(activity or pickup.summary)} → *{_escape_markdown(who)}*"
        )

    clashes = roster.find_clashes(found)
    if clashes:
        lines.append("\n*Needs sorting out:*")
        lines += [f"⚠️ {_escape_markdown(roster.describe_clash(c))}" for c in clashes]
        # /pickups has no buttons at all - unlike a confirm card, there is no
        # ✏️ Change something to point at, so the hint has to name the actual
        # mechanism: message the bot and it re-parses as an amend.
        lines.append('\n_To fix one: message e.g. "change child_2\'s piano pickup to Grandparent_1"._')

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


_AUDIT_ACTION_VERBS = {
    "create": "created", "delete": "deleted", "amend": "amended",
    # The 2nd..Nth event of a confirmed batch - logged for the trail, not
    # counted toward the write rate limit (see db.WRITE_ACTIONS).
    "create_series": "created", "amend_series": "amended",
}


@safe_handler
async def audit(update, context):
    telegram_id = update.effective_user.id
    if not permissions.can(telegram_id, "audit"):
        logger.info("Ignoring /audit from non-admin", extra={"telegram_id": telegram_id})
        return
    entries = await asyncio.to_thread(db.recent_audit, 10)
    if not entries:
        await update.message.reply_text("No audit entries yet.")
        return
    lines = []
    for e in entries:
        mark = "✅" if e["status"] == "success" else "⚠️"
        verb = _AUDIT_ACTION_VERBS.get(e["action"], e["action"])
        lines.append(f"{mark} {e['timestamp']} — {e['role']} ({e['telegram_id']}) {verb} \"{e['summary']}\"")
    await update.message.reply_text("\n".join(lines))


def _format_event_line(event, child_name=None):
    label = f" [{child_name}]" if child_name else ""
    return f"• {event.get('summary', '(no title)')} — {_format_when(event)}{label}"


# A glyph per category, instead of "[Child_1 — School]" on every single line.
# The tag was the longest thing on most rows and the least informative: in a
# list already filtered to one child it repeated the child's name too.
#
# Chosen for CONTRAST, not just meaning. The first set (🎒 🎵 📌) read as three
# shades of the same red on a dark-theme phone and the musical note was nearly
# invisible against it - an icon you have to squint at is worse than the text
# tag it replaced. These differ in hue AND silhouette (blue rectangle, yellow
# star, purple circle), so they stay distinguishable in either theme and
# without colour vision. "others" gets a deliberately neutral chip: it is a
# catch-all by design, and any picture would claim more than it knows.
_CATEGORY_ICON = {"school": "📘", "enrichment": "⭐", "others": "🟣"}

_NOBODY_ROSTERED = "⚠️ nobody"


def _pickup_person(calendar_key, event):
    """Who collects the child from this event, or None if it needs no pickup.

    Runs the single event through the same roster.pickups_from_events that
    /pickups uses rather than reading `Pickup:` directly, so the two commands
    can never disagree about who is collecting whom - and so the three cases
    that produce no pickup at all (all-day, no-school, unknown child) are
    skipped here by the same test, not by a second copy of it.
    """
    found = roster.pickups_from_events([(calendar_key, event)])
    if not found:
        return None
    return found[0].person or _NOBODY_ROSTERED


def _children_in(events) -> list:
    """Distinct child names in a (key, event) list, in calendar order."""
    names = []
    for key, _ in events:
        name = calendar_tools.child_display_name(key)
        if name and name not in names:
            names.append(name)
    return names


def _grouped_event_lines(events) -> list:
    """A day-grouped list, with everything repeated stripped out.

    The flat version restated the child, the calendar and the date on every
    row, and printed "14:45-15:00" next to a title that already said "3pm".
    Seven events filled a screen and none of it could be skimmed. Here the
    date is a heading, the calendar is one glyph, and the child appears only
    when more than one is in the list.

    The clock comes from the TITLE, not from the event's times, so the line
    can never state a different time from the title beside it.

    Each row also carries who is collecting, because the alternative was
    reading this list and then running /pickups to answer the one question the
    list had just raised.
    """
    show_child = len(_children_in(events)) > 1

    lines, current_day = [], None
    for key, event in events:
        child = calendar_tools.child_display_name(key)
        day = calendar_tools.event_start_date(event)
        if day != current_day:
            lines.append(("\n" if lines else "") + f"*{day.strftime('%a %d %b')}*")
            current_day = day

        summary = event.get("summary", "(no title)")
        activity, gate = event_parser.split_title(summary, child)
        icon = _CATEGORY_ICON.get(calendar_tools.category_for_key(key) or "", "•")

        # The clock leads and is set off by spacing rather than a separator,
        # so the eye can run down the times in a column.
        # A range title ("4-5pm") is shown here as its start alone, so
        # the column does not go ragged next to the single clocks around it.
        # The title keeps the full range - this is the scanning view, not the
        # record.
        clock = event_parser.range_lead_clock(
            event_parser.title_clock(summary, child))
        lead = f"*{_escape_markdown(clock)}*  " if clock else ""

        bits = []
        if show_child and child:
            bits.append(_escape_markdown(child))
        bits.append(_escape_markdown(activity or summary))
        if gate:
            # "Gate 2", not a bare "2". split_title returns the identifier
            # alone, which means nothing on its own line.
            bits.append(f"_Gate {_escape_markdown(str(gate))}_")

        # Same "→ person" shape /pickups uses, so the two read as one idea.
        person = _pickup_person(key, event)
        tail = f" → *{_escape_markdown(person)}*" if person else ""

        lines.append(f"{icon} {lead}" + " · ".join(bits) + tail)
    return lines


async def _reply_event_list(update, events, empty_msg, header=None, footer=None):
    """events is a flat list of event dicts (single calendar) or a list of
    (calendar_key, event) tuples (multi-calendar)."""
    if not events:
        await update.message.reply_text(empty_msg)
        return

    if not isinstance(events[0], tuple):
        # Single-calendar setups have no key to group or label by, so they
        # keep the original one-line-per-event rendering.
        await update.message.reply_text("\n".join(_format_event_line(e) for e in events))
        return

    lines = ([f"*{_escape_markdown(header)}*"] if header else []) + _grouped_event_lines(events)
    if footer:
        lines.append(f"\n_{_escape_markdown(footer)}_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def _estimated_cost(prompt_tokens, completion_tokens) -> float:
    """USD, from a hardcoded price table that may be out of date - see
    PRICE_PER_MILLION_TOKENS. Never used for the budget cutoff."""
    return (
        prompt_tokens * PRICE_PER_MILLION_TOKENS["input"]
        + completion_tokens * PRICE_PER_MILLION_TOKENS["output"]
    ) / 1_000_000


@safe_handler
async def usage(update, context):
    telegram_id = update.effective_user.id
    if not permissions.can(telegram_id, "audit"):
        logger.info("Ignoring /usage from non-admin", extra={"telegram_id": telegram_id})
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    windows = {
        "Today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "7 days": now - datetime.timedelta(days=7),
        "30 days": now - datetime.timedelta(days=30),
    }
    lines = []
    for label, since in windows.items():
        stats = await asyncio.to_thread(db.usage_since, since)
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(stats["by_kind"].items())) or "no calls"
        cost = _estimated_cost(stats["prompt_tokens"], stats["completion_tokens"])
        lines.append(
            f"{label}: {stats['calls']} calls ({kinds}), "
            f"{stats['total_tokens']:,} tokens, ~${cost:.2f}"
        )

    month = await asyncio.to_thread(db.tokens_this_month)
    pct = month / MONTHLY_TOKEN_BUDGET if MONTHLY_TOKEN_BUDGET else 0
    lines.append("")
    lines.append(f"This month: {month:,} of {MONTHLY_TOKEN_BUDGET:,} tokens ({pct:.0%})")
    if pct >= 1:
        lines.append("⚠️ Budget spent — everyone but you is blocked.")
    elif pct >= BUDGET_WARN_FRACTION:
        lines.append("⚠️ Approaching the budget.")
    # Said plainly rather than buried: an unverified price makes the dollar
    # figures indicative only, which is why the cap counts tokens.
    lines.append("$ figures are estimates from a hardcoded price table.")

    await update.message.reply_text("\n".join(lines))


_FRICTION_LABELS = {
    "clarify": "had to ask a question",
    "fallback": "couldn't match the words used",
    "not_found": "found nothing to act on",
    "cancelled": "proposal was cancelled",
    "not_understood": "couldn't tell what was wanted",
}

# The reply for action=unknown. Names the four things the bot does and gives one
# example each, rather than dumping the whole /help text at someone who may
# have just mistyped. Logged as friction so /digest shows how often it happens.
_NOT_UNDERSTOOD_REPLY = (
    "I didn't catch what you'd like me to do. I can:\n"
    "• *add* an event — \"swimming Saturday 4pm\"\n"
    "• *change* or *cancel* one — \"move piano to 5pm\", \"cancel the dentist\"\n"
    "• *answer* a question — \"what's on tomorrow?\", \"who's collecting Child_1 Friday?\"\n"
    "• read a *screenshot* of a schedule\n\n"
    "Try rephrasing, or /help for more."
)


async def _note_friction(telegram_id, kind: str, detail: str):
    """Record a moment the bot had to ask, guess or give up. Best-effort: a
    failure here must never surface to the user, since it happens on a path
    that is already going less than smoothly."""
    try:
        await asyncio.to_thread(db.log_friction, telegram_id, kind, detail)
    except Exception:
        logger.exception("Could not record friction")


@safe_handler
async def digest(update, context):
    """What tripped the bot up lately, so the wording can be fixed
    deliberately. On demand rather than pushed weekly: a scheduled job would
    need APScheduler or PTB's job-queue extra, and neither is worth a new
    dependency on a 1 GB VM for a report you can ask for."""
    telegram_id = update.effective_user.id
    if not permissions.can(telegram_id, "audit"):
        logger.info("Ignoring /digest from non-admin", extra={"telegram_id": telegram_id})
        return

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
    rows = await asyncio.to_thread(db.friction_since, since)
    if not rows:
        await update.message.reply_text("Nothing confused me in the last 7 days.")
        return

    lines = ["*What confused me (last 7 days):*"]
    for kind, detail, count in rows:
        label = _FRICTION_LABELS.get(kind, kind)
        lines.append(f"• {_escape_markdown(detail)} — {label} ({count}×)")
    lines.append("")
    lines.append("_Confirming these once teaches me the wording; see /hints._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@safe_handler
async def hints(update, context):
    """What the bot has learned, and a way to undo it. Both halves matter:
    learned phrases feed the model prompt, so they have to be inspectable and
    reversible rather than an invisible accumulation."""
    telegram_id = update.effective_user.id
    if not permissions.can(telegram_id, "audit"):
        logger.info("Ignoring /hints from non-admin", extra={"telegram_id": telegram_id})
        return

    if context.args and context.args[0].lower() == "reset":
        removed = await asyncio.to_thread(db.clear_hints)
        await update.message.reply_text(f"Forgot {removed} learned hint(s).")
        return

    lines = []
    for kind, label in (("category", "Category"), ("gate", "Gate")):
        learned = await asyncio.to_thread(db.top_hints, kind)
        if learned:
            lines.append(f"*{label}:*")
            lines += [f"• {_escape_markdown(p)} → {v} ({n}×)" for p, v, n in learned]
    if not lines:
        await update.message.reply_text(
            "I haven't learned anything yet. I pick up wording from events you confirm."
        )
        return
    lines.append("")
    lines.append("_/hints reset_ to forget all of it.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# Offered when correcting a journey. Deliberately finer than
# places.TRAVEL_BUCKETS, which is coarse because it is answered in a hurry
# mid-write; editing the table is a considered act and 20-vs-25 is exactly the
# distinction being made.
_TRAVEL_CHOICES = [5, 10, 15, 20, 25, 30, 35, 45, 60]

# Journeys listed before /travel starts hiding the rest. Sized off Telegram's
# 4096-character message limit at roughly 40 characters a line, with room for
# the header and footer. The picker can still reach anything hidden.
_TRAVEL_LIST_MAX = 80


def _load_user_places():
    """Push the family's added places into the places module.

    places.py never reads the database - that is what keeps its matching rules
    testable without one - so somebody has to hand them over. Called at startup
    and again after each add, so a new centre resolves without a restart.
    """
    try:
        places.set_user_places(db.all_user_places())
    except Exception:
        # A missing overlay costs recognition of a few places, not a write.
        logger.exception("Could not load user places")


def _place_choices() -> list:
    """Every place that can be picked, as a stable sorted list of keys.

    Stable matters: the picker puts INDEXES into this list in callback_data, so
    two taps a minute apart must mean the same places. Derived rather than
    stored - the schools come from the configured children, and anything the
    family has already timed is included so a place can be corrected even if it
    only ever arrived from an event's location field.
    """
    keys = set(places.known_places())
    keys.update(places.school_key(c) for c in calendar_tools.calendar_names())
    for pair in _stored_pairs():
        keys.update(pair)
    return sorted(k for k in keys if k)


def _stored_tables():
    """(answers, overrides) in the order stored_minutes takes them."""
    overrides, answers = db.travel_times_by_source()
    return answers, overrides


def _stored_pairs() -> list:
    try:
        return list(db.all_travel_times())
    except Exception:
        logger.exception("Could not read travel times")
        return []


def _place_candidates(text) -> list:
    """Places `text` could mean, best-first.

    Exact matches on the key or the label win outright. Failing that, a
    substring match - because people say "school" and "Studio_2", not
    "child_1-school" and "ballet-studio_2", and demanding the key on a phone
    keyboard is the friction that stops a table being maintained.

    Returns a LIST because "school" genuinely means two different places in
    this family, at different distances. That has to be asked about, not
    resolved by picking whichever sorts first.
    """
    wanted = places.pair_key(text, text)[0]  # reuse the module's normalisation
    if not wanted:
        return []
    choices = _place_choices()
    exact = [k for k in choices if k == wanted or places.label_for(k).casefold() == wanted]
    if exact:
        return exact
    return [k for k in choices
            if wanted in k or wanted in places.label_for(k).casefold()]


def _resolve_place(text):
    """The one place `text` means, or None if it names zero or several."""
    candidates = _place_candidates(text)
    return candidates[0] if len(candidates) == 1 else None


def _travel_lines(overrides, answers) -> list:
    """The table as it actually applies, marking anything that is not simply
    the seeded value - so a figure somebody edited is visibly theirs."""
    merged = {}
    for pair, minutes in places.TRAVEL_MINUTES.items():
        merged[pair] = (minutes, "")
    for pair, minutes in answers.items():
        merged.setdefault(pair, (minutes, " _(answered)_"))
    for pair, minutes in overrides.items():
        merged[pair] = (minutes, " _(edited)_")
    return [
        f"• {_escape_markdown(places.label_for(a))} ↔ "
        f"{_escape_markdown(places.label_for(b))} — {minutes} min{tag}"
        for (a, b), (minutes, tag) in sorted(merged.items())
    ]


def _travel_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Edit a journey", callback_data="tvedit"),
    ]])


def _example_pair():
    """Two real place keys for the help examples.

    Built from the live table rather than hardcoded, so an example can always
    be copied straight out and run - a help text demonstrating places this
    family does not have would be worse than none.
    """
    known = places.known_places()
    choices = _place_choices()
    schools = [k for k in choices if k.endswith("-school")]
    # Prefer a place both children use. A school-to-somewhere journey is the
    # everyday shape, and pairing one child's school with the OTHER child's
    # ballet studio would be a real journey nobody ever makes - a confusing
    # thing to lead with.
    shared = [k for k in choices
              if not k.endswith("-school") and not (known.get(k) or {}).get("child")]
    others = shared or [k for k in choices if not k.endswith("-school")]
    return (schools[0] if schools else (choices[0] if choices else "child_1-school"),
            others[0] if others else "centre_1")


def travel_help_text() -> str:
    """The typed syntax, spelled out. Shared by /help and `/travel help` so
    the two can never drift into describing different commands."""
    a, b = _example_pair()
    a_label, b_label = places.label_for(a), places.label_for(b)
    return "\n".join([
        "*Editing travel times* — all times are *by car*; "
        f"I add {places.PUBLIC_TRANSPORT_PENALTY_MINUTES} min for whoever takes the bus.",
        "",
        "Easiest way: *just tell me* —",
        f"• \"{_escape_markdown(a_label)} to {_escape_markdown(b_label)} takes 25 minutes\"",
        f"• \"{_escape_markdown(b_label)} is 20 minutes away\" — I'll ask from where",
        "• \"centre_2 is a new place\"",
        "• \"forget how long that takes\"",
        "",
        "Or /travel and tap ✏️ *Edit a journey* to pick from buttons.",
        "",
        "Or type the exact form:",
        f"• `/travel set {a} {b} 25` — change a journey",
        f"• `/travel forget {a} {b}` — undo, back to the built-in value",
        "• `/travel place Centre_2` — teach me a new place",
        "• `/travel place Centre_2 | chess, chess club` — with aliases",
        "• `/travel place Centre_2 for Child_1` — only one child goes there",
        "",
        "_Place names work either way — `Child_1's school` or `child_1-school`._",
    ])


def _index_arg(data, position, choices, telegram_id):
    """One validated index out of callback_data, or None.

    callback_data is client-controlled, the same reason cat:/kid:/create_opt:
    validate theirs. Unchecked, an out-of-range value would write a travel time
    against a journey the buttons never offered - or wrap to a real-but-
    different one, which is worse because it looks like it worked.
    """
    try:
        index = int(data.split(":")[position])
    except (IndexError, ValueError):
        return None
    if not 0 <= index < len(choices):
        logger.info("Rejected out-of-range place index",
                    extra={"telegram_id": telegram_id, "raw": data,
                           "place_count": len(choices)})
        return None
    return index


def _in_rows(buttons, per_row):
    rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# The two place pickers are separate functions with LITERAL callback prefixes,
# rather than one parameterised by prefix. test_seams scans this source for
# callback_data strings and checks each has a handler; a f"{prefix}:{i}" built
# from a variable is invisible to that check, which would quietly cost the one
# test that catches a button leading nowhere.
def _from_keyboard(choices):
    """Places, two per row. Nine of them today, so no paging - if the family
    ever has enough places for that to be unwieldy, this is where it goes."""
    return _in_rows([
        InlineKeyboardButton(places.label_for(key), callback_data=f"tvfrom:{i}")
        for i, key in enumerate(choices)
    ], 2)


def _to_keyboard(choices, first):
    return _in_rows([
        InlineKeyboardButton(places.label_for(key), callback_data=f"tvto:{first}:{i}")
        for i, key in enumerate(choices) if i != first
    ], 2)


def _minutes_keyboard(i, j):
    return _in_rows([
        InlineKeyboardButton(f"{m} min", callback_data=f"tvset:{i}:{j}:{m}")
        for m in _TRAVEL_CHOICES
    ], 3)


async def _show_travel_table(reply, note=None):
    """The list, with the edit button. `reply` is an awaitable sender so this
    serves both a fresh /travel and a re-render after an edit."""
    overrides, answers = await asyncio.to_thread(db.travel_times_by_source)
    lines = _travel_lines(overrides, answers)
    if not lines:
        await reply(
            "I don't know how long any journey takes yet. I'll ask when two of "
            "a child's events are close enough together for it to matter."
        )
        return

    # Who drives is part of reading these figures, not a separate topic: the
    # same journey is 20 minutes longer for a carer on public transport, so a
    # list of bare numbers would look wrong to whoever knows the drive.
    on_bus = sorted(p.title() for p, mode in places.TRANSPORT.items() if mode == "public")
    if places.DEFAULT_TRANSPORT == "public":
        on_bus.append("anyone I don't know")
    header = "*Journeys* — by car."
    if on_bus:
        header = (f"*Journeys* — by car. Add {places.PUBLIC_TRANSPORT_PENALTY_MINUTES} min for "
                  f"{_escape_markdown(', '.join(on_bus))}.")
    # The list is every PAIR of places, so it grows quadratically: nine places
    # is 36 lines, but fourteen would be 91 and would blow Telegram's 4096
    # character limit - and an over-long message is not truncated by Telegram,
    # it is REJECTED, so /travel would simply stop working with no clue why.
    if len(lines) > _TRAVEL_LIST_MAX:
        hidden = len(lines) - _TRAVEL_LIST_MAX
        lines = lines[:_TRAVEL_LIST_MAX]
        lines.append(f"_…and {hidden} more. Tap ✏️ to see or change a specific one._")

    body = [header] + ([f"_{_escape_markdown(note)}_", ""] if note else []) + lines
    # One short pointer rather than the whole syntax: this message is already
    # long, and the syntax is a screen of its own behind /travel help.
    body += ["", "_Tap ✏️ to change one, or /travel help for the typed commands._"]
    await reply("\n".join(body), reply_markup=_travel_keyboard(), parse_mode="Markdown")


@safe_handler
async def travel(update, context):
    """Every journey the bot thinks it knows, and the ways to correct it.

    These numbers decide whether the family is warned that a child cannot make
    it from one place to the next, so they have to be inspectable and
    reversible for the same reason /hints does - except this table is answered
    into existence one tap at a time, which makes a wrong tap easy and quiet.
    """
    telegram_id = update.effective_user.id
    if not permissions.can(telegram_id, "audit"):
        logger.info("Ignoring /travel from non-admin", extra={"telegram_id": telegram_id})
        return

    args = list(context.args or [])
    sub = args[0].lower() if args else ""

    if sub in ("help", "?"):
        await update.message.reply_text(travel_help_text(), parse_mode="Markdown")
        return
    if sub == "forget":
        await _travel_forget(update, args[1:])
        return
    if sub == "set":
        await _travel_set(update, telegram_id, args[1:])
        return
    if sub == "place":
        await _travel_add_place(update, telegram_id, args[1:])
        return

    async def reply(text, **kwargs):
        await update.message.reply_text(text, **kwargs)

    await _show_travel_table(reply)


async def _travel_forget(update, named):
    """Drop a stored journey.

    Two-stage on purpose. An override and an answer mean different things, and
    so does removing them: dropping an override puts the places.py value back
    in force, while dropping an answer leaves the journey unknown and the bot
    asking again. Reporting "forgotten" for both would hide which happened.
    """
    if len(named) != 2:
        await update.message.reply_text(
            "Name both ends of the journey, e.g. `/travel forget child_1-school centre_1`.",
            parse_mode="Markdown",
        )
        return
    a, b = (_resolve_place(named[0]), _resolve_place(named[1]))
    if not a or not b:
        await update.message.reply_text(_unknown_place_reply(named, a, b), parse_mode="Markdown")
        return

    pair = places.pair_key(a, b)
    dropped = await asyncio.to_thread(db.forget_travel_minutes, pair)
    if not dropped:
        await update.message.reply_text("I had nothing stored for that journey.")
        return
    seeded = places.TRAVEL_MINUTES.get(pair)
    if seeded is not None:
        await update.message.reply_text(
            f"Dropped. Back to {seeded} min from places.py.")
    else:
        await update.message.reply_text("Forgotten. I'll ask again next time it comes up.")


async def _travel_set(update, telegram_id, args):
    if len(args) != 3:
        await update.message.reply_text(
            "Give both ends and the minutes, e.g. `/travel set child_1-school centre_1 25`.",
            parse_mode="Markdown",
        )
        return
    a, b = (_resolve_place(args[0]), _resolve_place(args[1]))
    if not a or not b:
        await update.message.reply_text(_unknown_place_reply(args, a, b), parse_mode="Markdown")
        return
    try:
        minutes = int(args[2])
    except ValueError:
        await update.message.reply_text("The last part should be a number of minutes.")
        return
    if not 0 <= minutes <= 240:
        await update.message.reply_text("That doesn't look like a journey time in minutes.")
        return
    if a == b:
        await update.message.reply_text("That's the same place — no travel needed.")
        return

    await _store_travel_edit(update, telegram_id, a, b, minutes)


async def _store_travel_edit(update, telegram_id, a, b, minutes, sender=None):
    await asyncio.to_thread(
        db.set_travel_minutes, places.pair_key(a, b), minutes, telegram_id, db.SET_BY_HAND
    )
    logger.info("Travel time edited",
                extra={"telegram_id": telegram_id, "a": a, "b": b, "minutes": minutes})
    note = (f"{places.label_for(a)} ↔ {places.label_for(b)} is now {minutes} min "
            f"by car.")
    await _show_travel_table(sender or update.message.reply_text, note=note)


def _unknown_place_reply(named, a, b) -> str:
    missing = [n for n, resolved in zip(named, (a, b)) if not resolved]
    known = ", ".join(places.label_for(k) for k in _place_choices())
    return (f"I don't know {_escape_markdown(', '.join(missing))}. "
            f"I know: {_escape_markdown(known)}.")


# The shortest alias worth having. An alias is matched as a SUBSTRING of an
# event's title, so a two- or three-letter one fires on words that merely
# contain it and silently files events at the wrong place - which is worse
# than not knowing the place at all, because a wrong place produces a
# confident warning about a journey nobody is making.
_MIN_ALIAS = 4

_TRAILING_CHILD_RE = re.compile(r"\s+for\s+(\S+)\s*$", re.IGNORECASE)


def _vet_new_place(label, child, alias_text=""):
    """(key, aliases, error). One rule for both ways a place can be added.

    Typed and spoken creation must agree: a bad alias mis-files events
    whichever route it arrived by, and two copies of this check would
    eventually disagree about which aliases are acceptable.
    """
    label = (label or "").strip()
    if not label:
        return None, None, "That leaves no name for the place."

    key = re.sub(r"[^a-z0-9]+", "-", label.casefold()).strip("-")
    if not key:
        return None, None, "I can't make a name out of that."
    if key in places.PLACES:
        return None, None, (f"{_escape_markdown(places.label_for(key))} is already "
                            f"in places.py.")

    aliases = [a.strip().casefold() for a in (alias_text or "").split(",") if a.strip()]
    aliases = aliases or [label.casefold()]

    # Checked before storing, not after: an alias that matches an existing
    # place would quietly steal events from it, and the family would see a
    # warning about a journey nobody is making.
    for alias in aliases:
        if len(alias) < _MIN_ALIAS:
            return None, None, (
                f"\"{_escape_markdown(alias)}\" is too short to match on — it would "
                f"fire on any title containing those letters. Give me {_MIN_ALIAS}+.")
        clash = places.match_known(alias, child)
        if clash and clash != key:
            return None, None, (
                f"\"{_escape_markdown(alias)}\" already means "
                f"{_escape_markdown(places.label_for(clash))}. Pick another.")
    return key, aliases, None


def _trailing_child(text):
    """('Child_1', 'the rest') if `text` ends in "for Child_1", else (None, text)."""
    match = _TRAILING_CHILD_RE.search(text or "")
    if not match:
        return None, text
    return match.group(1), text[: match.start()]


async def _travel_add_place(update, telegram_id, args):
    """`/travel place <Label> [| alias, alias] [for <child>]`.

    Typed rather than buttoned because a place needs free text, and capturing
    free text would collide with handle_text's routing into the parser.
    """
    raw = " ".join(args).strip()
    if not raw:
        await update.message.reply_text(
            "Name the place, e.g. `/travel place Centre_2` — or with "
            "aliases: `/travel place Centre_2 | chess, academy` — or for "
            "one child: `/travel place Centre_2 for Child_1`.",
            parse_mode="Markdown",
        )
        return

    raw, _, alias_text = raw.partition("|")

    # "for <child>" may trail either half, because both read naturally:
    #   place Centre_2 for Child_1
    #   place Centre_2 | chess for Child_1
    # Checked on the aliases first, since that is where it ends up when both
    # are given. Left unhandled, it would silently become part of an alias.
    named, alias_text = _trailing_child(alias_text)
    if not named:
        named, raw = _trailing_child(raw)

    child = None
    if named:
        child = calendar_tools.resolve_child_name(named)
        if not child:
            await update.message.reply_text(
                f"I don't have a child called {_escape_markdown(named)}.",
                parse_mode="Markdown")
            return

    label = raw.strip()
    key, aliases, error = _vet_new_place(label, child, alias_text)
    if error:
        await update.message.reply_text(error, parse_mode="Markdown")
        return

    await asyncio.to_thread(db.add_user_place, key, label, aliases, child, telegram_id)
    _load_user_places()
    logger.info("Place added", extra={"telegram_id": telegram_id, "key": key})

    scope = f" for {calendar_tools.child_display_name(f'{child}_school') or child.title()}" if child else ""
    await update.message.reply_text(
        f"Added *{_escape_markdown(label)}*{_escape_markdown(scope)}, matching "
        f"{_escape_markdown(', '.join(aliases))}.\n"
        f"I'll ask how long the journeys to it take when they first come up — "
        f"or set one now with `/travel set {_escape_markdown(key)} <b> <min>`.",
        parse_mode="Markdown",
    )


# ----- core routing -----

@safe_handler
async def handle_text(update, context):
    chat_id = update.effective_chat.id
    telegram_id = update.effective_user.id
    role = permissions.get_role(telegram_id)
    if role is None:
        logger.info("Ignoring message from unknown Telegram ID", extra={"handler": "handle_text", "telegram_id": telegram_id})
        return

    allowed, usage_row = await _begin_llm_call(update, telegram_id, "text")
    if not allowed:
        return

    await _show_typing(update)

    text = update.message.text
    clarification = await asyncio.to_thread(db.pop_pending, chat_id, "clarification")
    if clarification and clarification.get("kind") != "edit" and clarification.get("raw"):
        # Answering a question the parser asked. Resumed against the stalled
        # parse - the answer fills the gaps, the original classification is
        # locked - rather than glued onto the original message and re-parsed,
        # which could silently turn a create into an amend (§20, §31).
        result, usage = await asyncio.to_thread(
            _resume_answer, clarification["raw"], clarification["text"],
            clarification.get("question") or "", text,
        )
        await _record_tokens(usage_row, usage)
        await _dispatch(update, chat_id, telegram_id, result, clarification["text"])
        return

    if clarification:
        if clarification.get("kind") == "edit":
            # A correction to a card, which is a different thing from answering
            # a question the parser asked, and has to be marked as such. Glued
            # on with a full stop it was simply ambiguous: "child_1 17 Aug 10 to
            # 11am swimming. change the date to 16 aug" read as an amend and
            # sent the bot hunting for an event that had never been created,
            # while a bare "16 aug" was dropped entirely and the card came back
            # unchanged (observed 2026-08-08).
            text = f"{clarification['text']}\n{event_parser.CORRECTION_MARKER} {text}"
        else:
            # A pre-Feature-B clarification with no `raw` stashed (or one the
            # parser could not carry): fall back to the glue-and-reparse.
            text = f"{clarification['text']}. {text}"

    result, usage = await asyncio.to_thread(_parse_text, text)
    await _record_tokens(usage_row, usage)

    if _correction_went_astray(clarification, result):
        # The card was a proposal, so there is no event on any calendar to
        # amend or delete - hunting for one produces "Which event would you
        # like to change?", which is worse than useless because it implies the
        # event exists. The prompt rule makes this rare; this makes it
        # recoverable, by keeping the correction pending for another go.
        await asyncio.to_thread(
            db.save_pending, chat_id, "clarification", dict(clarification)
        )
        await _note_friction(telegram_id, "correction_missed", text[:60])
        await update.message.reply_text(
            "Sorry — I didn't catch that change. Try naming just the new value, "
            "e.g. \"make it 16 Aug\" or \"5pm\"."
        )
        return

    await _dispatch(update, chat_id, telegram_id, result, text)


def _correction_went_astray(clarification, result) -> bool:
    """True when a correction to a not-yet-created event parsed as a change to
    an existing one. Only ever fires for ✏️ on a create card: correcting an
    amend card SHOULD still come back as an amend."""
    if not clarification or clarification.get("kind") != "edit":
        return False
    if clarification.get("card") != "create":
        return False
    return result.get("action") in ("amend", "delete")


@safe_handler
async def handle_photo(update, context):
    chat_id = update.effective_chat.id
    telegram_id = update.effective_user.id
    role = permissions.get_role(telegram_id)
    if role is None:
        logger.info("Ignoring photo from unknown Telegram ID", extra={"handler": "handle_photo", "telegram_id": telegram_id})
        return
    if not permissions.can(telegram_id, "write"):
        await update.message.reply_text(READ_ONLY_REPLY)
        return

    allowed, usage_row = await _begin_llm_call(update, telegram_id, "image")
    if not allowed:
        return

    source = _pick_image_source(update.message)
    if source is None:
        logger.info(
            "Rejected oversized image",
            extra={"telegram_id": telegram_id, "sizes": [p.file_size for p in (update.message.photo or [])]},
        )
        await update.message.reply_text(
            "That image is too large for me to read — please send a smaller screenshot."
        )
        return
    await _show_typing(update)
    file = await source.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    caption = update.message.caption or ""
    result, usage = await asyncio.to_thread(_parse_image, image_bytes, caption)
    await _record_tokens(usage_row, usage)
    await _dispatch(update, chat_id, telegram_id, result, caption)


def _pick_image_source(message):
    """The best image to read from a message, whether it arrived as a photo or
    as a file.

    Telegram sends the SAME screenshot two different ways depending on how the
    sender attached it: compressed (a `photo` with several resolutions) or
    "send as file" (a `document`). Phones often default to file for
    screenshots, to preserve quality. Handling only `photo` meant those
    messages matched no handler at all and were silently ignored - which is
    exactly how it looked in use: the bot appeared to do nothing until the
    same thing was typed out by hand.
    """
    if message.photo:
        return _pick_photo(message.photo)
    document = message.document
    if document is not None and (document.mime_type or "").startswith("image/"):
        if (document.file_size or 0) > HARD_PHOTO_LIMIT_BYTES:
            return None
        return document
    return None


def _pick_photo(variants):
    """Largest variant under MAX_PHOTO_BYTES, else the smallest available if
    that still fits the hard limit. Telegram lists variants smallest-first.
    A 4000px screenshot buys a vision model nothing over a 1500px one, but
    costs more tokens, more upload time and more memory on a 1GB VM."""
    usable = [p for p in variants if (p.file_size or 0) <= MAX_PHOTO_BYTES]
    if usable:
        return max(usable, key=lambda p: p.file_size or 0)
    smallest = min(variants, key=lambda p: p.file_size or 0, default=None)
    if smallest is not None and (smallest.file_size or 0) <= HARD_PHOTO_LIMIT_BYTES:
        return smallest
    return None


_budget_warned_on = None


async def _warn_admin_once_daily(update, used, budget):
    """Tell the admin the month's budget is running out - at most once a day,
    so a busy afternoon doesn't turn into a stream of warnings."""
    global _budget_warned_on
    today = datetime.date.today()
    if _budget_warned_on == today:
        return
    _budget_warned_on = today
    admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
    if not admin_id:
        return
    try:
        await update.get_bot().send_message(
            chat_id=int(admin_id),
            text=(
                f"⚠️ Usage is at {used:,} of {budget:,} tokens this month "
                f"({used / budget:.0%}). At 100% I stop replying to everyone except you. "
                "Raise MONTHLY_TOKEN_BUDGET in .env and restart to lift it."
            ),
        )
    except Exception:
        # A failed warning must never take down the message being handled.
        logger.exception("Could not send budget warning to admin")


async def _begin_llm_call(update, telegram_id, kind: str):
    """Meter one LLM call. Returns (allowed, usage_row_id).

    Deliberately a tuple rather than "row id or None": conflating the two
    would make any falsy row id - a db hiccup, a stubbed helper - look like a
    refusal and silently drop the user's message.

    Counted BEFORE the call, not after, so a burst can't slip through while
    several requests are in flight. Two separate limits apply: a per-user
    burst window, and the month's token budget.
    """
    recent = await asyncio.to_thread(db.count_recent_usage, telegram_id, LLM_RATE_WINDOW_MINUTES)
    if recent >= LLM_RATE_LIMIT:
        logger.info(
            "LLM rate limit hit",
            extra={"telegram_id": telegram_id, "recent_calls": recent, "window_minutes": LLM_RATE_WINDOW_MINUTES},
        )
        await update.message.reply_text(BUSY_REPLY)
        return False, None

    used = await asyncio.to_thread(db.tokens_this_month)
    if used >= MONTHLY_TOKEN_BUDGET * BUDGET_WARN_FRACTION:
        await _warn_admin_once_daily(update, used, MONTHLY_TOKEN_BUDGET)
    if used >= MONTHLY_TOKEN_BUDGET and not permissions.can(telegram_id, "audit"):
        # The admin is deliberately never blocked: /usage and raising the cap
        # have to stay reachable from a phone, without SSH to the VM.
        logger.info(
            "Monthly token budget exhausted",
            extra={"telegram_id": telegram_id, "used": used, "budget": MONTHLY_TOKEN_BUDGET},
        )
        await update.message.reply_text(BUDGET_EXCEEDED_REPLY)
        return False, None

    return True, await asyncio.to_thread(db.log_usage, telegram_id, kind)


async def _show_typing(update):
    """Telegram's "typing..." indicator. Purely perceptual - it shortens
    nothing - but a couple of seconds of silence reads as "did that send?",
    and the indicator answers that without a message to clean up afterwards.
    Best-effort: never let a cosmetic call break the request."""
    try:
        await update.get_bot().send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        logger.debug("Could not send typing action", exc_info=True)


def _parse_text(text):
    """Runs in the worker thread so last_usage() is read on the SAME thread
    that made the call - it is thread-local, and reading it back on the event
    loop would always see None."""
    return interpret(text), event_parser.last_usage()


def _resume_answer(raw, original_text, question, answer):
    """Worker-thread wrapper for resume_with_answer, same reasoning as
    _parse_text: last_usage() has to be read on the thread that made the call."""
    return (
        event_parser.resume_with_answer(raw, original_text, question, answer),
        event_parser.last_usage(),
    )


def _parse_image(image_bytes, caption):
    return parse_event_from_image(image_bytes, caption), event_parser.last_usage()


async def _record_tokens(row_id, usage):
    if row_id and usage:
        await asyncio.to_thread(
            db.record_usage_tokens, row_id,
            usage["prompt_tokens"], usage["completion_tokens"], usage["model"],
        )


class _QueryAsUpdate:
    """A CallbackQuery wearing enough of Update's shape to go through _dispatch.

    Everything below _dispatch was written against Update. A button that
    re-enters dispatch (kid:, cat:) hands it a CallbackQuery instead, and the
    two only overlap by accident: `.message` exists on both, `.effective_user`
    exists on neither the same way. That mismatch crashed delete after a child
    tap, silently, while create kept working because it happened to have been
    written to tolerate both callers (2026-08-08).

    Rather than ask every present/ask/handle function to remember which it has
    - a rule that has now been forgotten twice - the difference is erased once,
    here. `edit_message_text` is deliberately still exposed: the confirm cards
    check for it to decide whether to rewrite the tapped card in place or send
    a new message, which is the behaviour we want on this path.
    """

    __slots__ = ("_query", "effective_user", "effective_chat")

    def __init__(self, query):
        self._query = query
        self.effective_user = query.from_user
        self.effective_chat = query.message.chat

    @property
    def message(self):
        return self._query.message

    @property
    def edit_message_text(self):
        return self._query.edit_message_text

    def get_bot(self):
        return self._query.get_bot()


async def _dispatch(update, chat_id, telegram_id, result, source_text):
    """`update` is an Update on the message path and a _QueryAsUpdate on the
    button path - never a bare CallbackQuery, which lacks `.effective_user`
    and would crash half the branches below. telegram_id is still passed
    explicitly, so a handler never has to reach for identity at all."""
    action = result.get("action")

    if action in ("create", "delete", "amend") and not permissions.can(telegram_id, "write"):
        await update.message.reply_text(READ_ONLY_REPLY)
        return

    if action == "email" and not permissions.can(telegram_id, "email_approve"):
        # Admin only, and a separate capability from `write` on purpose: the
        # editor may reshape the family's own calendar all day, but writing to
        # an outside party in the family's name is a different kind of act.
        await update.message.reply_text(
            "Only the admin can email a centre. I can change the calendar for you instead."
        )
        return

    if action == "travel" and not permissions.can(telegram_id, "audit"):
        # Answered out loud, unlike /travel, which ignores non-admins in
        # silence. A slash command they were never shown can be left
        # unmentioned; a sentence somebody typed deserves a reply.
        await update.message.reply_text(
            "Only the admin can change travel times."
        )
        return

    if result.get("status") == "needs_clarification":
        await _note_friction(telegram_id, "clarify", (source_text or "")[:60])
        if result.get("clarify") == "category":
            await _ask_category(update, chat_id, result)
            return
        if result.get("clarify") == "child" and result.get("children"):
            await _ask_child(update, chat_id, result)
            return
        if action == "travel" and result.get("known_place"):
            await _ask_travel_other_end(update, result)
            return
        # `raw` rides along so the typed answer is resumed against the stalled
        # parse (event_parser.resume_with_answer) rather than glued onto the
        # original message and re-parsed from scratch - which could silently
        # re-classify the request. `question` is stored so the resume prompt
        # can tell the model what was actually asked.
        await asyncio.to_thread(
            db.save_pending, chat_id, "clarification",
            {"text": result["source_text"], "raw": result.get("raw"),
             "question": result.get("question")},
        )
        await update.message.reply_text(result["question"])
        return

    # Fold a carer's name onto its canonical spelling before it reaches a card
    # or the calendar - "paernt_1"/"PARENT_1" -> "Parent_1" - so the clash detector, which
    # compares names exactly, doesn't read a typo as a second adult. A name
    # that matches nobody known is left exactly as written.
    if result.get("pickup"):
        result["pickup"] = roster.canonical_carer(result["pickup"])
    _changes = result.get("changes")
    if isinstance(_changes, dict) and _changes.get("pickup"):
        _changes["pickup"] = roster.canonical_carer(_changes["pickup"])

    if action == "create":
        await _present_create(update, chat_id, result, source_text)
    elif action == "delete":
        await _present_delete(update, telegram_id, result)
    elif action == "amend":
        await _present_amend(update, chat_id, telegram_id, result, source_text)
    elif action == "email":
        await _present_email(update, chat_id, telegram_id, result, source_text)
    elif action == "query":
        await _handle_query(update, result)
    elif action == "travel":
        await _present_travel(update, telegram_id, result)
    else:
        await _note_friction(telegram_id, "not_understood", (source_text or "")[:60])
        await update.message.reply_text(_NOT_UNDERSTOOD_REPLY, parse_mode="Markdown")


# ----- travel, said in plain words -----
#
# Telegram caps callback_data at 64 BYTES. An undo payload carries two place
# keys, and a place can be named by an event's own location field ("specialist
# clinic m children's tower level 1"), so it is not always going to fit.
_CALLBACK_MAX = 64


def _undo_payload(a, b, previous) -> str:
    """`tvundo:<a>|<b>|<prev>`, or "" when it would not fit.

    Place KEYS, not indexes into _place_choices(). The picker uses indexes
    safely because two taps are seconds apart, but an Undo button sits in chat
    history for days - add a place in between and every index shifts, so Undo
    would confidently revert a DIFFERENT journey. Keys cannot go stale.

    `previous` is the minutes that were stored before, or -1 for "no row", so
    undoing a first-time edit deletes rather than writing a value back.
    """
    payload = f"tvundo:{a}|{b}|{previous if previous is not None else -1}"
    return payload if len(payload.encode()) <= _CALLBACK_MAX else ""


def _undo_keyboard(payload):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Undo", callback_data=payload)]]
    ) if payload else None


async def _present_travel(update, telegram_id, result):
    """Apply a travel statement, then offer to take it back.

    Applied rather than confirmed because this is a statement about the map,
    not a write to the diary: nothing leaves the house and no event moves. The
    reply names both places and the number so a misparse is visible at a
    glance, and Undo makes it one tap to reverse - which is a better trade
    than a confirm card in front of every correction.
    """
    if result.get("new_place"):
        await _add_place_in_words(update, telegram_id, result["new_place"])
        return

    said_a = result.get("from_place") or ""
    said_b = result.get("to_place") or ""
    hits_a, hits_b = _place_candidates(said_a), _place_candidates(said_b)
    a = hits_a[0] if len(hits_a) == 1 else None
    b = hits_b[0] if len(hits_b) == 1 else None

    # "school to Centre_1" names two schools in this family, at different
    # distances. One side ambiguous and the other settled is answerable with
    # buttons; guessing whichever sorts first would record the wrong journey.
    minutes = result.get("travel_minutes")
    if minutes and not result.get("forget"):
        if len(hits_a) > 1 and b:
            await _ask_between(update, b, minutes, hits_a, said_a)
            return
        if len(hits_b) > 1 and a:
            await _ask_between(update, a, minutes, hits_b, said_b)
            return

    if not a or not b:
        await update.message.reply_text(
            _unknown_place_reply([said_a, said_b], a, b), parse_mode="Markdown",
        )
        return
    if a == b:
        await update.message.reply_text("That's the same place — no travel needed.")
        return

    pair = places.pair_key(a, b)
    overrides, _ = await asyncio.to_thread(db.travel_times_by_source)
    previous = overrides.get(pair, -1)

    if result.get("forget"):
        dropped = await asyncio.to_thread(db.forget_travel_minutes, pair)
        if not dropped:
            await update.message.reply_text("I had nothing stored for that journey.")
            return
        seeded = places.TRAVEL_MINUTES.get(pair)
        back = f" Back to {seeded} min from places.py." if seeded is not None else \
               " I'll ask again next time it comes up."
        await update.message.reply_text(
            f"Dropped {places.label_for(a)} ↔ {places.label_for(b)}.{back}",
            reply_markup=_undo_keyboard(_undo_payload(a, b, previous)),
        )
        return

    minutes = result["travel_minutes"]
    await asyncio.to_thread(db.set_travel_minutes, pair, minutes, telegram_id, db.SET_BY_HAND)
    logger.info("Travel time set in words",
                extra={"telegram_id": telegram_id, "a": a, "b": b, "minutes": minutes})
    await update.message.reply_text(
        f"Noted — {_escape_markdown(places.label_for(a))} ↔ "
        f"{_escape_markdown(places.label_for(b))} is *{minutes} min by car*.",
        reply_markup=_undo_keyboard(_undo_payload(a, b, previous)),
        parse_mode="Markdown",
    )


async def _add_place_in_words(update, telegram_id, label):
    """A new place, introduced in a sentence. Runs the same validation the
    typed form does - a bad alias mis-files events either way."""
    key, aliases, error = _vet_new_place(label, None)
    if error:
        await update.message.reply_text(error, parse_mode="Markdown")
        return
    await asyncio.to_thread(db.add_user_place, key, label, aliases, None, telegram_id)
    _load_user_places()
    logger.info("Place added in words", extra={"telegram_id": telegram_id, "key": key})
    payload = f"tvunplace:{key}"
    await update.message.reply_text(
        f"Added *{_escape_markdown(label)}*, matching "
        f"{_escape_markdown(', '.join(aliases))}.\n"
        f"Tell me how far it is — e.g. \"{_escape_markdown(label)} is 20 minutes "
        f"from school\".",
        reply_markup=(
            InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Undo", callback_data=payload)]])
            if len(payload.encode()) <= _CALLBACK_MAX else None
        ),
        parse_mode="Markdown",
    )


async def _ask_between(update, known, minutes, candidates, said):
    """Which of several places they meant, given the other end is settled.

    Shares the tvone: callback with the one-sided question - both are "this
    many minutes, from here, to which of these?", and one code path for two
    questions that mean the same thing cannot drift.
    """
    choices = _place_choices()
    i = choices.index(known)
    buttons = [
        InlineKeyboardButton(places.label_for(key), callback_data=f"tvone:{i}:{minutes}:{j}")
        for j, key in enumerate(choices) if key in candidates and j != i
    ]
    if not buttons:
        await update.message.reply_text(_unknown_place_reply([said], None, None),
                                        parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"Which {_escape_markdown(said)} did you mean? "
        f"({minutes} min from {places.label_for(known)})",
        reply_markup=_in_rows(buttons, 2),
        parse_mode="Markdown",
    )


async def _ask_travel_other_end(update, result):
    """"20 minutes from where?" with buttons.

    Asked rather than assumed: defaulting the unnamed end to home would
    quietly record a home journey when school was meant, and nothing on screen
    would say so. State rides in callback_data - pending_state is one row per
    chat and would clobber an open confirm card.
    """
    known = _resolve_place(result.get("known_place") or "")
    minutes = result.get("travel_minutes")
    if not known or not minutes:
        await update.message.reply_text(result["question"])
        return
    choices = _place_choices()
    i = choices.index(known)
    buttons = [
        InlineKeyboardButton(places.label_for(key), callback_data=f"tvone:{i}:{minutes}:{j}")
        for j, key in enumerate(choices) if j != i
    ]
    await update.message.reply_text(
        f"{minutes} minutes from {places.label_for(known)} to where?",
        reply_markup=_in_rows(buttons, 2),
    )


# ----- child / category clarification -----

async def _ask_child(update, chat_id, result):
    """Ask which child an event is for, as buttons.

    A tap carries the whole parse forward. A typed answer could not: the
    answer was glued onto the original message and re-parsed, and a screenshot
    has no original message, so "Child_2" arrived on its own and came back as a
    query about Child_2 while the extracted event vanished."""
    children = result.get("children") or calendar_tools.calendar_names()
    await asyncio.to_thread(
        db.save_pending, chat_id, "child",
        {"raw": result["raw"], "source_text": result["source_text"], "children": children},
    )
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"kid:{i}")]
        for i, name in enumerate(children)
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.message.reply_text(result["question"], reply_markup=InlineKeyboardMarkup(buttons))


async def _ask_category(update, chat_id, result):
    """Ask which of a child's calendars an event belongs on, as buttons.
    Filing a private appointment under `school` would expose it to the
    grandparents, so this question is never answered by guessing - and a
    tap can't be misread the way typing "Others" was."""
    categories = result.get("categories") or list(calendar_tools.CATEGORIES)
    await asyncio.to_thread(
        db.save_pending, chat_id, "category",
        {"raw": result["raw"], "source_text": result["source_text"], "categories": categories},
    )
    buttons = [
        [InlineKeyboardButton(c.title(), callback_data=f"cat:{i}")]
        for i, c in enumerate(categories)
    ]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    await update.message.reply_text(result["question"], reply_markup=InlineKeyboardMarkup(buttons))


# ----- pickup clashes -----

def _other_children_keys(calendar_key):
    """Calendars belonging to a DIFFERENT child than calendar_key.

    A pickup clash is by definition across two children, and same-child
    conflicts are already found by find_conflicts_for_child. Fetching only the
    other children's calendars keeps this to 3 extra API calls on a two-child
    setup instead of 6, and means the two checks never report the same problem
    twice in different words.
    """
    entry = calendar_tools._calendars().get(calendar_key)
    if not entry or not entry.get("child"):
        return []
    return [k for k, c in calendar_tools._calendars().items() if c.get("child") != entry["child"]]


def _travel_lookup():
    """A (place_a, place_b) -> minutes callable over what the family has told
    us. Blocking (one small local read); call inside the same thread as the
    clash check, which is already off the event loop.

    Read once and closed over, rather than queried per pair: a week of events
    produces dozens of comparisons, and a round trip inside each one would sit
    in a Telegram handler that is already waiting on Google.
    """
    try:
        overrides, answers = db.travel_times_by_source()
    except Exception:
        # A travel table that cannot be read costs a warning, not a write.
        logger.exception("Travel time lookup failed")
        overrides, answers = {}, {}
    return lambda a, b, people=(): places.travel_minutes(a, b, answers, people, overrides)


def _pickup_clashes(calendar_key, title, occurrences, pickup=None, exclude_event_id=None,
                    location=None):
    """Clashes the proposed event(s) would create, and journeys nobody has
    timed yet. Blocking; call in a thread. Returns (clashes, journeys).

    Returns empty rather than raising if the lookup fails: a confirm card that
    cannot warn is worse than no card at all, so a Calendar hiccup here must
    not take down the write path the family actually depends on.

    `pickup` is a carer the message named. Passing it through matters: naming
    somebody else is the usual way to FIX a clash, so a card that ignored it
    would keep warning about a problem the user had just solved.

    Fetches the SAME child's calendars as well as the other children's. It
    deliberately did not before, because a pickup clash is by definition across
    two children - but a tight transition is by definition within one, and
    school-then-enrichment lives on two different calendars. That is 6 reads on
    a two-child setup instead of 3, still one per calendar however many dates
    are proposed, because the range is fetched in a single call.

    `exclude_event_id` drops one event from what is read back. An amend must
    pass the event it is moving, or the proposal is compared against the very
    booking it replaces and reports a clash with itself. That used to be
    guaranteed by fetching only the other children's calendars; now that the
    same child's are read too, it has to be said out loud.
    """
    proposals = roster.proposed_events(
        calendar_key, title, occurrences,
        description=roster.set_pickup("", pickup) if pickup else None,
        location=location,
    )
    keys = _other_children_keys(calendar_key)
    child = (calendar_tools._calendars().get(calendar_key) or {}).get("child")
    if child:
        keys += calendar_tools.calendar_keys_for_child(child)
    if not keys or not proposals:
        return [], []
    dates = [o["start"].date() for o in occurrences if o.get("start")]
    if not dates:
        return [], []
    try:
        existing = calendar_tools.list_events_range_across(min(dates), max(dates), keys=keys)
    except Exception:
        logger.exception("Pickup clash lookup failed", extra={"calendar_key": calendar_key})
        return [], []
    if exclude_event_id:
        existing = [(k, e) for k, e in existing if e.get("id") != exclude_event_id]
    travel = _travel_lookup()
    return (
        roster.clashes_from(existing, proposals, travel=travel),
        roster.unanswered_journeys(existing, proposals, travel=travel),
    )


def _clash_lines(clashes) -> list:
    """Clash text for a card, escaped and capped. Two is enough to make the
    point; a wall of warnings gets skimmed, which defeats the purpose.

    Ends with a nudge toward the fix that already exists rather than a new
    button (2026-08-07, discussed rather than built: neither "suggest a new
    time" nor "one-tap reassign" pulled its weight over what naming a carer
    already does). ✏️ Change something re-parses the message with a
    correction, and "change the pickup to X" is understood by that path
    already - so the fix is one tap plus a sentence, not a fifth button
    fighting Confirm/Change/Cancel for space.
    """
    lines = [f"⚠️ {_escape_markdown(roster.describe_clash(c))}" for c in clashes[:2]]
    if not lines:
        return lines
    # A tight transition is not fixed by naming a different adult - the child
    # still has the same distance to cover in the same minutes. Offering the
    # pickup nudge there would be advice that cannot work, which is worse than
    # no advice: it costs a tap to find out.
    if any(c.kind == roster.TIGHT_TRANSITION for c in clashes[:2]):
        lines.append(
            "_Tap ✏️ Change something to move one of them — "
            "e.g. \"make it 4.30pm instead\"._"
        )
    else:
        lines.append(
            "_Tap ✏️ Change something and say who's collecting instead — "
            "e.g. \"change the pickup to Grandparent_1\"._"
        )
    return lines


# ----- create -----

async def _present_create(update, chat_id, result, source_text=None):
    calendar_key = result.get("calendar_key")
    occurrences = result.get("occurrences") or []

    # Several times offered to choose between, rather than several dates to
    # add. Checked first because the two arrive in the same result shape and
    # the multi-date path below would otherwise create the whole set.
    if result.get("slot_options"):
        await _present_ranked_slots(update, chat_id, result, source_text)
        return

    if len(occurrences) > 1:
        # Several dates: flag which ones clash and let the user decide with
        # one tap (their call, 2026-08-03). Deliberately NOT the trade-off
        # option flow below - ranking alternative times across many dates is
        # a combinatorial mess, and nothing here is silently skipped or moved.
        clashes = await asyncio.to_thread(
            calendar_tools.find_conflicts_for_child_over, calendar_key, occurrences
        )
        await _show_create_confirm(update, chat_id, result, clashes, source_text)
        return

    if not result["all_day"]:
        # Across every calendar of the same child - a school event and an
        # enrichment class sit on different calendars but still clash.
        conflicts = await asyncio.to_thread(
            lambda: [e for _, e in calendar_tools.find_conflicts_for_child(
                calendar_key, result["start"], result["end"]
            )]
        )
        if conflicts:
            await _present_create_options(update, chat_id, result, conflicts, source_text)
            return
    await _show_create_confirm(update, chat_id, result, source_text=source_text)


def _describe_pending(pending) -> str:
    """Reconstruct a plain-words version of a proposal, for when the original
    request has no wording to reuse - a screenshot sent without a caption."""
    parts = [pending.get("title") or ""]
    if pending.get("all_day") and pending.get("date"):
        parts.append(f"on {pending['date'].strftime('%d %b %Y')}")
    elif pending.get("start"):
        parts.append(f"on {pending['start'].strftime('%d %b %Y')} at {pending['start'].strftime('%H:%M')}")
    return " ".join(p for p in parts if p).strip()


def _occurrence_when(occurrence) -> str:
    """One date's line on a multi-date card. The time is already in the title,
    so only the date is shown here."""
    if occurrence.get("all_day"):
        return occurrence["date"].strftime("%a %d %b")
    return occurrence["start"].strftime("%a %d %b")


async def _show_create_confirm(update_or_query, chat_id, result, clashes=None, source_text=None):
    # Deliberately NOT wrapped in *bold* - Telegram's legacy Markdown parser
    # does not honor \-escapes for characters that are inside an open
    # entity ("Escaping inside entities is not allowed" per its own docs),
    # so escaped model-derived text must never be nested inside our own
    # bold/italic markers, or the escaping silently breaks.
    occurrences = result.get("occurrences") or []
    multi = len(occurrences) > 1

    line = f"Add: {_escape_markdown(result['title'])}"
    if not multi:
        line += f" — {_proposed_when(result)}"
    if result.get("location"):
        line += f" at {_escape_markdown(result['location'])}"
    if calendar_tools.is_multi_calendar_configured() and result.get("calendar_key"):
        line += f" [{calendar_tools.calendar_display_name(result['calendar_key'])}]"
    if result.get("pickup"):
        # Only when the message named someone. The rostered default is not
        # echoed here - it would put a line on every single card, and a card
        # that always says the same thing stops being read.
        line += f"\nPickup: {_escape_markdown(result['pickup'])}"
        if not roster.is_known_carer(result["pickup"]):
            # A name the bot doesn't recognise as a regular. Not blocked - the
            # family adds carers - but flagged, because it is also what a
            # misspelling looks like, and a wrong name here goes silently
            # unmatched by the clash detector.
            line += " _(new name — tap ✏️ if that's a typo)_"

    if multi:
        # Every date is listed rather than summarised: a ✅ must mean "yes, to
        # the thing I am looking at", so the card has to show all of what it
        # commits.
        clashes = clashes or {}
        lines = [line]
        for index, occurrence in enumerate(occurrences):
            entry = f"• {_occurrence_when(occurrence)}"
            if index in clashes:
                names = ", ".join(
                    _escape_markdown(e.get("summary", "(no title)")) for e in clashes[index][:2]
                )
                entry += f"  ⚠️ clashes with {names}"
            lines.append(entry)
        text = "\n".join(lines) + f"\n\nAdd all {len(occurrences)}?"
    else:
        text = line + "?"

    # Who would have to be in two places if this went ahead. Shown on the card
    # rather than sent afterwards, because this is the one moment the user can
    # still do something about it with a single tap.
    pickup_clashes, journeys = await asyncio.to_thread(
        _pickup_clashes, result.get("calendar_key"), result["title"], occurrences,
        result.get("pickup"), None, result.get("location"),
    )

    # A Saturday event nobody has been named for. Who collects varies week to
    # week now that Parent_1 is available alongside Parent_2, and the clash check
    # cannot tell "one adult does both children" from "one each" until a real
    # name is on the event - so ASK, rather than warn off the {"*": "Parent_2"}
    # default. Single-date only: a re-render would drop a multi-date card's
    # per-date clash annotations.
    ask_carer = (
        not multi
        and not result.get("pickup")
        and occurrences and not occurrences[0].get("all_day")
        and roster.carer_choice_needed(occurrences[0]["start"])
    )
    carers = list(roster.SATURDAY_CARERS) if ask_carer else None

    # When the carer is still an open question, the clash and journey lines are
    # speculative (computed against the roster default) - so hold them until
    # the answer comes back and the re-render can show the real picture.
    journey = None if ask_carer else (journeys[0] if journeys and not multi else None)
    if ask_carer:
        text += "\n\nWho's collecting on Saturday?"
    else:
        if pickup_clashes:
            text += "\n\n" + "\n".join(_clash_lines(pickup_clashes))
        # One journey per card at most. Two questions and a confirmation on one
        # card is a form, not a card, and the second pair comes round again on
        # the next event that needs it. Not on a multi-date card either -
        # answering re-renders it and the per-date annotations would be lost.
        if journey:
            text += "\n\n" + _journey_question(journey)

    # source_text rides along so "✏️ Change something" can re-parse the
    # original request together with the correction, instead of making the
    # user retype it.
    await asyncio.to_thread(
        db.save_pending, chat_id, "confirm",
        {"kind": "create", "source_text": source_text,
         "pickup_clash": None if ask_carer else (
             roster.describe_clash(pickup_clashes[0]) if pickup_clashes else None),
         # The pair rides in pending state rather than in callback_data, which
         # is capped at 64 bytes and would truncate a place named by an event's
         # own location field.
         "journey": list(journey) if journey else None,
         **result},
    )
    keyboard = _confirm_keyboard("create", journey=journey, carers=carers)
    if hasattr(update_or_query, "edit_message_text"):  # a CallbackQuery (option-pick path)
        await update_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:  # an Update (direct, no-conflict path)
        await update_or_query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


def _format_option_label(option, is_original):
    when = f"{option['start'].strftime('%a %d %b, %H:%M')}-{option['end'].strftime('%H:%M')}"
    return f"⚠️ Keep {when} (conflicts)" if is_original else f"✅ Move to {when}"


def _score_slots(calendar_key, title, slots, pickup=None, location=None, exclude_event_id=None):
    """Rank candidate slots worst-problem-first. Blocking; call in a thread.

    Every calendar is read ONCE over the whole span the candidates cover, and
    each slot is then scored in memory. Reading per candidate would be six
    calls times six slots inside a handler the family is waiting on.

    Returns [{slot, clashes, conflicts, severity}] best first.
    """
    keys = _other_children_keys(calendar_key)
    child = (calendar_tools._calendars().get(calendar_key) or {}).get("child")
    if child:
        keys += calendar_tools.calendar_keys_for_child(child)
    dates = [s["start"].date() for s in slots if s.get("start")]
    existing = []
    if keys and dates:
        try:
            existing = calendar_tools.list_events_range_across(min(dates), max(dates), keys=keys)
        except Exception:
            # Ranking with no calendar is still worth something - the slots
            # come back unscored rather than the request failing outright.
            logger.exception("Slot ranking lookup failed", extra={"calendar_key": calendar_key})
    if exclude_event_id:
        existing = [(k, e) for k, e in existing if e.get("id") != exclude_event_id]

    travel = _travel_lookup()
    mine = [(k, e) for k, e in existing
            if calendar_tools._calendars().get(k, {}).get("child") == child]
    scored = []
    for slot in slots:
        proposals = roster.proposed_events(
            calendar_key, title, [slot],
            description=roster.set_pickup("", pickup) if pickup else None,
            location=location,
        )
        clashes = roster.clashes_from(existing, proposals, travel=travel) if proposals else []
        # Raw overlap as well as the roster's view. clashes_from only sees
        # events that produce a PICKUP, so an all-day or no-school event would
        # slip past it - and "she is already at school that day" is exactly
        # the kind of thing this is being asked to notice.
        conflicts = [e for _, e in mine if _overlaps_slot(e, slot)]
        severity = max(roster.worst_severity(clashes),
                       roster.CLASH_SEVERITY[roster.CHILD_DOUBLE_BOOKED] if conflicts else 0)
        scored.append({"slot": slot, "clashes": clashes,
                       "conflicts": conflicts, "severity": severity})

    # Worst problem first, then fewest problems, then earliest - so a tie
    # between two clear slots resolves to the one that comes round sooner
    # rather than to whichever the model happened to list first.
    scored.sort(key=lambda s: (s["severity"], len(s["clashes"]) + len(s["conflicts"]),
                               s["slot"]["start"]))
    return scored


def _overlaps_slot(event, slot) -> bool:
    start_raw = (event.get("start") or {}).get("dateTime")
    end_raw = (event.get("end") or {}).get("dateTime")
    if not start_raw or not end_raw:
        # An all-day event covers the whole day, so anything that day overlaps.
        day = (event.get("start") or {}).get("date")
        return bool(day) and day == slot["start"].date().isoformat()
    return (datetime.datetime.fromisoformat(start_raw) < slot["end"]
            and slot["start"] < datetime.datetime.fromisoformat(end_raw))


_RANK_MARK = ["✅", "🟡", "⚠️", "⚠️", "❌"]


def _slot_line(entry, best: bool) -> str:
    """One ranked row: when, how good, and WHY - the why being the whole point.
    A ranking with no reasons is just an opinion."""
    slot = entry["slot"]
    when = f"{slot['start'].strftime('%a %d %b, %H:%M')}"
    mark = _RANK_MARK[min(entry["severity"], len(_RANK_MARK) - 1)]
    if entry["conflicts"]:
        why = "already booked — " + ", ".join(
            e.get("summary", "(no title)") for e in entry["conflicts"][:2])
    elif entry["clashes"]:
        why = roster.summarise_clash(entry["clashes"][0])
    else:
        why = "clear"
    tag = "  ← best" if best and entry["severity"] == 0 else ""
    return f"{mark} *{_escape_markdown(when)}* — {_escape_markdown(why)}{tag}"


async def _present_ranked_slots(update, chat_id, result, source_text=None):
    """Several candidate times, ranked, with a tap to commit one.

    Flow 2 as CLAUDE.md always described it, but over slots the USER named
    rather than ones the bot invented. Nothing is written by showing this -
    picking a row leads to the ordinary confirm card, so the human tap that
    commits a calendar write is exactly where it has always been.
    """
    calendar_key = result.get("calendar_key")
    slots = result["slot_options"]
    scored = await asyncio.to_thread(
        _score_slots, calendar_key, result["title"], slots,
        result.get("pickup"), result.get("location"),
    )

    payload = {
        "title": result["title"], "location": result.get("location"),
        "notes": result.get("notes"), "pickup": result.get("pickup"),
        "calendar_key": calendar_key, "source_text": source_text,
        # Full occurrences, not just start/end: the time_anchor decides which
        # end of the block the retitled event shows, and dropping it here
        # would silently move every chosen slot's title by the block length.
        "options": [dict(s["slot"]) for s in scored],
    }
    await asyncio.to_thread(db.save_pending, chat_id, "create_options", payload)

    lines = [f"*{_escape_markdown(result['title'])}* — best first:", ""]
    lines += [_slot_line(entry, i == 0) for i, entry in enumerate(scored)]
    if all(e["severity"] for e in scored):
        lines += ["", "_Every option has a problem. Pick one and I'll still "
                      "show the warning before anything is saved._"]
    buttons = [
        [InlineKeyboardButton(
            f"{_RANK_MARK[min(entry['severity'], len(_RANK_MARK) - 1)]} "
            f"{entry['slot']['start'].strftime('%a %d %b, %H:%M')}",
            callback_data=f"create_opt:{i}")]
        for i, entry in enumerate(scored)
    ]
    buttons.append([InlineKeyboardButton(_DECLINE_LABEL["create"], callback_data="cancel")])
    await update.message.reply_text("\n".join(lines),
                                    reply_markup=InlineKeyboardMarkup(buttons),
                                    parse_mode="Markdown")


async def _present_create_options(update, chat_id, result, conflicts, source_text=None):
    """Flow 2's trade-off ranking, scoped to same-calendar availability (no
    per-family-member availability data exists in this POC): the requested
    slot plus a few nearby free alternatives, presented as tappable options
    instead of a single confirm-or-cancel."""
    calendar_key = result.get("calendar_key")
    alternatives = await asyncio.to_thread(
        calendar_tools.suggest_alternatives, result["start"], result["end"], calendar_key
    )
    options = [{"start": result["start"], "end": result["end"]}] + [
        {"start": s, "end": e} for s, e in alternatives
    ]
    payload = {
        "title": result["title"],
        "location": result.get("location"),
        "notes": result.get("notes"),
        "calendar_key": calendar_key,
        "options": options,
        "source_text": source_text,
    }
    await asyncio.to_thread(db.save_pending, chat_id, "create_options", payload)

    # Plain text, not Markdown - same reasoning as the delete/amend pickers:
    # simpler and safer than escaping, since no bold/italic styling is needed here.
    conflict_names = ", ".join(c.get("summary", "(no title)") for c in conflicts[:2])
    header = f"⚠️ \"{result['title']}\" clashes with {conflict_names} at that time."
    if not alternatives:
        header += " No free alternative found nearby."
    buttons = [
        [InlineKeyboardButton(_format_option_label(opt, i == 0), callback_data=f"create_opt:{i}")]
        for i, opt in enumerate(options)
    ]
    buttons.append([InlineKeyboardButton(_DECLINE_LABEL["create"], callback_data="cancel")])
    await update.message.reply_text(header + " Which time works?", reply_markup=InlineKeyboardMarkup(buttons))


# ----- delete -----

def _search_keys(calendar_key):
    """Calendars a title search should span. A resolved key searches just
    that one; a list (child resolved, category left open) searches all of
    them; None keeps the single/default-calendar behaviour."""
    if isinstance(calendar_key, list):
        return calendar_key
    return [calendar_key]


async def _find_candidates(result):
    """The events the user might have meant, as (calendar_key, event) pairs,
    plus whether we had to ignore their wording to find any.

    Matching is a NARROWING step, not a gate. "Cancel Child_1's appointment
    today" does not mean "find an event called appointment" - it means "the
    thing on Child_1's calendar today, whatever it is called". So when a day is
    known, that day's events are the candidate set and the title only filters
    it; if the filter matches nothing, the unfiltered day is offered instead.
    Dead-ending while events plainly exist that day is never the right answer.
    """
    keys = _search_keys(result.get("calendar_key"))
    search_text = (result.get("search_text") or "").strip()
    prefer_date = result.get("prefer_date")

    if prefer_date is None:
        # No day given - the title is all we have to go on.
        if not search_text:
            return [], False
        matches = await asyncio.to_thread(
            calendar_tools.find_events_by_title_across, search_text, None, keys
        )
        return matches, False

    candidates = await asyncio.to_thread(
        calendar_tools.list_events_range_across, prefer_date, prefer_date, keys
    )
    if not search_text:
        return candidates, False
    narrowed = [
        (key, event) for key, event in candidates
        if calendar_tools.title_matches(search_text, event.get("summary", ""))
    ]
    if narrowed:
        return narrowed, False
    return candidates, bool(candidates)


def _no_match_reply(result) -> str:
    search_text = (result.get("search_text") or "").strip()
    if result.get("prefer_date") is not None:
        when = result["prefer_date"].strftime("%a %d %b")
        return f"I don't see anything on {when}. Try /upcoming to see what's there."
    return f"I couldn't find an event matching \"{search_text}\". Try /upcoming to see what's there."


def _picker_header(result, fell_back: bool, verb: str, count: int) -> str:
    if fell_back:
        when = result["prefer_date"].strftime("%a %d %b")
        return (
            f"I couldn't match \"{result['search_text']}\" — here's what's on {when}. "
            f"Which one should I {verb}?"
        )
    if count == 1:
        return f"{verb.capitalize()} this event?"
    return f"Which event should I {verb}?"


async def _present_delete(update, telegram_id, result):
    matches, fell_back = await _find_candidates(result)
    if not matches:
        await _note_friction(telegram_id, "not_found", result.get("search_text") or "(no name given)")
        await update.message.reply_text(_no_match_reply(result))
        return
    if fell_back:
        await _note_friction(telegram_id, "fallback", result.get("search_text") or "")

    # calendar_key travels in callback_data itself (delete has no pending-
    # state stash the way amend_pick does) - empty string means "single/
    # default calendar", decoded back to None on the way in. Each match
    # carries its own key, since a search can span a child's calendars.
    # Each row IS the confirmation - tapping one deletes that event - so it
    # says so, and pairs the ✅ against the ❌ below it. The event label stays
    # on the button because with several matches the row is the only thing
    # saying WHICH event this particular ✅ would remove.
    buttons = [
        [InlineKeyboardButton(f"✅ Confirm Delete: {_event_label(e)}", callback_data=f"del:{key or ''}|{e['id']}")]
        for key, e in matches[:10]
    ]
    buttons.append([InlineKeyboardButton(_DECLINE_LABEL["delete"], callback_data="cancel")])
    header = _picker_header(result, fell_back, "delete", len(matches))
    await update.message.reply_text(header, reply_markup=InlineKeyboardMarkup(buttons))


# ----- amend -----

def _apply_changes(original: dict, changes: dict, calendar_key=None) -> dict:
    orig_start_raw = original["start"].get("dateTime")
    orig_end_raw = original["end"].get("dateTime")
    orig_start = datetime.datetime.fromisoformat(orig_start_raw) if orig_start_raw else None
    orig_end = datetime.datetime.fromisoformat(orig_end_raw) if orig_end_raw else None
    duration = (orig_end - orig_start) if (orig_start and orig_end) else datetime.timedelta(hours=1)

    orig_date = calendar_tools.event_start_date(original) or datetime.datetime.now(calendar_tools.TIMEZONE).date()
    date = changes.get("date", orig_date)

    st = datetime.time.fromisoformat(changes["start_time"]) if changes.get("start_time") else None
    et = datetime.time.fromisoformat(changes["end_time"]) if changes.get("end_time") else None

    # Which end of the block a lone new time names - worked out once, here,
    # rather than read straight off `changes`, because for anything but a
    # school event an unqualified new time is the START and the parser could
    # not know the category. Placement and the rebuilt title must agree, so
    # both come from this one value.
    child_name = event_parser.child_name_for_key(calendar_key)
    anchor = _anchor_for(changes, original, child_name, orig_start, orig_end, calendar_key)

    fields = {
        "title": changes.get("title", original.get("summary", "(no title)")),
        "location": changes.get("location", original.get("location")),
    }
    if changes.get("pickup"):
        # Rewritten from the event's CURRENT description rather than rebuilt, so
        # Type, Centre and anything a human typed underneath survive. No key at
        # all when the pickup is unchanged - patch_event leaves the description
        # alone unless it is given one, which is what stops an unrelated amend
        # wiping a "Pickup: Grandparent_1" somebody set by hand.
        fields["pickup"] = changes["pickup"]
        fields["description"] = roster.set_pickup(original.get("description"), changes["pickup"])

    def at(t):
        return datetime.datetime.combine(date, t, tzinfo=calendar_tools.TIMEZONE)

    shift = changes.get("shift_minutes")
    if shift and orig_start and orig_end and not (st or et or "date" in changes):
        # relative move of the whole event (e.g. "delay by 25 min"); keep duration
        delta = datetime.timedelta(minutes=shift)
        fields.update(all_day=False, start=orig_start + delta, end=orig_end + delta)
        return _retitle(fields, original, changes, calendar_key, orig_start, orig_end, anchor)

    if st and et:
        fields.update(all_day=False, start=at(st), end=at(et))
    elif st or et:
        # One new time. "move piano to 6pm" means the class now starts at 6;
        # "move assembly to 3pm" means she is now collected at 3. Which it is
        # comes from `anchor` (the message's wording, then the category), and
        # the existing duration is kept either way so a long event stays long.
        stated = at(et or st)
        if anchor == "start":
            fields.update(all_day=False, start=stated, end=stated + duration)
        else:
            fields.update(all_day=False, start=stated - duration, end=stated)
    elif orig_start and orig_end:
        # only the date/title/location changed; keep the original time-of-day
        fields.update(all_day=False, start=at(orig_start.time()), end=at(orig_end.time()))
    else:
        # original was all-day and no new time given -> stay all-day
        fields.update(all_day=True, date=date)
    return _retitle(fields, original, changes, calendar_key, orig_start, orig_end, anchor)


def _anchor_for(changes: dict, original: dict, child_name, orig_start, orig_end,
                calendar_key=None) -> str:
    """Which end of the event its rebuilt title should state.

    The message wins. Failing that the event keeps whatever it was already
    showing, worked out by comparing its existing title against its existing
    times - so editing only the location of a drop-off event ("Child_1 8am
    Assembly", 08:00-08:15) doesn't silently retitle it to 8.15am. Nothing is
    stored on the calendar to record this, and nothing should be: the title is
    the record, and it round-trips."""
    if changes.get("time_anchor"):
        return changes["time_anchor"]
    if (changes.get("start_time") and changes.get("end_time")
            and event_parser.wants_range_title(calendar_key)):
        # A full new range on a non-school event. Decided here rather than in
        # the parser because an amend searches every one of the child's
        # calendars - which one the event turned out to be on is only known
        # now. Without it, "move piano to 4 to 5.30" would keep the end-only
        # title the event happened to have.
        return "range"
    clock = event_parser.title_clock(original.get("summary", ""), child_name)
    if clock and "-" in clock:
        # Already showing both ends, and the time is not changing - keep it.
        # Falling through would retitle a "4-5pm" class to a bare "5pm" on an
        # amend that only touched its location.
        return "range"
    lone_new_time = bool(changes.get("start_time")) ^ bool(changes.get("end_time"))
    if lone_new_time and event_parser.lone_time_defaults_to_start(calendar_key):
        # A lone new time with no wording pinning which end it is: a school
        # event keeps its pickup-time title, an enrichment or 'others' event
        # is retitled to the start it now begins at. In the parser instead of
        # here this would silently do nothing - an amend has not resolved the
        # event's calendar (so its category) yet.
        return "start"
    if clock and orig_start and orig_end:
        starts_with_it = clock == event_parser.format_clock(orig_start)
        ends_with_it = clock == event_parser.format_clock(orig_end)
        if starts_with_it and not ends_with_it:
            return "start"
    return "end"


def _retitle(fields: dict, original: dict, changes: dict, calendar_key,
             orig_start=None, orig_end=None, anchor=None) -> dict:
    """Rebuild the title around the amended time. The title format embeds a
    clock, so moving an event without this leaves a title stating a time the
    event no longer has - "Child_2 5pm Piano" sitting at 6pm. Also normalises
    events created before this format existed, or by hand in Google
    Calendar. `anchor` is resolved once by the caller when it also places the
    block; recomputed here for callers that don't."""
    child_name = event_parser.child_name_for_key(calendar_key)
    existing_activity, existing_gate = event_parser.split_title(original.get("summary", ""), child_name)
    activity = changes.get("title") or existing_activity
    if anchor is None:
        anchor = _anchor_for(changes, original, child_name, orig_start, orig_end, calendar_key)
    if fields.get("all_day"):
        time_dt = None
    elif anchor == "range":
        time_dt = (fields.get("start"), fields.get("end"))
    else:
        time_dt = fields.get("start" if anchor == "start" else "end")
    # existing_gate is carried through so an amend never silently drops the
    # pickup point off a school event; the activity is passed so an event that
    # has become a no-school day loses the gate it no longer needs.
    gate = event_parser.gate_for(calendar_key, changes.get("gate"), existing_gate, activity=activity)
    fields["title"] = event_parser.format_title(activity, child_name, time_dt, gate)
    return fields


def _change_summary(changes: dict) -> str:
    """A few words naming what an amend would do, for a card that can't show a
    before/after per event because it covers many."""
    parts = []
    if changes.get("pickup"):
        parts.append(f"pickup → {changes['pickup']}")
    if changes.get("title"):
        parts.append(f"renamed to {changes['title']}")
    if changes.get("date"):
        parts.append(f"moved to {changes['date'].strftime('%d %b')}")
    if changes.get("start_time") or changes.get("end_time"):
        parts.append(f"time → {changes.get('start_time') or changes.get('end_time')}")
    if changes.get("shift_minutes"):
        mins = changes["shift_minutes"]
        parts.append(f"{'later' if mins > 0 else 'earlier'} by {abs(mins)} min")
    if changes.get("location"):
        parts.append(f"at {changes['location']}")
    if changes.get("gate"):
        parts.append(f"gate {changes['gate']}")
    return ", ".join(parts) or "updated"


async def _present_amend_series(update, chat_id, telegram_id, result, source_text=None):
    """Apply one change to every matching event from today to a horizon the
    user gave ("...for the next 4 months"). One card lists every date it would
    touch; one ✅ patches them all.
    """
    search_text = (result.get("search_text") or "").strip()
    changes = result.get("changes") or {}
    until = result["series_until"]
    today = datetime.datetime.now(calendar_tools.TIMEZONE).date()
    keys = _search_keys(result.get("calendar_key"))

    found = await asyncio.to_thread(
        calendar_tools.list_events_range_across, today, until, keys
    )
    matches = [
        (key, event) for key, event in found
        if calendar_tools.title_matches(search_text, event.get("summary", ""))
    ]
    if not matches:
        await _note_friction(telegram_id, "not_found", search_text or "(series)")
        await update.message.reply_text(
            f"I don't see any \"{search_text}\" between now and {until.strftime('%d %b')}. "
            "Try /upcoming to check the name."
        )
        return

    capped = matches[:event_parser.MAX_SERIES_EVENTS]
    summary = _change_summary(changes)
    lines = [f"Change *{_escape_markdown(search_text)}* — {_escape_markdown(summary)}", ""]
    for _, event in capped:
        lines.append(f"• {_format_when(event)}")
    if len(matches) > len(capped):
        lines.append(f"…and {len(matches) - len(capped)} more (not shown — give a nearer date to include them)")
    lines.append(f"\nApply to all {len(capped)}?")

    await asyncio.to_thread(
        db.save_pending, chat_id, "confirm",
        {"kind": "amend_series", "source_text": source_text,
         "changes": changes, "title": search_text, "change_summary": summary,
         "events": [{"id": e["id"], "calendar_key": key} for key, e in capped]},
    )
    await update.message.reply_text(
        "\n".join(lines), reply_markup=_confirm_keyboard("amend"), parse_mode="Markdown"
    )


async def _present_amend(update, chat_id, telegram_id, result, source_text=None):
    if result.get("series_until") and (result.get("changes") or {}):
        await _present_amend_series(update, chat_id, telegram_id, result, source_text)
        return

    matches, fell_back = await _find_candidates(result)
    if not matches:
        await _note_friction(telegram_id, "not_found", result.get("search_text") or "(no name given)")
        await update.message.reply_text(_no_match_reply(result))
        return
    if fell_back:
        await _note_friction(telegram_id, "fallback", result.get("search_text") or "")

    # A single match still goes straight to the confirm card - unless we got
    # there by ignoring the user's wording, in which case they should pick it
    # deliberately rather than be shown a card for an event they never named.
    if len(matches) == 1 and not fell_back:
        key, event = matches[0]
        if result.get("slot_options"):
            await _present_amend_options(update, chat_id, event, result, key, source_text)
            return
        await _show_amend_confirm(update, chat_id, event, result["changes"], key, source_text)
        return

    # Each option carries the calendar it was found on, so the pick resolves
    # both which event and which calendar to patch. The candidate times ride
    # along too: which event to move and when to move it to are two questions,
    # and the ranking cannot be built until the first is answered.
    await asyncio.to_thread(
        db.save_pending, chat_id, "amend_pick",
        {"changes": result["changes"], "source_text": source_text,
         "slot_options": result.get("slot_options") or [],
         "keys_by_event": {e["id"]: key for key, e in matches[:10]}},
    )
    buttons = [[InlineKeyboardButton(_event_label(e), callback_data=f"amend:{e['id']}")] for _, e in matches[:10]]
    buttons.append([InlineKeyboardButton(_DECLINE_LABEL["amend"], callback_data="cancel")])
    header = _picker_header(result, fell_back, "change", len(matches))
    await update.message.reply_text(header, reply_markup=InlineKeyboardMarkup(buttons))


def _changes_for_slot(changes, slot) -> dict:
    """`changes` with the time replaced by a chosen slot.

    Written in the shape `_apply_changes` already reads - a date object and
    isoformat times - so the picked slot goes through exactly the same retitle
    and clash path as a time the user named directly.
    """
    return {
        **(changes or {}),
        "date": slot["start"].date(),
        "start_time": slot["start"].time().isoformat(),
        "end_time": slot["end"].time().isoformat(),
        "time_anchor": slot.get("time_anchor", "end"),
    }


async def _present_amend_options(update_or_query, chat_id, original, result,
                                 calendar_key, source_text=None):
    """Rank candidate times for an event that already exists.

    The event being moved is excluded from its own scoring by id - otherwise
    every candidate would be compared against the booking it is replacing, and
    the slot it currently occupies would rank worst of all.
    """
    slots = result["slot_options"]
    changes = result.get("changes") or {}
    title = changes.get("title") or original.get("summary", "(no title)")
    scored = await asyncio.to_thread(
        _score_slots, calendar_key, title, slots,
        changes.get("pickup"), changes.get("location"), original.get("id"),
    )

    await asyncio.to_thread(
        db.save_pending, chat_id, "amend_options",
        {"event_id": original["id"], "calendar_key": calendar_key,
         "changes": changes, "source_text": source_text,
         "options": [dict(s["slot"]) for s in scored]},
    )

    was = f"{_escape_markdown(original.get('summary', '(no title)'))} — {_format_when(original)}"
    lines = [f"Move *{was}* to — best first:", ""]
    lines += [_slot_line(entry, i == 0) for i, entry in enumerate(scored)]
    if all(e["severity"] for e in scored):
        lines += ["", "_Every option has a problem. Pick one and I'll still "
                      "show the warning before anything is saved._"]
    buttons = [
        [InlineKeyboardButton(
            f"{_RANK_MARK[min(entry['severity'], len(_RANK_MARK) - 1)]} "
            f"{entry['slot']['start'].strftime('%a %d %b, %H:%M')}",
            callback_data=f"amend_opt:{i}")]
        for i, entry in enumerate(scored)
    ]
    buttons.append([InlineKeyboardButton(_DECLINE_LABEL["amend"], callback_data="cancel")])
    sender = getattr(update_or_query, "edit_message_text", None) or update_or_query.message.reply_text
    await sender("\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons),
                 parse_mode="Markdown")


def _current_pickup(original, calendar_key):
    """Who is collecting today, for the "was" half of the card.

    Falls back to the roster when the event carries no Pickup: line, because
    that is genuinely who would turn up - most events predate the stamping and
    showing "nobody → Other_carer" for them would misrepresent the change.
    """
    stated = roster.pickup_override(original)
    if stated:
        return stated
    start_raw = (original.get("start") or {}).get("dateTime")
    child = calendar_tools.child_display_name(calendar_key)
    if not start_raw or not child:
        return None
    return roster.pickup_for(child, datetime.datetime.fromisoformat(start_raw))


async def _show_amend_confirm(update_or_query, chat_id, original, changes, calendar_key=None, source_text=None):
    final = _apply_changes(original, changes, calendar_key)

    # The amended event is checked at its NEW time, with its CURRENT booking
    # excluded by id - otherwise the proposal is compared against the very
    # event it replaces and reports a clash with itself.
    occurrence = {"start": final.get("start"), "end": final.get("end"),
                  "all_day": final.get("all_day")}
    pickup_clashes, journeys = await asyncio.to_thread(
        _pickup_clashes, calendar_key, final["title"], [occurrence], final.get("pickup"),
        original["id"], final.get("location"),
    )
    journey = journeys[0] if journeys else None

    await asyncio.to_thread(
        db.save_pending, chat_id, "confirm",
        {"kind": "amend", "event_id": original["id"], "calendar_key": calendar_key,
         "source_text": source_text,
         "pickup_clash": roster.describe_clash(pickup_clashes[0]) if pickup_clashes else None,
         "journey": list(journey) if journey else None,
         **final},
    )

    # Not wrapped in *bold* - same reason as _present_create: escaping breaks
    # inside an open Markdown entity in Telegram's legacy parser.
    line = f"Update to: {_escape_markdown(final['title'])} — {_proposed_when(final)}"
    if final.get("location"):
        line += f" at {_escape_markdown(final['location'])}"
    if calendar_tools.is_multi_calendar_configured() and calendar_key:
        line += f" [{calendar_tools.calendar_display_name(calendar_key)}]"
    if final.get("pickup"):
        # Shown as old → new, on its own line. The pickup lives in the
        # description rather than the title, so without this the card would
        # look identical to one that changed nothing - and a confirmation you
        # cannot read is not a confirmation.
        line += f"\nPickup: {_escape_markdown(_current_pickup(original, calendar_key) or 'nobody')}" \
                f" → {_escape_markdown(final['pickup'])}"
        if not roster.is_known_carer(final["pickup"]):
            line += " _(new name — tap ✏️ if that's a typo)_"
    was_summary = _escape_markdown(original.get("summary", "(no title)"))
    text = f"Was: {was_summary} — {_format_when(original)}\n{line}?"
    if pickup_clashes:
        text += "\n\n" + "\n".join(_clash_lines(pickup_clashes))
    if journey:
        text += "\n\n" + _journey_question(journey)

    keyboard = _confirm_keyboard("amend", journey=journey)
    if hasattr(update_or_query, "edit_message_text"):  # a CallbackQuery (event-pick path)
        await update_or_query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    else:  # an Update (single-match path)
        await update_or_query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


# ----- query -----

async def _answer_pickup_question(update, result) -> bool:
    """Answer "who is picking Child_1 up on Sunday" with a name.

    Returns False if it cannot, so the caller falls back to the event list -
    a question the bot half-understood is better answered with the events than
    with "I don't know".

    Deliberately built on roster.pickups_from_events, the same function behind
    /pickups. Two ways of working out who collects a child would eventually
    disagree, and disagreeing with itself about who is fetching a seven-year-
    old is the worst thing this bot could do.
    """
    start, end = result.get("start"), result.get("end")
    if not start or not end:
        # "who is picking Child_1 up?" names no day. An event list is a poor
        # answer to a who-question, so assume the same week /pickups uses
        # rather than giving up - each line carries its date when the window
        # spans more than one day.
        start = datetime.datetime.now(calendar_tools.TIMEZONE).date()
        end = start + datetime.timedelta(days=PICKUPS_DAYS_AHEAD)

    calendar_key = result.get("calendar_key")
    if isinstance(calendar_key, list):
        keys = calendar_key
    elif calendar_key:
        keys = [calendar_key]
    else:
        keys = list(calendar_tools._calendars())
    if not keys:
        return False

    # list_events_range_across, not the query helpers: the roster needs
    # calendar keys to know whose event it is, and those return display names
    # (see the backlog note on reshaping them).
    events = await asyncio.to_thread(calendar_tools.list_events_range_across, start, end, keys)
    pickups = roster.pickups_from_events(events)
    if not pickups:
        return False

    when = (f"on {start.strftime('%a %d %b')}" if start == end
            else f"{start.strftime('%a %d %b')}–{end.strftime('%a %d %b')}")
    lines = []
    for pickup in pickups:
        activity, _ = event_parser.split_title(pickup.summary, pickup.child)
        who = pickup.person or "⚠️ nobody rostered"
        lines.append(
            f"• *{_escape_markdown(who)}* — {_escape_markdown(pickup.child)} at "
            f"{roster._clock(pickup.when)}, {_escape_markdown(activity or pickup.summary)}"
            + (f" ({pickup.when.strftime('%a %d %b')})" if start != end else "")
        )

    header = "Pickup" if len(lines) == 1 else "Pickups"
    text = f"{header} {when}:\n" + "\n".join(lines)

    clashes = roster.find_clashes(pickups)
    if clashes:
        text += "\n\n" + "\n".join(
            f"⚠️ {_escape_markdown(roster.describe_clash(c))}" for c in clashes[:2]
        )

    await update.message.reply_text(text, parse_mode="Markdown")
    return True


_PART_OF_DAY_LABEL = {span: name for name, span in event_parser._DAY_PARTS.items()}


def _event_in_time_window(entry, time_from, time_to) -> bool:
    """Whether an event overlaps a clock span on its own day. An all-day event,
    or one with no times, counts as overlapping - the safe answer to
    'is she free then?' errs toward 'check'."""
    event = entry[1] if isinstance(entry, tuple) else entry
    start, end = event.get("start") or {}, event.get("end") or {}
    if start.get("date"):
        return True
    sr, er = start.get("dateTime"), end.get("dateTime")
    if not sr or not er:
        return True
    st = datetime.datetime.fromisoformat(sr).time()
    et = datetime.datetime.fromisoformat(er).time()
    return st < time_to and time_from < et


def _query_subject(calendar_key) -> str:
    """'Child_1', 'everyone' - who an availability answer is about."""
    if isinstance(calendar_key, list):
        names = sorted({k.split("_")[0].title() for k in calendar_key})
        return names[0] if len(names) == 1 else "everyone"
    if calendar_key:
        return calendar_tools.child_display_name(calendar_key) or "the calendar"
    return "everyone"


def _availability_when(start, end, time_from, time_to) -> str:
    if start and end and start != end:
        day = f"{start.strftime('%a %d %b')}–{end.strftime('%a %d %b')}"
    elif start:
        day = start.strftime("%a %d %b")
    else:
        day = "then"
    if not time_from:
        return day
    part = _PART_OF_DAY_LABEL.get((time_from, time_to))
    span = part or f"{event_parser.format_clock(time_from)}–{event_parser.format_clock(time_to)}"
    return f"{day} {span}"


async def _answer_availability(update, result, events, start, end):
    """Yes/no plus whatever would clash, for 'is Child_1 free Saturday morning?'."""
    who = _query_subject(result.get("calendar_key"))
    when = _availability_when(start, end, result.get("time_from"), result.get("time_to"))
    if not events:
        await update.message.reply_text(f"✅ {_escape_markdown(who)} is free {when}.")
        return
    lines = [f"⚠️ {_escape_markdown(who)} isn't free {when} —"]
    for entry in events[:6]:
        ev = entry[1] if isinstance(entry, tuple) else entry
        lines.append(f"• {_escape_markdown(ev.get('summary', '(no title)'))} — {_format_when(ev)}")
    await update.message.reply_text("\n".join(lines))


async def _handle_query(update, result):
    if result.get("asks_pickup") and await _answer_pickup_question(update, result):
        return

    start, end = result.get("start"), result.get("end")
    time_from, time_to = result.get("time_from"), result.get("time_to")
    asks_free = result.get("asks_free")
    # "is she free this morning?" often names no day - take it as today.
    if (asks_free or time_from) and not (start and end):
        start = end = datetime.datetime.now(calendar_tools.TIMEZONE).date()

    calendar_key = result.get("calendar_key")
    # A query resolves to one calendar (str), a subset such as one child's
    # school/enrichment/others (list), or nothing at all (None = everyone).
    keys = calendar_key if isinstance(calendar_key, list) else None
    query_all = keys is not None or (calendar_key is None and calendar_tools.is_multi_calendar_configured())

    if start and end:
        if query_all:
            events = await asyncio.to_thread(calendar_tools.list_events_range_all, start, end, keys)
        else:
            events = await asyncio.to_thread(calendar_tools.list_events_range, start, end, calendar_key)
        if start == end:
            empty = f"Nothing on {start.strftime('%a %d %b')}."
        else:
            empty = f"Nothing from {start.strftime('%a %d %b')} to {end.strftime('%a %d %b')}."
    else:
        if query_all:
            events = await asyncio.to_thread(calendar_tools.list_upcoming_events_all, 10, keys)
        else:
            events = await asyncio.to_thread(calendar_tools.list_upcoming_events, calendar_key)
        empty = "Nothing coming up."

    if time_from and time_to and start and end:
        events = [e for e in events if _event_in_time_window(e, time_from, time_to)]

    if asks_free:
        await _answer_availability(update, result, events, start, end)
        return
    await _reply_event_list(update, events, empty)


# ----- email -----

EMAIL_WINDOW_MINUTES = 24 * 60


async def _present_email(update, chat_id, telegram_id, result, source_text=None):
    """Draft an email to a centre and show it in full for approval.

    Order matters. The recipient is resolved BEFORE the model is asked to
    write anything: a draft addressed to nobody is wasted tokens, and more to
    the point, refusing an unknown centre up front means there is never a
    half-finished email looking for somewhere to go.
    """
    if not calendar_tools.email_configured():
        await update.message.reply_text(
            "Email isn't set up yet — no centre addresses are configured."
        )
        return

    try:
        centre_name, address = await asyncio.to_thread(
            gmail_tools.resolve_recipient, result["centre"]
        )
    except (gmail_tools.UnknownRecipient, gmail_tools.EmailNotConfigured) as exc:
        await update.message.reply_text(str(exc))
        return

    sent = await asyncio.to_thread(db.count_recent_emails, EMAIL_WINDOW_MINUTES)
    if gmail_tools.sends_today(sent):
        await update.message.reply_text(
            f"That's {sent} emails in 24 hours already — I'll stop there. "
            "Try again tomorrow, or send this one yourself."
        )
        return

    # Which event, if any. An email need not be about one ("ask about term
    # dates"), so a miss here is not fatal - it just means less context.
    event, calendar_key = None, None
    if result.get("search_text") or result.get("prefer_date"):
        matches, _ = await _find_candidates(result)
        if len(matches) == 1:
            calendar_key, event = matches[0]

    child = calendar_tools.child_display_name(calendar_key) if calendar_key else None
    try:
        body = await asyncio.to_thread(
            event_parser.compose_email,
            centre_name, result["request"], child,
            event.get("summary") if event else None,
            _format_when(event) if event else None,
        )
    except Exception as exc:
        await update.message.reply_text(await _explain_failure(
            update.get_bot(), exc, telegram_id, "email draft",
            "I couldn't draft that email.", "I couldn't draft that email.",
        ))
        return

    subject = gmail_tools.default_subject(child, result["request"])
    await asyncio.to_thread(
        db.save_pending, chat_id, "email_confirm",
        {"address": address, "centre": centre_name, "subject": subject, "body": body,
         "source_text": source_text,
         "event_id": event.get("id") if event else None, "calendar_key": calendar_key,
         "summary": event.get("summary") if event else result["request"]},
    )

    # The whole body, verbatim. Approving a summary of an email is not
    # approving the email (THREAT_MODEL.md §6), so nothing here is shortened
    # except an absurdly long draft, which says so.
    text = (
        f"*Send this email?*\n\n"
        f"To: {_escape_markdown(centre_name)} <{_escape_markdown(address)}>\n"
        f"Subject: {_escape_markdown(subject)}\n\n"
        f"{_escape_markdown(gmail_tools.quote_for_review(body))}"
    )
    buttons = [
        [InlineKeyboardButton("✅ Send it", callback_data="sendmail")],
        [InlineKeyboardButton("✏️ Change something", callback_data="edit")],
        [InlineKeyboardButton("❌ Don't send", callback_data="cancel")],
    ]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


async def _send_pending_email(query, telegram_id, role, chat_id):
    pending = await asyncio.to_thread(db.pop_pending, chat_id, "email_confirm")
    if pending is None:
        await query.edit_message_text(EXPIRED_REPLY)
        return

    # Re-checked at the moment of sending, not just when the card was built.
    # A card can sit for half an hour, and the cap exists to bound what leaves
    # the system, not what was proposed.
    sent = await asyncio.to_thread(db.count_recent_emails, EMAIL_WINDOW_MINUTES)
    if gmail_tools.sends_today(sent):
        await query.edit_message_text("Daily email limit reached — not sent.")
        return

    details = {"address": pending["address"], "centre": pending["centre"],
               "subject": pending["subject"]}
    try:
        result = await asyncio.to_thread(
            gmail_tools.send, pending["address"], pending["subject"], pending["body"]
        )
    except Exception as exc:
        await asyncio.to_thread(
            db.log_audit, telegram_id, role, "email", pending["summary"], details, "failed"
        )
        await query.edit_message_text(await _explain_failure(
            query.get_bot(), exc, telegram_id, "email send",
            "That didn't send.",
            "That may not have sent — please check the Sent folder before trying again.",
        ))
        return

    await asyncio.to_thread(
        db.track_thread, result.get("threadId"), result.get("id"), pending["centre"],
        pending["address"], pending.get("event_id"), pending.get("calendar_key"),
        pending["summary"],
    )
    await asyncio.to_thread(
        db.log_audit, telegram_id, role, "email", pending["summary"], details
    )
    await query.edit_message_text(
        f"✅ Sent to {pending['centre']}.\n\n"
        "When they reply, forward it to me and I'll work out what changes."
    )


# ----- callbacks -----

@safe_handler
async def handle_callback(update, context):
    query = update.callback_query
    chat_id = update.effective_chat.id
    telegram_id = update.effective_user.id
    data = query.data

    # Identity gate comes FIRST, before any branch acts. An unknown ID gets
    # no reply at all (CLAUDE.md: log and ignore silently) - previously the
    # "cancel" branch ran above this check, letting a stranger clear pending
    # state and rewrite the card, and the refusal alert below confirmed the
    # bot's existence to anyone who tapped a button.
    role = permissions.get_role(telegram_id)
    if role is None:
        logger.info(
            "Ignoring callback from unknown Telegram ID",
            extra={"handler": "handle_callback", "telegram_id": telegram_id},
        )
        return

    if data == "cancel":
        await query.answer()
        await asyncio.to_thread(db.clear_pending, chat_id)
        await query.edit_message_text("Okay, nothing changed.")
        return

    if data == "edit":
        # Correct a nearly-right proposal instead of throwing it away. The
        # original request is put back as a pending clarification, so the next
        # message is parsed as "what I asked for, plus this correction" - the
        # same mechanism that already handles answering a clarifying question.
        await query.answer()
        pending = await asyncio.to_thread(db.pop_pending, chat_id, "confirm")
        card_kind = pending.get("kind") if pending else None
        if pending is None:
            # Email cards stash their state under a different kind, and offer
            # the same ✏️ button. Looked up explicitly rather than assumed:
            # missing this made ✏️ on an email card answer "expired".
            pending = await asyncio.to_thread(db.pop_pending, chat_id, "email_confirm")
            card_kind = "email"
        if pending is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        original = (pending.get("source_text") or "").strip()
        if not original:
            # A screenshot with no caption has no original wording to build
            # on, so fall back to describing the proposal itself. An email
            # card has no dates or times to describe, so it restates the ask.
            original = (
                f"email {pending.get('centre', '')} about {pending.get('summary', '')}".strip()
                if card_kind == "email" else _describe_pending(pending)
            )
        await asyncio.to_thread(
            db.save_pending, chat_id, "clarification",
            # `card` records what was being corrected: a correction to a
            # proposed create can never legitimately parse as an amend.
            {"text": original, "kind": "edit", "card": card_kind},
        )
        await _note_friction(telegram_id, "edited", original[:60])
        await query.edit_message_text(
            "What should I change? (e.g. \"make it 5pm\", \"it's for Child_2\", \"that's enrichment\")"
        )
        return

    if data == "sendmail":
        # email_approve, not confirm: tapping ✅ on a calendar card and tapping
        # ✅ on something that leaves the house are different permissions.
        if not permissions.can(telegram_id, "email_approve"):
            await query.answer("You don't have permission to do that.", show_alert=True)
            return
        await query.answer()
        await _send_pending_email(query, telegram_id, role, chat_id)
        return

    if not permissions.can(telegram_id, "confirm"):
        await query.answer("You don't have permission to do that.", show_alert=True)
        return
    await query.answer()

    if data.startswith("del:") or data == "confirm":
        recent = await asyncio.to_thread(db.count_recent_writes, telegram_id, WRITE_RATE_WINDOW_MINUTES)
        if recent >= WRITE_RATE_LIMIT:
            logger.info(
                "Write rate limit hit",
                extra={"telegram_id": telegram_id, "recent_writes": recent, "window_minutes": WRITE_RATE_WINDOW_MINUTES},
            )
            await query.edit_message_text(RATE_LIMIT_REPLY)
            return

    if data.startswith("del:"):
        calendar_key_part, event_id = data[len("del:"):].split("|", 1)
        calendar_key = calendar_key_part or None
        summary = event_id  # fallback label if we can't even fetch the event
        try:
            original = await asyncio.to_thread(calendar_tools.get_event, event_id, calendar_key)
            summary = original.get("summary", "(no title)")
            await asyncio.to_thread(calendar_tools.delete_event, event_id, calendar_key)
        except Exception as exc:
            await asyncio.to_thread(
                db.log_audit, telegram_id, role, "delete", summary,
                {"event_id": event_id, "calendar_key": calendar_key}, "failed",
            )
            await query.edit_message_text(await _explain_failure(
                query.get_bot(), exc, telegram_id, "delete",
                "That wasn't deleted.",
                "That may not have been deleted — please check your calendar before trying again.",
            ))
            return
        await asyncio.to_thread(
            db.log_audit, telegram_id, role, "delete", summary, {"event_id": event_id, "calendar_key": calendar_key}
        )
        await query.edit_message_text("🗑️ Deleted.")
        return

    if data.startswith("amend:"):
        pick = await asyncio.to_thread(db.pop_pending, chat_id, "amend_pick")
        if pick is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        event_id = data.split(":", 1)[1]
        calendar_key = (pick.get("keys_by_event") or {}).get(event_id)
        original = await asyncio.to_thread(calendar_tools.get_event, event_id, calendar_key)
        if pick.get("slot_options"):
            await _present_amend_options(
                query, chat_id, original,
                {"changes": pick["changes"], "slot_options": pick["slot_options"]},
                calendar_key, pick.get("source_text"),
            )
            return
        await _show_amend_confirm(query, chat_id, original, pick["changes"], calendar_key,
                                  pick.get("source_text"))
        return

    if data.startswith("amend_opt:"):
        payload = await asyncio.to_thread(db.pop_pending, chat_id, "amend_options")
        if payload is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        options = payload["options"]
        # Validated like every other client-supplied index: unchecked, a
        # negative value would wrap to a real-but-different slot and move the
        # class to a time the card never offered.
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(options):
            logger.info("Rejected out-of-range amend option",
                        extra={"telegram_id": telegram_id, "raw": data,
                               "option_count": len(options)})
            await query.edit_message_text(EXPIRED_REPLY)
            return
        calendar_key = payload.get("calendar_key")
        original = await asyncio.to_thread(
            calendar_tools.get_event, payload["event_id"], calendar_key
        )
        await _show_amend_confirm(
            query, chat_id, original,
            _changes_for_slot(payload.get("changes"), options[idx]),
            calendar_key, payload.get("source_text"),
        )
        return

    # ----- editing the travel table -----
    #
    # The two chosen places ride in callback_data as INDEXES into
    # _place_choices(), not in pending_state. pending_state holds one row per
    # chat, so a multi-step flow there would clobber an open confirm card and
    # turn the family's pending write into "that request expired". Indexes
    # avoid the problem entirely and cost ~11 bytes of the 64 available.
    if data.startswith("tvundo:"):
        # Place keys, not indexes - see _undo_payload. This button can be
        # tapped days later, by which time an index would point elsewhere.
        try:
            a, b, previous = data[len("tvundo:"):].split("|")
            previous = int(previous)
        except ValueError:
            logger.info("Rejected a malformed undo",
                        extra={"telegram_id": telegram_id, "raw": data})
            await query.edit_message_text(EXPIRED_REPLY)
            return
        pair = places.pair_key(a, b)
        if previous < 0:
            await asyncio.to_thread(db.forget_travel_minutes, pair)
        else:
            await asyncio.to_thread(
                db.set_travel_minutes, pair, previous, telegram_id, db.SET_BY_HAND
            )
        restored = places.stored_minutes(a, b, *(await asyncio.to_thread(_stored_tables)))
        logger.info("Travel edit undone",
                    extra={"telegram_id": telegram_id, "a": a, "b": b})
        back = f" Back to {restored} min by car." if restored is not None else \
               " I'll ask again next time it comes up."
        await query.edit_message_text(
            f"Undone — {places.label_for(a)} ↔ {places.label_for(b)}.{back}"
        )
        return

    if data.startswith("tvunplace:"):
        key = data[len("tvunplace:"):]
        removed = await asyncio.to_thread(db.remove_user_place, key)
        _load_user_places()
        await query.edit_message_text(
            "Undone — I've forgotten that place." if removed
            else "That place is already gone."
        )
        return

    if data.startswith("tvone:"):
        # "20 minutes from <known>" - the other end, picked from buttons.
        choices = _place_choices()
        i = _index_arg(data, 1, choices, telegram_id)
        j = _index_arg(data, 3, choices, telegram_id)
        try:
            minutes = int(data.split(":")[2])
        except (IndexError, ValueError):
            minutes = -1
        if i is None or j is None or i == j or not 0 < minutes <= 240:
            logger.info("Rejected a one-sided travel callback",
                        extra={"telegram_id": telegram_id, "raw": data})
            await query.edit_message_text(EXPIRED_REPLY)
            return
        a, b = choices[i], choices[j]
        overrides, _ = await asyncio.to_thread(db.travel_times_by_source)
        previous = overrides.get(places.pair_key(a, b), -1)
        await asyncio.to_thread(
            db.set_travel_minutes, places.pair_key(a, b), minutes, telegram_id, db.SET_BY_HAND
        )
        await query.edit_message_text(
            f"Noted — {places.label_for(a)} ↔ {places.label_for(b)} is {minutes} min by car.",
            reply_markup=_undo_keyboard(_undo_payload(a, b, previous)),
        )
        return

    if data == "tvedit":
        choices = _place_choices()
        if len(choices) < 2:
            await query.edit_message_text("I need to know at least two places first.")
            return
        await query.edit_message_text(
            "Which journey? Pick the first place.",
            reply_markup=_from_keyboard(choices),
        )
        return

    if data.startswith("tvfrom:"):
        choices = _place_choices()
        i = _index_arg(data, 1, choices, telegram_id)
        if i is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        await query.edit_message_text(
            f"{places.label_for(choices[i])} → where?",
            reply_markup=_to_keyboard(choices, i),
        )
        return

    if data.startswith("tvto:"):
        choices = _place_choices()
        i = _index_arg(data, 1, choices, telegram_id)
        j = _index_arg(data, 2, choices, telegram_id)
        if i is None or j is None or i == j:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        overrides, answers = await asyncio.to_thread(db.travel_times_by_source)
        # The stored CAR time, deliberately not the transport-adjusted one -
        # the question below asks for a car figure, so showing an inflated one
        # would invite an answer with the penalty already baked in.
        current = places.stored_minutes(choices[i], choices[j], answers, overrides)
        now = f" (now {current} min by car)" if current is not None else ""
        await query.edit_message_text(
            f"{places.label_for(choices[i])} ↔ {places.label_for(choices[j])}{now}"
            f"\nHow long by car?",
            reply_markup=_minutes_keyboard(i, j),
        )
        return

    if data.startswith("tvset:"):
        choices = _place_choices()
        i = _index_arg(data, 1, choices, telegram_id)
        j = _index_arg(data, 2, choices, telegram_id)
        try:
            minutes = int(data.split(":")[3])
        except (IndexError, ValueError):
            minutes = -1
        if i is None or j is None or i == j or minutes not in _TRAVEL_CHOICES:
            logger.info("Rejected a travel edit callback",
                        extra={"telegram_id": telegram_id, "raw": data})
            await query.edit_message_text(EXPIRED_REPLY)
            return
        await _store_travel_edit(
            None, telegram_id, choices[i], choices[j], minutes,
            sender=query.edit_message_text,
        )
        return

    if data.startswith("trav:"):
        # Peeked, never popped: this button sits on a live confirm card, and
        # answering a question about a route must not consume the write the
        # card is offering.
        pending = await asyncio.to_thread(db.peek_pending, chat_id, "confirm")
        journey = (pending or {}).get("journey")
        if not pending or not journey:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        # callback_data is client-controlled, same as cat:/kid:/create_opt:. An
        # out-of-range index here would store a travel time nobody offered.
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(places.TRAVEL_BUCKETS):
            logger.info(
                "Rejected out-of-range travel bucket",
                extra={"telegram_id": telegram_id, "raw": data},
            )
            await query.edit_message_text(EXPIRED_REPLY)
            return
        _, minutes = places.TRAVEL_BUCKETS[idx]
        await asyncio.to_thread(
            db.set_travel_minutes, places.pair_key(journey[0], journey[1]), minutes, telegram_id
        )
        logger.info(
            "Learned a travel time",
            extra={"telegram_id": telegram_id, "journey": journey, "minutes": minutes},
        )
        # Re-render the same card. Everything on it is recomputed from the
        # payload, so the answer takes effect immediately: the question
        # disappears and, if the gap is now too short, the warning replaces it.
        await _show_create_confirm(
            query, chat_id, pending, source_text=pending.get("source_text")
        )
        return

    if data.startswith("pk:"):
        # "Who's collecting on Saturday?" answered. Peeked, not popped - the
        # write the card offers must survive answering a question about it.
        pending = await asyncio.to_thread(db.peek_pending, chat_id, "confirm")
        if not pending or pending.get("kind") != "create":
            await query.edit_message_text(EXPIRED_REPLY)
            return
        carers = list(roster.SATURDAY_CARERS)
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(carers):
            logger.info("Rejected out-of-range carer index",
                        extra={"telegram_id": telegram_id, "raw": data})
            await query.edit_message_text(EXPIRED_REPLY)
            return
        # Sets result["pickup"], which flows into the clash check (as a
        # Pickup: line on the proposal), the description that gets written, and
        # the co-parent notification. The re-render then shows the real
        # picture for the carer actually chosen.
        pending["pickup"] = carers[idx]
        await _show_create_confirm(
            query, chat_id, pending, source_text=pending.get("source_text")
        )
        return

    if data.startswith("cat:"):
        payload = await asyncio.to_thread(db.pop_pending, chat_id, "category")
        if payload is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        # Same reasoning as create_opt below: callback_data is client-controlled,
        # so an out-of-range index must be rejected rather than wrapped - here it
        # would file the event on a calendar the buttons never offered.
        categories = payload["categories"]
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(categories):
            logger.info(
                "Rejected out-of-range category index",
                extra={"telegram_id": telegram_id, "raw": data, "category_count": len(categories)},
            )
            await query.edit_message_text(EXPIRED_REPLY)
            return
        result = await asyncio.to_thread(
            event_parser.resume_with_category, payload["raw"], payload["source_text"], categories[idx]
        )
        await _dispatch(_QueryAsUpdate(query), chat_id, telegram_id, result, payload["source_text"])
        return

    if data.startswith("kid:"):
        payload = await asyncio.to_thread(db.pop_pending, chat_id, "child")
        if payload is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        # Index validated for the same reason as cat: below-range would wrap
        # and file the event against a child the buttons never offered.
        children = payload["children"]
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(children):
            logger.info(
                "Rejected out-of-range child index",
                extra={"telegram_id": telegram_id, "raw": data, "child_count": len(children)},
            )
            await query.edit_message_text(EXPIRED_REPLY)
            return
        result = await asyncio.to_thread(
            event_parser.resume_with_child, payload["raw"], payload["source_text"], children[idx]
        )
        # May stall again on the category - _dispatch re-asks with buttons, so
        # the two questions chain without either losing the parse.
        await _dispatch(_QueryAsUpdate(query), chat_id, telegram_id, result, payload["source_text"])
        return

    if data.startswith("create_opt:"):
        payload = await asyncio.to_thread(db.pop_pending, chat_id, "create_options")
        if payload is None:
            await query.edit_message_text(EXPIRED_REPLY)
            return
        # callback_data is client-controlled, so the index is validated rather
        # than trusted. Unvalidated, a negative value would silently wrap to a
        # real-but-different option - i.e. confirming a time the card never showed.
        options = payload["options"]
        try:
            idx = int(data.split(":", 1)[1])
        except ValueError:
            idx = -1
        if not 0 <= idx < len(options):
            logger.info(
                "Rejected out-of-range option index",
                extra={"telegram_id": telegram_id, "raw": data, "option_count": len(options)},
            )
            await query.edit_message_text(EXPIRED_REPLY)
            return
        chosen = options[idx]
        calendar_key = payload.get("calendar_key")
        occurrence = {
            "all_day": False, "start": chosen["start"], "end": chosen["end"],
            "date": None, "time_anchor": chosen.get("time_anchor", "end"),
        }
        # The title carries the time, so picking a different slot has to
        # RETITLE. The stored title was built for whichever option was listed
        # first; keeping it would write "Child_1 4pm Piano" onto a Saturday
        # 10am event - a row that contradicts itself, which is the exact
        # failure the title-derived clock exists to prevent.
        child_name = calendar_tools.child_display_name(calendar_key)
        activity, gate = event_parser.split_title(payload["title"], child_name)
        result = {
            "action": "create", "status": "ok",
            "title": event_parser.format_title(
                activity, child_name, event_parser.title_time(occurrence), gate),
            "location": payload.get("location"), "notes": payload.get("notes"),
            # Carried through so the confirm card can still warn. Without
            # occurrences, _pickup_clashes has no proposal to check and the
            # card comes back silent - on the one path where the user has just
            # been shown that the time is a trade-off.
            "pickup": payload.get("pickup"),
            "occurrences": [occurrence],
            "calendar_key": calendar_key,
            **occurrence,
        }
        await _show_create_confirm(query, chat_id, result,
                                   source_text=payload.get("source_text"))
        return

    if data == "confirm":
        pending = await asyncio.to_thread(db.pop_pending, chat_id, "confirm")
        if pending is None:
            # Covers both "no card was pending" and "the card aged out" -
            # db.pop_pending reports an expired row as absent.
            await query.edit_message_text("Nothing to confirm right now — the request may have expired.")
            return
        audit_details = {
            "start": pending.get("start"),
            "end": pending.get("end"),
            "date": pending.get("date"),
            "location": pending.get("location"),
            "calendar_key": pending.get("calendar_key"),
        }
        occurrences = pending.get("occurrences") or []
        if pending["kind"] == "create" and len(occurrences) > 1:
            await _create_many(query, telegram_id, role, pending, occurrences, audit_details)
            return
        if pending["kind"] == "amend_series":
            await _amend_many(query, telegram_id, role, pending, audit_details)
            return

        try:
            if pending["kind"] == "create":
                created = await asyncio.to_thread(
                    calendar_tools.create_event,
                    pending["title"],
                    start_dt=pending.get("start"),
                    end_dt=pending.get("end"),
                    all_day_date=pending.get("date") if pending["all_day"] else None,
                    location=pending.get("location"),
                    notes=_event_description(pending),
                    calendar_key=pending.get("calendar_key"),
                )
                await query.edit_message_text(f"✅ Added: {created.get('htmlLink')}")
            else:  # amend
                audit_details["event_id"] = pending["event_id"]
                updated = await asyncio.to_thread(
                    calendar_tools.patch_event,
                    pending["event_id"],
                    title=pending["title"],
                    start_dt=pending.get("start"),
                    end_dt=pending.get("end"),
                    all_day_date=pending.get("date") if pending.get("all_day") else None,
                    location=pending.get("location"),
                    # Only present when the pickup changed; None leaves the
                    # existing description untouched.
                    notes=pending.get("description"),
                    calendar_key=pending.get("calendar_key"),
                )
                await query.edit_message_text(f"✅ Updated: {updated.get('htmlLink')}")
        except Exception as exc:
            await asyncio.to_thread(
                db.log_audit, telegram_id, role, pending["kind"], pending["title"], audit_details, "failed"
            )
            await query.edit_message_text(await _explain_failure(
                query.get_bot(), exc, telegram_id, pending["kind"],
                "That didn't save.",
                "That may not have saved — please check your calendar before trying again.",
            ))
            return
        await asyncio.to_thread(
            db.log_audit, telegram_id, role, pending["kind"], pending["title"], audit_details
        )
        if pending["kind"] == "create":
            await _learn_from(telegram_id, role, pending)
        await _tell_the_other_adult(context, telegram_id, pending)


async def _tell_the_other_adult(context, telegram_id, pending):
    """Tell the other write-capable adult that a pickup clash was accepted.

    This is the coordination signal the whole feature exists for: one of you
    has just agreed to something that needs two people, and the other one
    should not find out on the day. Only fires when the card actually showed a
    clash, so it is not a running commentary on the calendar.

    Silently does nothing while the other adult has no Telegram ID configured,
    which is the state the family is in until WIFE_TELEGRAM_ID is filled in -
    a missing recipient is a setup gap, not an error worth surfacing.
    """
    clash = pending.get("pickup_clash")
    if not clash:
        return
    recipients = [
        tid for tid, role in permissions.PERMISSIONS.items()
        if role in LEARNING_ROLES and str(tid) != str(telegram_id)
    ]
    for recipient in recipients:
        try:
            await context.bot.send_message(
                chat_id=int(recipient),
                text=f"Heads up — a change was just confirmed that needs coordinating.\n\n⚠️ {clash}",
            )
        except Exception:
            # A blocked bot or a stale ID must not turn a successful write into
            # a visible failure; the write is already done and audited.
            logger.exception("Could not notify co-parent", extra={"recipient": recipient})


def _event_description(pending, occurrence=None) -> str:
    """The description block written onto a new event.

    Resolving the carer at WRITE time rather than storing it on the card means
    the roster in force when the event is created is what gets recorded - and
    it puts the answer to "who's collecting?" inside Google Calendar, where the
    grandparents and wife already look, without inventing recurring pickup
    events that would bury the glance-at-the-week view.
    """
    calendar_key = pending.get("calendar_key")
    child = calendar_tools.child_display_name(calendar_key)
    when = (occurrence or pending).get("end") or (occurrence or pending).get("start")
    # A carer the message named wins over the roster, and applies to every date
    # in a multi-date create - "Other_carer is getting her" is a statement about the
    # arrangement, not about one Tuesday.
    person = pending.get("pickup") or (roster.pickup_for(child, when) if (child and when) else None)
    category = calendar_tools.category_for_key(calendar_key)
    return roster.describe_event(
        pickup=person,
        event_type=category.title() if category else None,
        notes=pending.get("notes"),
    ) or pending.get("notes")


LEARNING_ROLES = ("admin", "editor")


async def _learn_from(telegram_id, role, pending):
    """Remember what this confirmed event taught us about wording.

    Learned at CONFIRM time, not when a category button is tapped: only events
    that were actually committed should teach anything, so a proposal the user
    thought better of never becomes a lesson.

    Restricted to admin/editor because these phrases feed back into the model
    prompt - that makes learning a path from semi-trusted content (a forwarded
    screenshot) into the model's instructions, so it must never be reachable
    by a reader. Inspect with /hints, undo with /hints reset.
    """
    if role not in LEARNING_ROLES:
        return
    calendar_key = pending.get("calendar_key")
    category = calendar_tools.category_for_key(calendar_key)
    if not category:
        return  # uncategorized setup: nothing to learn

    child_name = event_parser.child_name_for_key(calendar_key)
    activity, gate = event_parser.split_title(pending.get("title", ""), child_name)
    phrases = event_parser.hint_phrases(activity)
    try:
        for phrase in phrases:
            await asyncio.to_thread(db.record_hint, telegram_id, "category", phrase, category)
            if gate and category == "school":
                await asyncio.to_thread(db.record_hint, telegram_id, "gate", phrase, gate)
    except Exception:
        # Learning is a nicety; the write already succeeded and the user has
        # been told. Never turn a bookkeeping failure into a visible error.
        logger.exception("Could not record learned hints")


async def _create_many(query, telegram_id, role, pending, occurrences, audit_details):
    """Write one event per date. Deliberately does NOT abort on the first
    failure: with the write already approved, getting 4 of 5 dates in and
    saying so is more useful than losing all five to one network blip. Each
    event is audited individually, so the log reflects what actually exists."""
    created, failed = [], []
    last_error = None
    for index, occurrence in enumerate(occurrences):
        try:
            event = await asyncio.to_thread(
                calendar_tools.create_event,
                pending["title"],
                start_dt=occurrence.get("start"),
                end_dt=occurrence.get("end"),
                all_day_date=occurrence.get("date") if occurrence.get("all_day") else None,
                location=pending.get("location"),
                # Per occurrence, not once for the batch: a weekly class that
                # runs into the weekend is collected by a different person on
                # the Saturday, and the roster knows that only if it is asked
                # about each date.
                notes=_event_description(pending, occurrence),
                calendar_key=pending.get("calendar_key"),
            )
            created.append(event)
            status = "success"
        except Exception as exc:
            logger.exception(
                "Write failed for one occurrence",
                extra={"chat_id": query.message.chat.id, "when": _occurrence_when(occurrence)},
            )
            last_error = exc
            failed.append(occurrence)
            status = "failed"
        # The first event of the batch counts toward the write rate limit; the
        # rest are logged for the trail but not counted - one ✅ tap is one
        # reviewed decision, however many Saturdays it covers.
        await asyncio.to_thread(
            db.log_audit, telegram_id, role,
            "create" if index == 0 else "create_series", pending["title"],
            {**audit_details, **{k: occurrence.get(k) for k in ("start", "end", "date")}}, status,
        )

    if created:
        await _learn_from(telegram_id, role, pending)

    total = len(occurrences)
    if not failed:
        await query.edit_message_text(f"✅ Added all {total}: {created[0].get('htmlLink')}")
    elif created:
        missed = ", ".join(_occurrence_when(o) for o in failed)
        await query.edit_message_text(
            f"⚠️ Added {len(created)} of {total}. These did not save: {missed}. "
            "Please check your calendar and re-add them.\n\n"
            + await _explain_failure(query.get_bot(), last_error, telegram_id, "create")
        )
    else:
        await query.edit_message_text(await _explain_failure(
            query.get_bot(), last_error, telegram_id, "create",
            "None of those saved.",
            "Those may not have saved — please check your calendar before trying again.",
        ))


async def _amend_many(query, telegram_id, role, pending, audit_details):
    """Apply one `changes` dict to every event in a confirmed series. Like
    _create_many, it does NOT abort on the first failure - the change is
    already approved, so patching 11 of 12 and saying which one missed beats
    losing all twelve to one blip. Each event is re-fetched and re-titled
    individually, so a school and an enrichment class in the same run each get
    the right clock."""
    changes = pending.get("changes") or {}
    refs = pending.get("events") or []
    done, failed = [], []
    last_error = None
    for index, ref in enumerate(refs):
        key = ref.get("calendar_key")
        try:
            original = await asyncio.to_thread(calendar_tools.get_event, ref["id"], key)
            final = _apply_changes(original, changes, key)
            await asyncio.to_thread(
                calendar_tools.patch_event, ref["id"],
                title=final["title"],
                start_dt=final.get("start"),
                end_dt=final.get("end"),
                all_day_date=final.get("date") if final.get("all_day") else None,
                location=final.get("location"),
                notes=final.get("description"),
                calendar_key=key,
            )
            done.append(ref["id"])
            status = "success"
        except Exception as exc:
            logger.exception("Series amend failed for one event",
                             extra={"chat_id": query.message.chat.id, "event_id": ref["id"]})
            last_error = exc
            failed.append(ref["id"])
            status = "failed"
        # First event counts toward the rate limit, the rest don't - see
        # _create_many.
        await asyncio.to_thread(
            db.log_audit, telegram_id, role,
            "amend" if index == 0 else "amend_series", pending.get("title") or "(series)",
            {**audit_details, "event_id": ref["id"]}, status,
        )

    total = len(refs)
    if not failed:
        await query.edit_message_text(f"✅ Updated all {total} — {pending.get('change_summary', 'done')}.")
    elif done:
        await query.edit_message_text(
            f"⚠️ Updated {len(done)} of {total}. The rest didn't save — please check your calendar.\n\n"
            + await _explain_failure(query.get_bot(), last_error, telegram_id, "amend")
        )
    else:
        await query.edit_message_text(await _explain_failure(
            query.get_bot(), last_error, telegram_id, "amend",
            "None of those were changed.",
            "Those may not have been changed — please check your calendar before trying again.",
        ))


REQUIRED_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "ADMIN_TELEGRAM_ID",
]


def validate_config():
    """Fail fast at startup instead of crashing on the first message that
    happens to touch a missing setting."""
    problems = []

    for var in REQUIRED_ENV_VARS:
        if not os.environ.get(var):
            problems.append(f"Missing required .env variable: {var}")

    # At least one calendar must be configured - legacy CALENDAR_ID, the
    # per-child DAUGHTER_N_CALENDAR_ID (ADR-003), or a per-category
    # DAUGHTER_N_<CATEGORY>_CALENDAR_ID (ADR-004).
    if not calendar_tools._calendars():
        problems.append(
            "No calendar configured: set CALENDAR_ID, DAUGHTER_1_CALENDAR_ID, "
            "or DAUGHTER_1_SCHOOL_CALENDAR_ID (see .env.example)"
        )

    # Exactly one Google auth mode must be usable. Service-account mode makes
    # GOOGLE_CREDENTIALS_PATH (an OAuth *client* secret) irrelevant.
    sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
    if sa_path:
        if not os.path.exists(sa_path):
            problems.append(f"GOOGLE_SERVICE_ACCOUNT_PATH points to a file that doesn't exist: {sa_path}")
    elif not creds_path:
        problems.append(
            "Missing Google auth config: set GOOGLE_CREDENTIALS_PATH (user OAuth) "
            "or GOOGLE_SERVICE_ACCOUNT_PATH (service account)"
        )
    elif not os.path.exists(creds_path):
        problems.append(f"GOOGLE_CREDENTIALS_PATH points to a file that doesn't exist: {creds_path}")

    if problems:
        for p in problems:
            logger.error(p)
        raise SystemExit(1)


def run_auth_bootstrap():
    """`python main.py --auth` - the ONLY path allowed to open a browser for
    Google OAuth. Run it on a machine with a browser, then copy the resulting
    token.json to the server. Keeping it out of normal startup is what stops
    a headless host from hanging forever on an OAuth callback."""
    validate_config()
    calendar_tools.get_credentials(allow_interactive=True)
    logger.info("Authorization complete; token.json written.")


def main():
    validate_config()

    # Fail loudly at boot on broken credentials rather than degrading into a
    # generic error reply on every single message.
    try:
        calendar_tools.preflight()
    except (calendar_tools.AuthRequiredError, ValueError) as exc:
        logger.error(str(exc))
        raise SystemExit(1)

    migrated = db.init_db()
    outstanding = db.missing_columns()
    if outstanding:
        # Refusing to start beats starting broken. Without this the migration
        # could fail - a locked file, a read-only volume, a database at a path
        # the service cannot write - and the bot would come up looking healthy
        # while answering "Something went wrong on my end" to every single
        # message, since the failing query sits below safe_handler.
        logger.error(
            "Database schema is out of date and could not be migrated",
            extra={"missing_columns": outstanding, "db_path": db.DB_PATH},
        )
        raise SystemExit(1)
    if migrated:
        # Logged loudly: this is the one part of a deploy that cannot be
        # verified from a development machine, because the server's database
        # has a different history to the local one.
        logger.info("Database migrated", extra={"added_columns": migrated})
    # usage_log now serves two masters with very different retention needs:
    # the per-user burst limit (minutes) and the monthly token budget plus
    # /usage (months). Pruning to the rate-limit window - as this did when
    # metering was the only consumer - would have wiped the month's token
    # totals on every restart, quietly zeroing the budget the bot enforces.
    pruned = db.prune_usage(USAGE_RETENTION_DAYS * 24 * 60)
    if pruned:
        logger.info("Pruned stale usage rows", extra={"rows": pruned})

    _load_user_places()

    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("upcoming", upcoming))
    app.add_handler(CommandHandler("pickups", pickups))
    app.add_handler(CommandHandler("audit", audit))
    app.add_handler(CommandHandler("usage", usage))
    app.add_handler(CommandHandler("hints", hints))
    app.add_handler(CommandHandler("travel", travel))
    app.add_handler(CommandHandler("digest", digest))
    # Document.IMAGE as well as PHOTO: a screenshot sent with "send as file"
    # (the phone default for screenshots on some devices) arrives as a
    # document, and would otherwise match no handler and be silently dropped.
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.post_init = _register_command_menu
    app.run_polling()


async def _register_command_menu(app):
    """Populate the "/" menu in Telegram's UI. Family members see only the
    commands they can use; the admin additionally gets the admin-only ones,
    scoped to their own chat so nobody else is shown a command that would
    silently ignore them."""
    family = [BotCommand(name, desc) for name, desc, admin_only in COMMANDS if not admin_only]
    try:
        await app.bot.set_my_commands(family)
        admin_id = os.environ.get("ADMIN_TELEGRAM_ID")
        if admin_id:
            await app.bot.set_my_commands(
                [BotCommand(name, desc) for name, desc, _ in COMMANDS],
                scope=BotCommandScopeChat(chat_id=int(admin_id)),
            )
    except Exception:
        # Cosmetic: the commands all work whether or not the menu registered.
        logger.exception("Could not register the command menu")


if __name__ == "__main__":
    import sys

    if "--auth" in sys.argv:
        run_auth_bootstrap()
    else:
        main()
