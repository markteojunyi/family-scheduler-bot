"""Who collects which child, and when that becomes impossible.

The problem this solves is NOT a calendar conflict. Two children at two places
at 4pm is fine when two adults are free; it is only a problem when they are
not. So the thing modelled here is *pickup capacity*, which nothing else in the
system knows about.

Deliberately no LLM anywhere in this file. Working out that one person cannot
be in two places is arithmetic, and arithmetic that decides whether somebody
gets collected from school should be inspectable and testable, not inferred.
"""
import datetime
import os
import re

import calendar_tools
import event_parser
import places

# ----- the roster -----
#
# Keyed by day type, then by the child key used in the calendar registry (the
# lowercased DAUGHTER_N_NAME), with "*" meaning "whoever is on that day covers
# every child". Tune this here as the family's arrangements change - it is the
# same "one obvious place to edit" idea as _CATEGORY_HINTS in event_parser.
#
# Capacity is NOT configured separately: the number of DISTINCT people rostered
# on a day IS the number of simultaneous pickups possible. That falls out of
# the table rather than being a second thing to keep in sync, which means it
# cannot drift out of agreement with it.
#
#   weekday  -> Parent_1 + Helper_1 = 2 adults, two simultaneous pickups are fine
#   saturday -> Parent_2 alone    = 1 adult, ANY two overlapping pickups clash
#   sunday   -> Parent_1 alone    = 1 adult, likewise
#
# The weekends being structurally fragile is the whole reason this file exists.
PICKUP_ROSTER = {
    "weekday": {"child_2": "Parent_1", "child_1": "Helper_1"},
    "saturday": {"*": "Parent_2"},
    "sunday": {"*": "Parent_1"},
}

# Saturday used to be Parent_2 alone. Parent_1 is now available too, and who actually
# collects varies week to week - so the confirm card ASKS rather than assuming
# (see carer_choice_needed). The roster entry above stays as the fallback for
# /pickups and for a card the family dismissed without answering; these are the
# names the card offers. First is the pre-filled suggestion. Tune as the
# arrangement changes - the same "one obvious place to edit" idea as the roster.
SATURDAY_CARERS = ("Parent_2", "Parent_1")

# The people who actually do pickups, in canonical spelling. Used to fold a
# typo or a case difference back onto the real name ("paernt_1" -> "Parent_1") so the
# clash detector doesn't read it as a second adult - _same_person compares
# exactly. A name that matches nobody here is passed through untouched: adding
# a new carer ("Other_carer is getting her Thursdays") has to keep working. Should
# stay in step with places.TRANSPORT, which knows how each of them travels.
KNOWN_CARERS = ("Parent_1", "Parent_2", "Helper_1", "Grandparent_1", "Grandparent_2")


def _within_one_edit(a: str, b: str) -> bool:
    """True if `a` is at most one insertion, deletion or substitution from `b`.
    Deliberately tight - "bobby" is nowhere near any carer, so it never folds."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:  # one substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    longer, shorter = (a, b) if la > lb else (b, a)  # one insertion/deletion
    for i in range(len(longer)):
        if longer[:i] + longer[i + 1:] == shorter:
            return True
    return False


def canonical_carer(name):
    """A carer name folded onto its canonical spelling if it names someone in
    KNOWN_CARERS - an exact case-insensitive match, or a single-character typo
    on a name of four or more letters. Anything else is returned as written,
    so a genuinely new carer still works."""
    if not name or not str(name).strip():
        return name
    written = str(name).strip()
    low = written.casefold()
    for canon in KNOWN_CARERS:
        if low == canon.casefold():
            return canon
    # Fold only when exactly ONE carer is a single typo away. With names this
    # close together ("parent_1" / "parent_2") a typo can sit between two of
    # them, and guessing would read one adult as the other.
    near = [c for c in KNOWN_CARERS
            if len(c) >= 4 and _within_one_edit(low, c.casefold())]
    if len(near) == 1:
        return near[0]
    return written


def is_known_carer(name) -> bool:
    """Whether `name` (after canonicalising) is one of the family's regulars."""
    if not name:
        return False
    known = {c.casefold() for c in KNOWN_CARERS}
    return canonical_carer(name).casefold() in known

# How close two pickups have to be, in minutes, before one person cannot do
# both. Not real travel-time routing - a single buffer is enough to catch the
# case that actually bites (4.00pm and 4.15pm at different places) without
# pretending to know about traffic.
PICKUP_BUFFER_MINUTES = int(os.environ.get("PICKUP_BUFFER_MINUTES") or 45)

# Clash kinds. Kept distinct because they need different responses: a
# double-booked child is impossible and someone must move an event, whereas a
# pickup clash is a coordination problem a phone call might solve.
CHILD_DOUBLE_BOOKED = "child_double_booked"
PICKUP_CLASH = "pickup_clash"
UNASSIGNED = "unassigned"
# One child, two events that do not overlap but are too close together to
# travel between. The only kind here that needs to know WHERE things happen.
TIGHT_TRANSITION = "tight_transition"


def day_type(date) -> str:
    """Which roster column a date falls in.

    "weekday" means Mon-Fri, not "a day the school is open". The bot has no
    school-holiday calendar and inventing one would be guesswork; instead a day
    with no school simply produces no school pickup at all, because the event
    itself says so (see event_parser.is_no_pickup). Enrichment on a school
    holiday still needs collecting, and the weekday roster still covers it -
    which is the behaviour we want.
    """
    weekday = date.weekday()
    if weekday == 5:
        return "saturday"
    if weekday == 6:
        return "sunday"
    return "weekday"


def pickup_for(child, when, override=None):
    """Who collects `child` at `when`, or None if nobody is rostered.

    Precedence is the same discipline as the pickup gate, for the same reason:
    a person a human named is evidence, the roster is only a default.
        stated on the event  >  roster  >  nobody
    """
    if override:
        return override
    table = PICKUP_ROSTER.get(day_type(_as_date(when)), {})
    return table.get(_child_key(child)) or table.get("*")


def carers_on(date) -> set:
    """The distinct people rostered on a date - i.e. how many pickups can
    happen at once. Derived, never configured; see the note on PICKUP_ROSTER."""
    return {p for p in PICKUP_ROSTER.get(day_type(date), {}).values() if p}


def carer_choice_needed(when, override=None) -> bool:
    """True when the confirm card should ASK who is collecting, instead of
    taking the roster's word for it.

    Saturday only, and only when the message did not already name someone -
    "Parent_1's getting her" answers the question before it is asked. The point is
    the clash check: it can only tell "Parent_2 does both children" from "one
    each" once a real name is on the event, not the {"*": "Parent_2"} default.
    """
    if override:
        return False
    return day_type(_as_date(when)) == "saturday"


def unknown_roster_children() -> list:
    """Roster entries naming a child that no configured calendar knows about.

    The roster is keyed by child, and those keys come from DAUGHTER_N_NAME in
    .env. Rename a child there and the roster silently stops matching, so every
    pickup becomes unassigned - a failure that looks like "the feature does
    nothing" rather than an error. Called at startup so it is logged instead.
    """
    known = {c["child"] for c in calendar_tools._calendars().values() if c.get("child")}
    named = {
        child
        for table in PICKUP_ROSTER.values()
        for child in table
        if child != "*"
    }
    return sorted(named - known) if known else []


# ----- reading pickups off real events -----

class Pickup:
    """One moment an adult has to be somewhere for a child.

    `when` is the event's END, because that is when a child is collected. For a
    school event a lone stated time IS that end ("Child_1 assembly 9am" = collected
    at 9); for an enrichment class the stated time is the start and the end is
    the 15-minute block's - close enough for a 45-minute buffer. Either way the
    feature works off data already in the calendar, with nothing new recorded.
    """

    __slots__ = ("child", "person", "start", "when", "summary", "calendar_key",
                 "event_id", "proposed", "place")

    def __init__(self, child, person, start, when, summary, calendar_key, event_id,
                 proposed=False, place=None):
        self.child = child
        self.person = person
        self.start = start
        self.when = when
        self.summary = summary
        self.calendar_key = calendar_key
        self.event_id = event_id
        # Where this happens, as a places.py key, or None when unknowable.
        # Only ever used to look up a travel time; nothing here depends on
        # recognising the place itself.
        self.place = place
        # True for something not on the calendar yet - the event on a confirm
        # card. Lets a warning be narrowed to "clashes this change would
        # CREATE", rather than re-reporting problems the calendar already has
        # and that the user is not currently being asked about.
        self.proposed = proposed

    def __repr__(self):
        return f"<Pickup {self.child} {self.when:%a %d %b %H:%M} by {self.person or 'nobody'}: {self.summary}>"

    def __eq__(self, other):
        return isinstance(other, Pickup) and self._key() == other._key()

    def __hash__(self):
        return hash(self._key())

    def _key(self):
        return (self.child, self.person, self.start, self.when, self.summary,
                self.calendar_key, self.event_id, self.proposed, self.place)


_PICKUP_FIELD_RE = re.compile(r"^\s*Pickup:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def pickup_override(event) -> str:
    """The `Pickup:` line from an event's description, if it has one. This is
    the per-event exception to the roster ("Grandparent_1 is getting Child_1 Friday")."""
    match = _PICKUP_FIELD_RE.search(event.get("description") or "")
    if not match:
        return None
    value = match.group(1).strip()
    # The schema's own placeholder, if a template was pasted in unedited.
    return None if value.startswith("[") else (value or None)


def pickups_from_events(dated_events, proposed=False) -> list:
    """Turn (calendar_key, event) pairs into the pickups they actually create.

    Three kinds of event produce none at all, and skipping them is what keeps
    the false-positive rate low enough for anyone to trust a warning:
      - all-day events, which have no moment to turn up at
      - days the child is not attending ("no school", a public holiday), reusing
        the same test that decides whether a title shows a gate
      - events on a calendar whose child cannot be resolved
    """
    pickups = []
    for calendar_key, event in dated_events:
        start_raw = (event.get("start") or {}).get("dateTime")
        end_raw = (event.get("end") or {}).get("dateTime")
        if not start_raw or not end_raw:
            continue

        child = calendar_tools.child_display_name(calendar_key)
        if not child:
            continue

        summary = event.get("summary") or "(no title)"
        activity, _ = event_parser.split_title(summary, child)
        if event_parser.is_no_pickup(activity) or event_parser.is_no_pickup(summary):
            continue

        when = datetime.datetime.fromisoformat(end_raw)
        pickups.append(
            Pickup(
                child=child,
                person=pickup_for(child, when, pickup_override(event)),
                start=datetime.datetime.fromisoformat(start_raw),
                when=when,
                summary=summary,
                calendar_key=calendar_key,
                event_id=event.get("id"),
                proposed=proposed,
                place=places.place_for(calendar_key, event),
            )
        )
    return sorted(pickups, key=lambda p: p.when)


def proposed_events(calendar_key, title, occurrences, description=None, location=None) -> list:
    """A pending create/amend as event-shaped dicts.

    Deliberately shaped like something Google returned, so a proposal goes
    through exactly the same pickup logic as an event already on the calendar.
    Two code paths that both decide "does this need collecting" would sooner or
    later disagree, and the confirm card would then promise something the
    detector later contradicts.

    `location` has to be carried for the same reason. It is the highest-
    precedence source of an event's PLACE (places.place_for), and it is the
    only way a centre this bot has never heard of enters the system at all -
    so dropping it here made a proposal at a brand-new place look like a
    proposal at no place, and silently skipped every travel check on it.
    """
    events = []
    for occurrence in occurrences or []:
        if occurrence.get("all_day") or not occurrence.get("end"):
            continue
        event = {
            "id": None,
            "summary": title,
            "start": {"dateTime": occurrence["start"].isoformat()},
            "end": {"dateTime": occurrence["end"].isoformat()},
        }
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        events.append((calendar_key, event))
    return events


def clashes_from(existing_events, proposals, buffer_minutes=None, travel=None) -> list:
    """Clashes a proposed change would CREATE, given what is already booked.

    Filtered to clashes touching the proposal on purpose. A family whose
    Saturday is already double-booked should not have that thrown at them again
    every time they add an unrelated Tuesday event - the card is asking about
    one thing, so it reports on that one thing.
    """
    pickups = pickups_from_events(existing_events) + pickups_from_events(proposals, proposed=True)
    found = find_clashes(pickups, buffer_minutes, travel)
    return [c for c in found if any(p.proposed for p in c.pickups)]


def unanswered_journeys(existing_events, proposals, travel=None, window_minutes=None) -> list:
    """Journeys the proposal depends on that nobody has timed yet.

    Returned separately from clashes because it is a QUESTION, not a warning.
    The bot cannot say whether 15 minutes is enough to get from school to piano
    until somebody tells it how far apart they are, and guessing a number would
    be exactly the invention this module refuses to make elsewhere.

    Narrowed twice, so the family is asked rarely and only about journeys that
    could actually matter:
      - only pairs where one side is the event being proposed
      - only gaps under `window_minutes`; three hours between two events makes
        the travel time irrelevant however far apart they are

    Yields (from, to) in the order the journey is actually MADE, so the
    question reads the way the day goes: school to chess, not chess to school.
    Deduplication and storage still go through places.pair_key, which sorts -
    travel is symmetric, so the two directions are one entry however the
    question was phrased.
    """
    window = datetime.timedelta(
        minutes=window_minutes if window_minutes is not None else places.CHECK_WINDOW_MINUTES
    )
    pickups = pickups_from_events(existing_events) + pickups_from_events(proposals, proposed=True)
    seen, out = set(), []
    for first, second in _transitions(pickups, window):
        if not (first.proposed or second.proposed):
            continue
        if travel and travel(first.place, second.place, _people(first, second)) is not None:
            continue
        key = places.pair_key(first.place, second.place)
        if key[0] == key[1] or key in seen:
            continue
        seen.add(key)
        out.append((first.place, second.place))
    return out


def _transitions(pickups, window):
    """Consecutive same-child event pairs close enough for travel to matter.

    Pairs one event's END with the next one's START - that is the journey the
    child actually makes. Both places must be known, since a pair with an
    unknown side has nothing to look up and nothing to ask about.
    """
    ordered = sorted(pickups, key=lambda p: (p.child or "", p.when))
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if first.child != second.child:
                continue
            if not first.place or not second.place:
                continue
            gap = second.start - first.when
            # Negative means they overlap, which is CHILD_DOUBLE_BOOKED's
            # problem and a strictly worse one - reporting both would say the
            # same thing twice in two different registers.
            if datetime.timedelta(0) <= gap <= window:
                yield first, second


# ----- the detector -----

class Clash:
    __slots__ = ("kind", "pickups", "needed")

    def __init__(self, kind, pickups, needed=None):
        self.kind = kind
        self.pickups = list(pickups)
        # Minutes the journey actually needs, for the kinds that know. Carried
        # on the clash rather than recomputed in describe_clash, which has no
        # travel lookup - and recomputing it there could disagree with the
        # number that decided this was a clash in the first place.
        self.needed = needed

    @property
    def when(self):
        return min(p.when for p in self.pickups)

    @property
    def person(self):
        return self.pickups[0].person

    def __repr__(self):
        who = " + ".join(sorted({p.child for p in self.pickups}))
        return f"<Clash {self.kind} {self.when:%a %d %b %H:%M} {who}>"


def find_clashes(pickups, buffer_minutes=None, travel=None) -> list:
    """Every place this schedule needs a human decision.

    `travel` is a callable (place_a, place_b) -> minutes, or None for a pair
    nobody has timed. Passed in rather than looked up here so this module never
    touches the database and stays testable with a dict. When it is omitted
    every pair reads as unknown, which is exactly the behaviour this had before
    places existed.

    Two pickups for the SAME child close together used to be skipped outright,
    on the reasoning that school-then-enrichment is the normal shape of a
    weekday - one adult drops her off and comes back. That holds only while the
    two places are near each other, and it is why a 3pm finish and a 3.15pm
    start on opposite sides of town warned about nothing. Now it is skipped
    only when there is time to make the journey, or when nobody has said how
    long the journey takes.

    The weekend case needs no special handling. On a Saturday the roster maps
    both children to the same person, so two pickups there collide under the
    ordinary same-person rule. Capacity never has to be counted.
    """
    default_buffer = datetime.timedelta(
        minutes=buffer_minutes if buffer_minutes is not None else PICKUP_BUFFER_MINUTES
    )
    clashes = []
    ordered = sorted(pickups, key=lambda p: p.when)

    # Every pair, with no early exit. Sorting by end time does NOT bound the
    # search: a long event can start before this pickup and end hours after it,
    # so it still overlaps while being far away in the ordering. A week of a
    # family's events is a few dozen items, and being obviously correct is
    # worth more here than the loop being clever.
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            if first.child == second.child:
                if _overlaps(first, second):
                    clashes.append(Clash(CHILD_DOUBLE_BOOKED, [first, second]))
                else:
                    needed = _too_tight(first, second, travel)
                    if needed is not None:
                        clashes.append(Clash(TIGHT_TRANSITION, [first, second], needed))
                continue

            if first.person and _same_person(first.person, second.person):
                # One adult, two children, so the question is whether they can
                # get from the first pickup to the second. A known journey
                # answers that exactly; the flat buffer is the fallback for
                # when it is unanswered. Note this can make the bot QUIETER
                # than the blanket 45 minutes did - two children finishing 20
                # minutes apart at the same centre is genuinely fine, and
                # warning about it was the false positive that teaches people
                # to ignore warnings.
                minutes = _needed_minutes(first, second, travel)
                gap = second.when - first.when
                if minutes is not None:
                    tight = gap < datetime.timedelta(minutes=minutes)
                else:
                    tight = gap <= default_buffer
                if tight:
                    clashes.append(Clash(PICKUP_CLASH, [first, second], minutes))

    clashes.extend(Clash(UNASSIGNED, [p]) for p in ordered if not p.person)
    return sorted(clashes, key=lambda c: c.when)


def _people(*pickups):
    """The carers involved, for a travel lookup that depends on how they get
    around. Deduplicated but order-preserving, so a warning is reproducible."""
    seen, out = set(), []
    for p in pickups:
        if p.person and p.person.casefold() not in seen:
            seen.add(p.person.casefold())
            out.append(p.person)
    return tuple(out)


def _needed_minutes(first, second, travel):
    """Travel time between two pickups in minutes, or None if unknown."""
    if not travel:
        return None
    return travel(first.place, second.place, _people(first, second))


def _too_tight(first, second, travel):
    """Minutes the journey needs, if there is not enough time - else None.

    Measures END of the earlier event to START of the later one - the journey
    the child actually makes. An unknown journey is never a clash: the bot asks
    about it instead (see unanswered_journeys), because a warning built on a
    number nobody gave is worse than no warning.
    """
    minutes = _needed_minutes(first, second, travel)
    if minutes is None:
        return None
    earlier, later = (first, second) if first.when <= second.when else (second, first)
    tight = later.start - earlier.when < datetime.timedelta(minutes=minutes)
    return minutes if tight else None


# How bad each kind of problem is, for ranking one candidate slot against
# another. Ordered by how hard it is for a human to FIX, not by how alarming
# it sounds:
#
#   double-booked - impossible; the child cannot be in two places, so
#                   something has to move whatever anyone decides
#   unassigned    - nobody is rostered; needs a person found before the day
#   pickup clash  - one adult, two children; a phone call might solve it
#   tight         - the journey is short on minutes; leaving earlier might do
#
# A slot with no problems at all scores 0 and sorts first.
CLASH_SEVERITY = {
    CHILD_DOUBLE_BOOKED: 4,
    UNASSIGNED: 3,
    PICKUP_CLASH: 2,
    TIGHT_TRANSITION: 1,
}


def worst_severity(clashes) -> int:
    """The worst problem in a set, as a number. 0 means nothing wrong."""
    return max((CLASH_SEVERITY.get(c.kind, 0) for c in clashes), default=0)


def _overlaps(a, b) -> bool:
    return a.start < b.when and b.start < a.when


def _same_person(a, b) -> bool:
    """Compared loosely, because carers are now free text.

    The roster says "Parent_1"; somebody typing "change the pickup to parent_1" gets
    "parent_1" recorded verbatim, as they should - it is what they wrote. Matching
    exactly would read those as two different adults and quietly find no clash,
    which is the exact failure this whole module exists to prevent. Names are
    still DISPLAYED as typed.
    """
    return bool(a) and bool(b) and a.strip().casefold() == b.strip().casefold()


def describe_clash(clash) -> str:
    """One line a human can act on, naming the person and both events.

    Says who and what, not just "conflict" - the whole point is that this is a
    coordination problem, and the answer is usually "ask someone else", which
    needs the name to be on the card.
    """
    if clash.kind == CHILD_DOUBLE_BOOKED:
        child = clash.pickups[0].child
        others = " and ".join(_activity(p) for p in clash.pickups)
        return f"{child} is booked twice at once — {others}."

    if clash.kind == UNASSIGNED:
        p = clash.pickups[0]
        return f"Nobody is rostered to collect {p.child} at {_clock(p.when)} on {p.when:%a %d %b}."

    if clash.kind == TIGHT_TRANSITION:
        first, second = sorted(clash.pickups, key=lambda p: p.when)
        gap = int((second.start - first.when).total_seconds() // 60)
        # The needed figure is what makes this actionable: "15 min" alone does
        # not say how much short it is, and the answer is usually to move one
        # event by the difference.
        need = f" — needs {clash.needed}{_by(first, second)}" if clash.needed else ""
        return (
            f"{first.child} has {gap} min to get from {_place(first)} "
            f"to {_place(second)}{need}. "
            f"{_activity(first)} ends {_clock(first.when)}, "
            f"{_activity(second)} starts {_clock(second.start)} "
            f"on {first.when:%a %d %b}."
        )

    first, second = clash.pickups[0], clash.pickups[1]
    return (
        f"{first.person} is needed twice on {first.when:%a %d %b} — "
        f"{first.child} at {_clock(first.when)} ({_activity(first)}) and "
        f"{second.child} at {_clock(second.when)} ({_activity(second)})."
    )


def summarise_clash(clash) -> str:
    """A few words, for a list where every row carries one.

    describe_clash names both events and both times, which is right on a card
    showing a single warning and unreadable in a ranked list of four. The long
    form is still what the confirm card shows once a slot is chosen, so no
    detail is lost - it is deferred.
    """
    if clash.kind == CHILD_DOUBLE_BOOKED:
        return f"{clash.pickups[0].child} is booked twice"
    if clash.kind == UNASSIGNED:
        return f"nobody rostered for {clash.pickups[0].child}"
    if clash.kind == TIGHT_TRANSITION:
        first, second = sorted(clash.pickups, key=lambda p: p.when)
        gap = int((second.start - first.when).total_seconds() // 60)
        need = f", needs {clash.needed}" if clash.needed else ""
        return f"only {gap} min from {_place(first)}{need}"
    return f"{clash.person} needed twice"


def _clock(when) -> str:
    hour = when.hour % 12 or 12
    suffix = "am" if when.hour < 12 else "pm"
    return f"{hour}.{when.minute:02d}{suffix}" if when.minute else f"{hour}{suffix}"


def _by(*pickups) -> str:
    """" min by bus" when a carer is on public transport, else " min".

    Worth saying: the same journey is 20 minutes longer for Helper_1 than for
    Parent_1, so a warning that omitted the mode would look wrong to whoever knows
    the drive takes half that.
    """
    people = _people(*pickups)
    public = [p for p in people if places.transport_for(p) == "public"]
    return " min by public transport" if public else " min"


def _place(pickup) -> str:
    """A place name for a sentence that has already said whose it is.

    "Child_1 has 15 min to get from Child_1's school to Piano" reads like two
    different Erins. The sentence names the child once, at the front, so the
    place drops the possessive.
    """
    label = places.label_for(pickup.place)
    owned = f"{pickup.child}'s "
    return label[len(owned):] if label.startswith(owned) else label


def _activity(pickup) -> str:
    """The event's name without the child/time/gate prefix the title carries."""
    activity, _ = event_parser.split_title(pickup.summary, pickup.child)
    return activity or pickup.summary


# ----- the event description block (CLAUDE.md's schema) -----

def describe_event(pickup=None, event_type=None, centre=None, notes=None) -> str:
    """Build the description block every event is supposed to carry.

    Specified in CLAUDE.md since the first commit and never actually written by
    any code path until now. Only fields with a value are emitted - a
    description full of "Notes: [none]" is worse than a short one.
    """
    lines = [
        ("Pickup", pickup),
        ("Type", event_type),
        ("Centre", centre),
        ("Notes", notes),
    ]
    return "\n".join(f"{label}: {value}" for label, value in lines if value)


_PICKUP_LINE_RE = re.compile(r"^[ \t]*Pickup:.*$", re.IGNORECASE | re.MULTILINE)


def set_pickup(description, person) -> str:
    """`description` with its Pickup: line set to `person`, everything else kept.

    Rewriting the one line rather than rebuilding the block is the whole point.
    A description is shared with the family and may hold a Centre, a Notes line,
    or a sentence somebody typed by hand explaining an arrangement; changing who
    collects a child must not quietly delete the reason why.

    Inserted at the top when absent, matching describe_event's order, so the
    answer to "who's getting her?" is the first line either way.
    """
    person = (person or "").strip()
    text = description or ""
    if not person:
        return text
    line = f"Pickup: {person}"
    if _PICKUP_LINE_RE.search(text):
        # A lambda, not the string: a name is user input, and re.sub would read
        # a backslash in it as a group reference.
        return _PICKUP_LINE_RE.sub(lambda _: line, text, count=1)
    return f"{line}\n{text}" if text else line


# ----- helpers -----

def _as_date(when):
    return when.date() if isinstance(when, datetime.datetime) else when


def _child_key(child):
    """Roster keys are the registry's lowercase child keys; callers hold
    display names ("Child_1"). Resolved through calendar_tools so a nickname the
    registry accepts works here too."""
    if not child:
        return None
    return calendar_tools.resolve_child_name(child) or str(child).strip().lower()
