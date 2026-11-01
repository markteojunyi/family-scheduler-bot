"""✏️ Change something: correcting a card that has not been written yet.

Reported live (2026-08-08): "child_1 17 Aug, 10 to 11am swimming" → ✏️ →
"change the date to 16 aug" → **"Which event would you like to change?"**, and
a bare "16 aug" was dropped entirely so the card came back looking unchanged.

The replay glued the two messages together with a full stop and re-parsed the
lot, which is genuinely ambiguous - "swimming. change the date to 16 aug" reads
as an instruction about an existing event, and there is no existing event. So
the correction is now marked, and the parser is told what the marker means.

These tests cover the SEAM (what string gets built, and what happens when the
parse still comes back wrong). Whether the model then reads the marker
correctly is a live-call question and was verified separately - see PROGRESS
§31; six phrasings all landed on one create for the right date.
"""
import asyncio
import datetime
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import event_parser as ep
import main

ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])


@pytest.fixture
def captured(monkeypatch):
    """Run handle_text with the model and the calendar stubbed out, and record
    the text that would have been parsed."""
    seen = {}
    monkeypatch.setattr(main.permissions, "get_role", lambda tid: "admin")
    monkeypatch.setattr(main.permissions, "can", lambda tid, cap: True)
    monkeypatch.setattr(main, "_begin_llm_call", AsyncMock(return_value=(True, 1)))
    monkeypatch.setattr(main, "_record_tokens", AsyncMock())
    monkeypatch.setattr(main, "_show_typing", AsyncMock())
    monkeypatch.setattr(main, "_note_friction", AsyncMock())
    monkeypatch.setattr(main, "_dispatch", AsyncMock())
    monkeypatch.setattr(main.db, "save_pending", lambda *a, **k: None)

    def fake_parse(text):
        seen["text"] = text
        return dict(seen.get("result") or {"action": "create", "status": "ok"}), None

    monkeypatch.setattr(main, "_parse_text", fake_parse)
    return seen


def _message(text):
    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = ADMIN_ID
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _run(update):
    asyncio.run(main.handle_text(update, MagicMock()))


# ----- the string that gets parsed -----

def test_an_edit_marks_the_correction(captured, monkeypatch):
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "child_1 17 Aug, 10 to 11am swimming",
                               "kind": "edit", "card": "create"} if kind == "clarification" else None,
    )
    _run(_message("change the date to 16 aug"))

    assert captured["text"] == (
        "child_1 17 Aug, 10 to 11am swimming\nCORRECTION: change the date to 16 aug"
    )


def test_answering_a_parser_question_is_not_marked(captured, monkeypatch):
    """Answering "Is this for Child_1 or Child_2?" with "Child_2" is a different thing -
    it completes a request rather than revising one, and gluing it on with a
    full stop has always worked. Marking it would change a path that is fine."""
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "swimming at 5pm"} if kind == "clarification" else None,
    )
    _run(_message("Child_2"))

    assert captured["text"] == "swimming at 5pm. Child_2"
    assert "CORRECTION" not in captured["text"]


def test_an_ordinary_message_is_untouched(captured, monkeypatch):
    monkeypatch.setattr(main.db, "pop_pending", lambda chat_id, kind: None)
    _run(_message("child_1 17 Aug, 10 to 11am swimming"))
    assert captured["text"] == "child_1 17 Aug, 10 to 11am swimming"


# ----- the parser is told what the marker means -----

def test_the_correction_rule_is_only_sent_when_there_is_a_correction():
    """It is ~90 tokens on every single message otherwise, for a rule that
    could not apply."""
    assert "CORRECTION" in ep._prompt("x", correction=True)
    assert "CORRECTION" not in ep._prompt("x", correction=False)


def test_interpret_detects_the_marker_from_the_text(monkeypatch):
    """Detected from the text rather than passed as a flag, so the parser stays
    a pure function of its input and stays testable on its own."""
    prompts = []
    monkeypatch.setattr(ep, "_call_model", lambda content: prompts.append(content) or {"action": "unknown"})
    ep.interpret(f"swimming\n{ep.CORRECTION_MARKER} 16 aug")
    assert "CORRECTION" in prompts[0]


# ----- the backstop -----

def test_a_correction_that_still_parses_as_an_amend_is_caught(captured, monkeypatch):
    """The card was a proposal, so no event exists to amend. Falling through
    would ask "Which event would you like to change?" - worse than useless,
    because it implies the event is already on a calendar."""
    captured["result"] = {"action": "amend", "status": "ok", "search_text": "swimming"}
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "child_1 17 Aug swimming", "kind": "edit",
                               "card": "create"} if kind == "clarification" else None,
    )
    update = _message("change the date to 16 aug")
    _run(update)

    main._dispatch.assert_not_awaited()
    assert "didn't catch that change" in update.message.reply_text.await_args.args[0]


def test_correcting_an_amend_card_may_still_parse_as_an_amend(captured, monkeypatch):
    """The backstop must not fire here: ✏️ on an amend card is a correction to
    a change to a REAL event, so amend is the right answer."""
    captured["result"] = {"action": "amend", "status": "ok", "search_text": "swimming"}
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "move swimming to 6pm", "kind": "edit",
                               "card": "amend"} if kind == "clarification" else None,
    )
    _run(_message("5pm actually"))

    main._dispatch.assert_awaited_once()


def test_a_correction_that_parses_as_a_create_goes_straight_through(captured, monkeypatch):
    captured["result"] = {"action": "create", "status": "ok", "title": "Child_1 11am Swimming"}
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "child_1 17 Aug swimming", "kind": "edit",
                               "card": "create"} if kind == "clarification" else None,
    )
    _run(_message("16 aug"))

    main._dispatch.assert_awaited_once()


def test_the_correction_survives_a_miss_so_it_can_be_retried(captured, monkeypatch):
    """Losing the pending state would force the user to retype the whole
    original request - which is the thing ✏️ exists to avoid."""
    saved = []
    captured["result"] = {"action": "amend", "status": "ok", "search_text": "swimming"}
    monkeypatch.setattr(
        main.db, "pop_pending",
        lambda chat_id, kind: {"text": "child_1 17 Aug swimming", "kind": "edit",
                               "card": "create"} if kind == "clarification" else None,
    )
    monkeypatch.setattr(main.db, "save_pending", lambda cid, kind, payload: saved.append((kind, payload)))
    _run(_message("change the date to 16 aug"))

    assert ("clarification", {"text": "child_1 17 Aug swimming", "kind": "edit",
                              "card": "create"}) in saved
