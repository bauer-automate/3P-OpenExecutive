"""Email watches: one person's own inbox on the watch list.

"Tell me every time a sign-up email comes in", "ping me if Pat replies":
a watch of kind ``mailbox`` (``SOURCE_KIND_MAILBOX``) reads the inbox of the
person who set it up (``mailbox_of`` in its config, their People id) through
Act as me's own mailbox client (``delegation.gmail.gmail_for``, Gmail or
Outlook), and tells them about every email that matches, until they turn it
off. Its trigger says what matches:

- ``from``: sender addresses; an email from one of them matches;
- ``keywords``: words; an email whose subject or sender names one matches.

With both, an email must meet both. With neither, it matches nothing (a
watch on every email is what the inbox itself is).

Private, like the mail it reads: only its owner sees it on the watch list
and in the Executive's tools (``shown_to``), its cards go to them and no one
else, whoever the watch was set to tell (``alerts.models.PERSONAL_SOURCES``,
``monitoring.pipeline``), and its audit rows are private to them
(``audit_privacy``). Privacy hides it from everyone else; it never stops it.
"""
from __future__ import annotations

import re
from typing import Any

from openexecutive.monitoring.models import SOURCE_KIND_MAILBOX, WatchlistItem

OWNER_KEY = "mailbox_of"
FROM_KEY = "from"
KEYWORDS_KEY = "keywords"
# ``config_json`` key: when the last read that worked began
# (``sources.mailbox``); the next read starts a little before it.
READ_THROUGH_KEY = "read_through"
# ``config_json`` key: when it was last turned back on. Nothing that came
# before is read, as nothing from before the watch was set is.
READ_FROM_KEY = "read_from"
MAX_SENDERS = 10
MAX_KEYWORDS = 10
_KEYWORD_MAX_CHARS = 60
# The longest watch name (``monitoring.validation.WATCHLIST_SLUG_RE``).
_SLUG_MAX = 61
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_WORD_RE = re.compile(r"[\W_]+")


def is_mailbox(item: WatchlistItem) -> bool:
    return item.signal_type == SOURCE_KIND_MAILBOX


def owner_id(item: WatchlistItem) -> int | None:
    """The People id whose inbox this watch reads, or None (not an email
    watch, or a row with no owner, which reads nothing)."""
    if not is_mailbox(item):
        return None
    raw = (item.config_json or {}).get(OWNER_KEY)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def owner_config(config: dict[str, Any], person_id: int) -> dict[str, Any]:
    """``config`` for a new email watch on ``person_id``'s inbox. Matches are
    the person's own words, so the company-relevance read is off for it."""
    return {**config, OWNER_KEY: int(person_id), "enrich": False}


def shown_to(item: WatchlistItem, person_id: int | None) -> bool:
    """Whether the person ``person_id`` (None: nobody in particular, a
    surface everyone reads) may see this watch. Every watch but an email
    watch is the company's; an email watch is its owner's alone."""
    if not is_mailbox(item):
        return True
    return person_id is not None and owner_id(item) == person_id


def own_slug(slug: str) -> str:
    """The name a new email watch is stored under: the name asked for plus
    a random ending, so no one else's watch name ever runs into it, and
    what someone else is watching their inbox for can't be found by trying
    names."""
    import secrets

    suffix = f"-{secrets.token_hex(3)}"
    return f"{slug[: _SLUG_MAX - len(suffix)]}{suffix}"


def audit_privacy(item: WatchlistItem) -> dict[str, Any]:
    """The ``audit.log_event`` keywords for a row about this watch: an email
    watch's rows name a sender and a subject, so they are its owner's
    alone; every other watch's rows are the company's (no keywords)."""
    if not is_mailbox(item):
        return {}
    return {"private": True, "private_to_person": owner_id(item)}


def _words(values: object, *, cap: int, each: int) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values:
        word = " ".join(v.split())[:each] if isinstance(v, str) else ""
        if word and word.lower() not in {o.lower() for o in out}:
            out.append(word)
    return out[:cap]


def clean_trigger(raw: object) -> dict[str, Any]:
    """The trigger as stored: real addresses, lowercased, and plain words."""
    if not isinstance(raw, dict):
        return {}
    senders = [a.lower() for a in _words(raw.get(FROM_KEY), cap=MAX_SENDERS, each=254) if _ADDRESS_RE.match(a)]
    keywords = _words(raw.get(KEYWORDS_KEY), cap=MAX_KEYWORDS, each=_KEYWORD_MAX_CHARS)
    out: dict[str, Any] = {}
    if senders:
        out[FROM_KEY] = senders
    if keywords:
        out[KEYWORDS_KEY] = keywords
    return out


def matches(trigger: dict[str, Any], *, from_addr: str, from_name: str, subject: str) -> bool:
    """Whether an email meets the watch's trigger (see the module doc)."""
    senders = [str(a).lower() for a in trigger.get(FROM_KEY) or []]
    keywords = [str(k).lower() for k in trigger.get(KEYWORDS_KEY) or [] if str(k).strip()]
    if not senders and not keywords:
        return False
    if senders and (from_addr or "").strip().lower() not in senders:
        return False
    if keywords:
        # Whole words only, punctuation as a space: "ai" is not in "gmail",
        # "sign up" is in "Sign-up:", and "stripe" is in "billing@stripe.com".
        haystack = f" {_plain_words(' '.join((subject or '', from_name or '', from_addr or '')))} "
        if not any(f" {k} " in haystack for k in map(_plain_words, keywords) if k):
            return False
    return True


def _plain_words(text: str) -> str:
    """``text`` lowercased, with every run of non-word characters one space."""
    return " ".join(_NON_WORD_RE.split(text.lower())).strip()


__all__ = [
    "FROM_KEY",
    "KEYWORDS_KEY",
    "OWNER_KEY",
    "READ_FROM_KEY",
    "READ_THROUGH_KEY",
    "audit_privacy",
    "clean_trigger",
    "own_slug",
    "is_mailbox",
    "matches",
    "owner_config",
    "owner_id",
    "shown_to",
]
