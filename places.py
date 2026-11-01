"""Where an event happens, and how long it takes to get from one to the next.

The gap this fills: `roster.find_clashes` deliberately ignores two events for
the SAME child close together, on the reasoning that school-then-enrichment is
the normal shape of a weekday. That is true when the two places are near each
other, and it is exactly the assumption travel time breaks. Child_1 finishing
school at 3pm and starting piano at 3.15pm across town clashes with nothing:
the events do not overlap, and one child never competes with herself for a
carer. Nothing warned. This module is what lets that be noticed.

Deliberately no routing API and no LLM, for the same reason `roster` has
neither: whether a child can physically get to her next class is arithmetic,
and arithmetic with that consequence should be inspectable and testable rather
than fetched or inferred. A travel time here is a number a human stated - the
family knows the school-to-ballet run better than a traffic model does.

Travel is treated as SYMMETRIC, confirmed by the family on 2026-08-16: the
return leg takes the same time as the outbound one. One number per pair, and
half as much for anyone to keep truthful.
"""
import os
import re

import calendar_tools
import event_parser


def _normalise(text) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


def _pair(a, b):
    """A travel key. Sorted, so a lookup finds the same entry whichever way
    round the journey is being made."""
    return tuple(sorted([_normalise(a), _normalise(b)]))


# ----- the places -----
#
# The one obvious place to edit, like PICKUP_ROSTER in roster.py. A key is an
# internal id; `label` is what appears on a confirm card; `aliases` are matched
# case-insensitively as substrings against an event's location, its `Centre:`
# line, and (for enrichment and others) its activity - so "Make up centre_1"
# resolves to Centre_1 without anyone re-typing anything.
#
# `child` restricts a place to one girl. Three ballet studios all answer to the
# word "ballet", so without this, "Child_2 7.15pm Ballet class" - which names no
# branch - would resolve by dictionary order and could put Child_2 at Child_1's
# Studio_2 studio. Scoped, Child_2's unqualified "Ballet class" lands on Studio_1
# (her only studio) and Child_1's lands on NOTHING, because hers are told apart
# only by branch name. Resolving to nothing is the safe outcome: no place means
# no travel check, and the bot never warns on a guess.
#   -> If a girl ever starts attending the other's studio, drop the `child`
#      key from that entry rather than adding an alias.
#
# Only places listed here can be recognised from an activity. That is on
# purpose: activity text is free-form ("Replacement at the Competition Squad
# time slot"), and treating any of it as a place name would invent junk places
# and then ask the family junk questions about them. An event's own `location`
# or `Centre:` line is trusted verbatim, though - a human wrote it.
#
# Schools are NOT listed here. They are derived from the calendar an event
# lives on (see school_key), which is what makes this work on events already
# in the calendar with nothing re-entered.
PLACES = {
    "swimming": {
        "label": "Swimming",
        "aliases": ["swim", "swimming", "pool", "squad", "competition squad"],
    },
    "centre_1": {
        "label": "Centre_1",
        "aliases": ["centre_1", "centre_1 world"],
    },
    # Child_1's two studios, told apart by branch name only - see the note above.
    "ballet-studio_2": {
        "label": "Ballet (Studio_2)",
        "aliases": ["studio_2", "ballet studio_2"],
        "child": "child_1",
    },
    "ballet-studio_3": {
        "label": "Ballet (Studio_3)",
        "aliases": ["studio_3", "ballet studio_3"],
        "child": "child_1",
    },
    # Child_2's only studio, so a bare "Ballet class" can safely mean this one.
    # Same building as Centre_1, 5 minutes apart.
    "ballet-studio_1": {
        "label": "Ballet (Studio_1)",
        "aliases": ["studio_1", "ballet studio_1", "ballet"],
        "child": "child_2",
    },
    "home": {
        "label": "Home",
        "aliases": ["home", "house", "maths tuition", "math tuition"],
    },
    # "specialist clinic" is deliberately NOT an alias - every hospital has
    # one, and a wrong match here would silently compare against the wrong
    # distance. The children's tower is specific to HOSPITAL_1.
    "hospital_1": {
        "label": "HOSPITAL_1",
        "aliases": ["hospital_1", "kk women", "children's tower"],
    },
}

# Every journey between the places above, in minutes, as told by the family on
# 2026-08-16. Written as a flat list because that is what a human edits: add a
# line, and _pair() handles the ordering. Symmetric, so each journey appears
# once.
#
# `child_1-school` / `child_2-school` embed the names from DAUGHTER_1_NAME and
# DAUGHTER_2_NAME. Rename a child in .env and these keys must be renamed here
# too, or every school journey silently becomes unknown.
#
# A hand-written entry here BEATS a learned one. This file is the thing the
# family is told to tune, so editing it must actually take effect; a learned
# value that silently overrode an edit here would be a genuinely baffling bug.
_JOURNEYS = [
    # Between the two schools - the weekend case, when one adult covers both.
    ("child_1-school", "child_2-school", 10),

    # Child_1's school to everywhere.
    ("child_1-school", "home", 20),
    ("child_1-school", "swimming", 20),
    ("child_1-school", "centre_1", 20),
    ("child_1-school", "ballet-studio_2", 35),
    ("child_1-school", "ballet-studio_3", 20),
    ("child_1-school", "ballet-studio_1", 10),
    ("child_1-school", "hospital_1", 30),

    # Child_2's school to everywhere.
    ("child_2-school", "home", 10),
    ("child_2-school", "swimming", 10),
    ("child_2-school", "centre_1", 20),
    ("child_2-school", "ballet-studio_2", 35),
    ("child_2-school", "ballet-studio_3", 20),
    ("child_2-school", "ballet-studio_1", 20),
    ("child_2-school", "hospital_1", 30),

    # Home to everywhere - journeys often start here.
    ("home", "swimming", 20),
    ("home", "centre_1", 20),
    ("home", "ballet-studio_2", 35),
    ("home", "ballet-studio_3", 20),
    ("home", "ballet-studio_1", 20),
    ("home", "hospital_1", 30),

    # Between the activity places.
    ("swimming", "centre_1", 20),
    ("swimming", "ballet-studio_2", 35),
    ("swimming", "ballet-studio_3", 20),
    ("swimming", "ballet-studio_1", 20),
    ("swimming", "hospital_1", 30),
    ("centre_1", "ballet-studio_2", 35),
    ("centre_1", "ballet-studio_3", 20),
    ("centre_1", "ballet-studio_1", 5),   # same building
    ("centre_1", "hospital_1", 30),
    ("ballet-studio_2", "ballet-studio_3", 35),
    ("ballet-studio_2", "ballet-studio_1", 35),
    ("ballet-studio_2", "hospital_1", 45),
    ("ballet-studio_3", "ballet-studio_1", 20),
    ("ballet-studio_3", "hospital_1", 45),
    ("ballet-studio_1", "hospital_1", 30),
]

TRAVEL_MINUTES = {_pair(a, b): minutes for a, b, minutes in _JOURNEYS}

# ----- how the carer travels -----
#
# Every number above is a CAR time, stated by the family on 2026-08-16. A
# journey made on public transport takes about 20 minutes longer, so the mode
# has to be known before a figure means anything.
#
# Keyed by carer rather than set globally, because the child travels with
# whoever collects her and the roster already knows who that is. A global
# switch would be wrong on exactly the day it matters most: the day two carers
# with different modes are both doing runs.
#
# This matters more than it looks. Helper_1 covers Child_1's weekday pickups, so
# every one of Child_1's weekday journeys is 20 minutes longer than the table
# says - school to Ballet (Studio_2) is 55 minutes, not 35.
TRANSPORT = {
    "parent_1": "car",
    "parent_2": "car",
    "grandparent_1": "car",
    "grandparent_2": "car",
    "helper_1": "public",
}

# Somebody not in the table - a new helper, a friend's parent. Assumed to be
# on public transport, so an unfamiliar carer makes the bot MORE likely to
# warn, not less. Same instinct as everywhere else here: the cost of
# under-estimating is a child waiting alone.
DEFAULT_TRANSPORT = "public"

PUBLIC_TRANSPORT_PENALTY_MINUTES = 20

# At or below this, a "journey" is a walk inside one location - Centre_1 and
# Ballet (Studio_1) share a building, 5 minutes apart. Transport mode is
# irrelevant to a walk, and adding 20 minutes to it would claim a 25-minute
# trip between two units of the same block.
WALKABLE_MINUTES = 10


def transport_for(person) -> str:
    """How a carer gets around. Matched loosely, for the same reason
    roster._same_person is: the roster says "Parent_1", and somebody typing
    "change the pickup to parent_1" gets "parent_1" recorded verbatim, as they should."""
    return TRANSPORT.get(_normalise(person), DEFAULT_TRANSPORT)


def _penalty(people) -> int:
    """Extra minutes for the slowest carer involved.

    Slowest, not first: when a transition hands a child from one adult to
    another, the pessimistic read is the safe one. Nobody involved means
    nobody named, which DEFAULT_TRANSPORT already treats cautiously.
    """
    people = [p for p in (people or ()) if p]
    if not people:
        return PUBLIC_TRANSPORT_PENALTY_MINUTES if DEFAULT_TRANSPORT == "public" else 0
    if any(transport_for(p) == "public" for p in people):
        return PUBLIC_TRANSPORT_PENALTY_MINUTES
    return 0

# Below this gap between two of one child's events, the journey is worth
# checking at all. Above it, travel time cannot plausibly be the problem, so
# neither a warning nor a question is worth the family's attention.
CHECK_WINDOW_MINUTES = int(os.environ.get("TRAVEL_CHECK_WINDOW_MINUTES") or 90)

# What the bot offers when it asks how long a journey takes. Buckets rather
# than a typed number, for the reason the category question uses buttons
# (task #6): a tap cannot be misread, and nobody knows their commute to the
# minute anyway. The stored value is the TOP of the bucket - the pessimistic
# read, because the cost of under-estimating is a child waiting alone.
TRAVEL_BUCKETS = [
    ("Under 15 min", 15),
    ("15–30 min", 30),
    ("30–45 min", 45),
    ("Over 45 min", 60),
]

_SCHOOL_SUFFIX = "-school"
_CENTRE_RE = re.compile(r"^\s*Centre:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# Places added from Telegram, injected by main.py at startup and after each
# add. Held here rather than read from SQLite because this module deliberately
# never imports db - that is what lets every matching rule above be tested
# without a database. Same instinct as passing `learned` into travel_minutes.
_USER_PLACES = {}


def set_user_places(mapping) -> None:
    """Replace the injected overlay. Called once at startup and again whenever
    a place is added, so a new centre resolves without a restart."""
    global _USER_PLACES
    _USER_PLACES = dict(mapping or {})


def known_places() -> dict:
    """Everything this bot can recognise by name.

    A file entry wins a key collision, matching how a seeded travel time beats
    a stored one: places.py is the version under review and in git, so it is
    the one that should be hard to shadow by accident.
    """
    return {**_USER_PLACES, **PLACES}

# Categories whose activity text may name a place. School is excluded: a school
# event is at school, and "Child_1 3pm Squad assembly" must not be dragged off to
# the swimming pool because the word "squad" appears in it.
_ALIAS_CATEGORIES = ("enrichment", "others")


def school_key(child) -> str:
    """The place key for a child's school.

    Per child rather than one shared "school": the two daughters are at
    different schools (confirmed 2026-08-16), and a family where they were not
    could simply record that journey as taking no time.
    """
    resolved = calendar_tools.resolve_child_name(child) or _normalise(child)
    return f"{resolved}{_SCHOOL_SUFFIX}" if resolved else None


def label_for(place) -> str:
    """What a place is called on a card.

    Falls back to the key itself title-cased, because a place can legitimately
    come from an event's own `location` and never appear in PLACES at all.
    """
    if not place:
        return ""
    known = known_places().get(place)
    if known:
        return known["label"]
    if place.endswith(_SCHOOL_SUFFIX):
        child = place[: -len(_SCHOOL_SUFFIX)]
        return f"{child.title()}'s school"
    return place.title()


def match_known(text, child=None) -> str:
    """The PLACES key whose aliases appear in `text`, or None.

    Longest alias first, so "ballet studio_1" cannot be claimed by the shorter
    "ballet". Places carrying a `child` are skipped for anyone else - see the
    note on PLACES for why the three ballet studios need that.
    """
    haystack = _normalise(text)
    if not haystack:
        return None
    wanted = calendar_tools.resolve_child_name(child) if child else None
    best, best_len = None, 0
    for key, entry in known_places().items():
        owner = entry.get("child")
        if owner and owner != wanted:
            continue
        for alias in entry.get("aliases") or []:
            alias = _normalise(alias)
            if alias and alias in haystack and len(alias) > best_len:
                best, best_len = key, len(alias)
    return best


def centre_of(event) -> str:
    """The `Centre:` line from an event's description, if it has one."""
    match = _CENTRE_RE.search(event.get("description") or "")
    if not match:
        return None
    value = match.group(1).strip()
    # The schema's own placeholder, if a template was pasted in unedited.
    return None if value.startswith("[") else (value or None)


def place_for(calendar_key, event) -> str:
    """Where this event happens, as a place key, or None if unknowable.

    Precedence mirrors the pickup rule in `roster.pickup_for`, and for the same
    reason: something a human wrote on the event is evidence, and a default
    derived from the calendar is only a default.

        location  >  Centre:  >  a known place named in the activity
                  >  the child's school, for school events  >  unknown

    A `location` or `Centre:` that matches nothing in PLACES still becomes a
    place, under its own normalised name. That is the path by which the family
    gets places into the system without editing anything - they typed it, so
    it is real, even if this file has never heard of it.
    """
    child = calendar_tools.child_display_name(calendar_key)

    stated = (event.get("location") or "").strip() or centre_of(event)
    if stated:
        return match_known(stated, child) or _normalise(stated)

    summary = event.get("summary") or ""
    activity, _ = event_parser.split_title(summary, child)
    category = calendar_tools.category_for_key(calendar_key)

    if category in _ALIAS_CATEGORIES:
        found = match_known(activity or summary, child)
        if found:
            return found

    if category == "school" and child:
        return school_key(child)
    return None


def stored_minutes(a, b, learned=None, overrides=None):
    """The recorded CAR time for a journey, with no transport penalty applied.

    This is the number a human typed or tapped, and the number the editor must
    show: the edit question asks "how long by car?", so displaying a figure
    already inflated for a carer on a bus would invite an answer that has the
    penalty baked in, and it would then be added a second time on every lookup.
    """
    if not a or not b:
        return None
    key = _pair(a, b)
    if key[0] == key[1]:
        return 0
    base = (overrides or {}).get(key)
    if base is None:
        base = TRAVEL_MINUTES.get(key)
    if base is None:
        base = (learned or {}).get(key)
    return base


def travel_minutes(a, b, learned=None, people=(), overrides=None):
    """Minutes needed between two places, or None if nobody has said.

    Three sources, in this order:

        overrides  - a deliberate /travel edit
        TRAVEL_MINUTES - the seeded table in this file
        learned    - a bucket tapped on a confirm card

    An edit beats the file because the file is a starting point and the edit is
    a correction to it, made later and on purpose. A tap loses to the file
    because it is answered in a hurry, mid-write, and must never quietly
    overwrite a number somebody sat down and set.

    Both stored dicts are passed in rather than read here, so this module stays
    free of the database and testable without one - the same separation that
    keeps `roster` pure.

    `people` are the carers making the journey. Every stored number is a CAR
    time - the seeded table, an edit and a tap alike, which is why the bot's
    question says "by car" - so a carer on public transport adds
    PUBLIC_TRANSPORT_PENALTY_MINUTES.

    Returns 0 for a journey that starts and ends in the same place: back-to-back
    events at one centre need no travel at all, and asking about that pair
    would be the most annoying question the bot could ask.
    """
    base = stored_minutes(a, b, learned, overrides)
    if base is None:
        return None
    # A short hop is a walk within one location; how the carer would otherwise
    # be travelling does not apply to it.
    if base <= WALKABLE_MINUTES:
        return base
    return base + _penalty(people)


def is_known(a, b, learned=None) -> bool:
    return travel_minutes(a, b, learned) is not None


def pair_key(a, b):
    """The (a, b) a caller should store an answer under. Exposed so main.py
    and db.py agree on normalisation without each writing their own."""
    return _pair(a, b)
