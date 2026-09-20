"""Compact open-alert digest for the chat user turn.

The ``/today`` page shows a synthesized "What's going on" narrative whose items
(e.g. "Gulf Coast Port Cyberattack") are drawn from the open alerts queue — the
same rows that render as proposal cards below it. But ``/chat`` never saw that
data, so when the principal clicked a briefing item (or just typed its name) the
Executive had no record of it and couldn't discuss it.

This renders the current open alerts into a compact ``<briefing>`` block that the
chat route injects into the **user turn** (never a cached system block, so prompt
caching is unaffected). It mirrors how ``/today`` builds proposals
(`api/routes/today.py`): company-wide, live ``unread`` rows (inside TTL,
not snoozed — see ``alerts.lifecycle.list_live_alerts``).
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Cap each alert body so a wordy proposal can't blow the context budget; the
# headline + suggested_action carry the gist and the user can open the card for
# the full text.
_BODY_SNIPPET_CHARS = 200

# Cap how many open alerts the digest carries. The board rarely holds this many
# unread items at once; the ceiling just bounds the per-turn token cost.
_MAX_ALERTS = 30


def _one_line(value: str | None) -> str:
    """Collapse a field to a single line of single-spaced text.

    EVERY interpolated field must go through this. Alert headlines, suggested
    actions, tags and review notes all originate in inbound email and chat, so
    they are attacker-controlled; a newline in any of them lets the sender
    forge an extra line in this block. That matters because a line starting
    `[N]` is one of only two sources `ack_alert` is told to trust, so a forged
    line is a forged instruction to clear somebody else's alert. Only `body`
    was being stripped.
    """
    return " ".join((value or "").split())


def format_open_alerts_for_prompt(
    db_path: Path | None = None,
    limit: int = _MAX_ALERTS,
    rendered_ids: list[int] | None = None,
) -> str:
    """Render current open (unread) alerts as a compact digest, or ``""`` when none.

    One line per alert::

        [<id>] (<category>) <headline> — <body snippet> | suggested: ... | tags: ...

    Company-wide and scoped to ``status="unread"``, mirroring ``/today``'s
    proposal build. ``category`` (``action``/``monitoring``) comes from
    :func:`openexecutive.briefing.ranking.score_and_categorize` so the Executive
    can tell an item awaiting a decision from a passive monitoring signal.

    ``rendered_ids``, when given, is filled with the alert ids this block
    actually names — the caller records them on the session so ``ack_alert``
    can refuse an id the model did not get from here. Prompt wording alone is
    not a control.

    Pure synchronous SQLite read — wrap in ``asyncio.to_thread`` at the call
    site. Never raises: any failure logs and returns ``""`` so a chat turn is
    never blocked by an alerts-store hiccup.
    """
    from openexecutive.alerts.lifecycle import list_live_alerts
    from openexecutive.briefing.ranking import score_and_categorize

    try:
        # Fetch one more than we render so we can tell a full board from a
        # truncated one. Without this the header below claims the list is the
        # whole board even when it is the most recent `limit` of many more —
        # the Executive then tells the principal a partial set is everything
        # (#136, second symptom).
        alerts = list_live_alerts(limit=limit + 1, db_path=db_path)
    except Exception:
        logger.exception("briefing_context.list_alerts_failed")
        return ""

    truncated = len(alerts) > limit
    alerts = alerts[:limit]

    lines: list[str] = []
    for alert in alerts:
        _score, category, _reason = score_and_categorize(alert)
        body = _one_line(alert.body)
        if len(body) > _BODY_SNIPPET_CHARS:
            body = body[:_BODY_SNIPPET_CHARS].rstrip() + "…"
        line = f"[{alert.id}] ({category}) {_one_line(alert.headline)}"
        if body:
            line += f" — {body}"
        if alert.suggested_action:
            line += f" | suggested: {_one_line(alert.suggested_action)}"
        if alert.topic_tags:
            tags = ", ".join(_one_line(t) for t in alert.topic_tags)
            line += f" | tags: {tags}"
        if alert.review_verdict:
            review = f" | review: {_one_line(alert.review_verdict)}"
            if alert.review_note:
                review += f" — {_one_line(alert.review_note)}"
            if alert.recommended_move and alert.recommended_move != "none":
                review += f" | next move: {_one_line(alert.recommended_move)}"
            line += review
        if alert.occurrence_count > 1:
            line += f" | seen x{alert.occurrence_count}"
        lines.append(line)
        if rendered_ids is not None and alert.id is not None:
            rendered_ids.append(alert.id)

    if not lines:
        return ""

    header = (
        "Open items currently on the briefing board — the principal sees these "
        "as cards and as the 'What's going on' summary on /today. Each line is "
        "[alert_id] (category) headline — details. When the user asks about one "
        "of these by name, this is what they mean."
    )
    if truncated:
        header += (
            f" NOTE: this is only the {len(lines)} most recent open items, not "
            "the complete board — there are more. Do not describe this list as "
            "everything that is open; say it is the most recent slice and point "
            "the principal at the briefing page for the rest."
        )
    return header + "\n" + "\n".join(lines)


__all__ = ["format_open_alerts_for_prompt"]
