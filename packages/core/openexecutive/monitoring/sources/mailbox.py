"""Email-watch adapter: one person's own inbox (``monitoring.mailbox``).

Watchlist row shape:

  - ``signal_type``: ``"mailbox"``
  - ``target``: what it watches for in plain words ("sign-up emails",
    "Pat's reply"); the watch's name.
  - ``config_json``: ``{"mailbox_of": <People id>, "enrich": false}``.
  - ``trigger_json``: ``{"from": [addresses], "keywords": [words]}``.

Each poll reads the inbox mail that arrived since the watch was set or since
the last poll (a little overlap; the dedup key makes it once per email),
every tab, and emits one signal per email that matches, at HIGH, so the
person hears about it on their own channel as well as on a card. The signal
carries the sender's name and the subject only, never the email's text.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.alerts.models import AlertSeverity
from openexecutive.monitoring import mailbox, store
from openexecutive.monitoring.models import SOURCE_KIND_MAILBOX, Signal, WatchlistItem
from openexecutive.monitoring.sources.base import observe

logger = logging.getLogger(__name__)

# Mail read per page, and pages per poll: a poll reads back page by page to
# where it last read (one more page to see there is nothing older), so a busy inbox or a day disconnected loses nothing,
# up to this many emails at once.
_READ_LIMIT = 50
_MAX_PAGES = 20
# New emails told per poll, oldest first. Each is a card and a triage call,
# and triage takes about a minute's worth at once; the rest wait for the
# next poll (the read point stops at the last one told), so a backlog after
# a day disconnected comes through in order, a few minutes apart.
PER_POLL = 20
# Re-read this far before the last poll: mail the provider listed a little
# late is still caught (the dedup key makes a re-read email a no-op).
_OVERLAP = timedelta(minutes=10)
_SUMMARY_MAX = 200
NOT_CONNECTED = "Couldn't read your inbox. Connect your mailbox under Act as me, and it will try again."
NO_OWNER = "This watch has no inbox to read."
READ_THROUGH_KEY = mailbox.READ_THROUGH_KEY
# ``config_json`` key: where the last poll that told emails read to, and the
# dedup keys of those emails. It becomes the read point only once all of
# them are recorded, so a tick cut off before it recorded them (a restart, a
# locked database) reads them again rather than skipping them.
PENDING_KEY = "read_pending"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_through(item: WatchlistItem, *, db_path: Path | None) -> datetime | None:
    """Where the last read that worked read to (``READ_THROUGH_KEY``), moved
    on to the last poll's point (``PENDING_KEY``) once every email that poll
    told is recorded."""
    config = item.config_json or {}
    read = _parse(str(config.get(READ_THROUGH_KEY) or ""))
    pending = config.get(PENDING_KEY)
    if not isinstance(pending, dict):
        return read
    through = _parse(str(pending.get("through") or ""))
    keys = [str(k) for k in pending.get("keys") or []]
    if through is None or len(store.recorded_dedup_keys(keys, db_path=db_path)) < len(set(keys)):
        return read
    return through if read is None else max(read, through)


def _start(item: WatchlistItem, now: datetime) -> datetime:
    """The oldest mail it may tell: from when the watch was set, or when it
    was last turned back on (``mailbox.READ_FROM_KEY``)."""
    created = _parse(item.created_at) or now
    turned_on = _parse(str((item.config_json or {}).get(mailbox.READ_FROM_KEY) or ""))
    return max(created, turned_on) if turned_on is not None else created


def _since(item: WatchlistItem, now: datetime, read: datetime | None) -> datetime:
    """From when to read: a little before the last read that worked
    (``read``), else from its start (``_start``), never before that. Not
    the last poll: a poll that couldn't read (inbox not connected, the mail
    service down) must not move it on, or the mail that came meanwhile is
    skipped."""
    start = _start(item, now)
    return max(start, read - _OVERLAP) if read is not None else start


async def _read_back_to(client: Any, since: datetime) -> list[Any]:
    """Every inbox email since ``since``: the newest page, then the page
    before its oldest email, and so on until a page has nothing new (up to
    ``_MAX_PAGES``). Not until a page comes back short: the clients drop the
    person's own mail and mail deleted since it was listed, so a short page
    can still have older mail behind it. A page boundary is read twice (the
    mail services count in whole seconds); each email is kept once."""
    received: list[Any] = []
    seen: set[str] = set()
    before: datetime | None = None
    for _ in range(_MAX_PAGES):
        page = await client.received_since(since, before=before, limit=_READ_LIMIT)
        new = [m for m in page if m.id not in seen]
        seen.update(m.id for m in new)
        received += new
        stamps = [t for m in page if (t := _parse(getattr(m, "received_at", "") or "")) is not None]
        if not new or not stamps:
            break
        before = min(stamps) + timedelta(seconds=1)
    else:
        logger.warning("email watch: more than %d emails since %s; read the newest", _READ_LIMIT * _MAX_PAGES, since)
    return received


def _dedup_key(watch_id: int, message: Any) -> str:
    return f"mailbox:{watch_id}:{message.id}"


def summary_of(message: Any) -> str:
    """Who it is from and its subject, cleaned: one line, no links or
    addresses (``delegation.reminders.clean_text``)."""
    from openexecutive.delegation.reminders import clean_text

    who = clean_text(str(getattr(message, "from_name", "") or "")) or "Someone"
    subject = clean_text(str(getattr(message, "subject", "") or ""))
    line = f"{who}: {subject}" if subject else f"{who} (no subject)"
    return line[:_SUMMARY_MAX]


class MailboxSource:
    kind: str = SOURCE_KIND_MAILBOX
    # The old one-off email watch checked every 5 minutes; so does this.
    default_poll_interval_minutes: int = 5
    # Only mail from after the watch was set is read, so nothing older can
    # replay: no baseline needed.
    seed_on_first_poll: bool = False

    async def poll(self, item: WatchlistItem, *, db_path: Path | None = None) -> list[Signal]:
        from openexecutive.delegation.gmail import (
            gmail_for,
            gmail_status,
            mailbox_link,
            normalize_email,
        )
        from openexecutive.people.store import get_person

        owner = mailbox.owner_id(item)
        person = get_person(owner) if owner is not None else None
        if person is None or person.archived or item.id is None:
            observe(item, NO_OWNER)
            return []
        email = normalize_email(person.email)
        if await gmail_status(email) != "connected":
            observe(item, NOT_CONNECTED)
            return []
        now = datetime.now(UTC)
        read = _read_through(item, db_path=db_path)
        since = _since(item, now, read)
        start = _start(item, now)
        received = await _read_back_to(gmail_for(email), since)
        found: list[tuple[datetime, Any]] = []
        for message in received:
            arrived = _parse(getattr(message, "received_at", "") or "")
            if arrived is not None and arrived < start:
                continue
            if mailbox.matches(
                item.trigger_json,
                from_addr=normalize_email(getattr(message, "from_addr", "") or ""),
                from_name=str(getattr(message, "from_name", "") or ""),
                subject=str(getattr(message, "subject", "") or ""),
            ):
                found.append((arrived or now, message))
        # Only what isn't told yet (the overlap re-reads a few), oldest first.
        told = store.recorded_dedup_keys([_dedup_key(item.id, m) for _, m in found], db_path=db_path)
        new = sorted(((at, m) for at, m in found if _dedup_key(item.id, m) not in told), key=lambda f: f[0])
        # Read: the next poll starts from here, or, when more is new than one
        # poll tells, from the last email it told (see PER_POLL). With
        # nothing to tell, at once; else once what it tells is recorded.
        read_to = new[PER_POLL - 1][0] if len(new) > PER_POLL else now
        if new:
            if read is not None:
                store.set_watchlist_config_key(item.id, READ_THROUGH_KEY, read.isoformat(), db_path=db_path)
            store.set_watchlist_config_key(item.id, PENDING_KEY, {
                "through": read_to.isoformat(),
                "keys": [_dedup_key(item.id, m) for _, m in new[:PER_POLL]],
            }, db_path=db_path)
        else:
            store.set_watchlist_config_key(item.id, READ_THROUGH_KEY, read_to.isoformat(), db_path=db_path)
            store.set_watchlist_config_key(item.id, PENDING_KEY, None, db_path=db_path)
        signals: list[Signal] = []
        for arrived, message in new[:PER_POLL]:
            link = mailbox_link(email, thread_id=message.thread_id or None, message_id=message.id)
            signals.append(Signal(
                watchlist_id=item.id,
                source_kind=SOURCE_KIND_MAILBOX,
                source_external_id=message.id,
                captured_at=now.isoformat(),
                published_at=arrived.isoformat(),
                normalized_summary=summary_of(message),
                raw_payload={},
                provenance_url=link if link.startswith("https://") else "https://mail.google.com/",
                severity_hint=AlertSeverity.HIGH,
                dedup_key=_dedup_key(item.id, message),
            ))
        if not signals:
            observe(item, "Nothing new matched." if received else "No new email.")
        return signals

    def matches_trigger(self, signal: Signal, item: WatchlistItem) -> bool:
        # Matched in poll, where the sender and subject are at hand.
        return True
