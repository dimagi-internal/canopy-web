"""A Claude subscription usage limit: recognise one, and read WHEN it resets.

A capped login takes out every turn on the box until its window rolls over, and
Claude Code says exactly when that is — "You've hit your session limit · resets
2:30am (America/Denver)". The runners use this to park the box until then
(`POST /runners/{id}/pause` with `until`), so routing sends the work to a runner
that can still spend tokens instead of feeding turns into a wall.

**Getting the reset wrong is silent, which is why it is code.** Too early and
the box un-parks into the same cap and burns a turn rediscovering it; too late
and a healthy box sits idle. So the parser is conservative about what it
accepts, and a message it cannot read yields `None` — the caller then picks a
short default and lets the next cap re-park it, rather than trusting a guess.

The shapes, all observed in the shipped CLI or real transcripts:

    You've hit your session limit · resets 2:30am (America/Denver)
    You've hit your weekly limit · resets Aug 3, 11pm (UTC)
    You've hit your limit · resets 3pm (America/New_York)
    Claude AI usage limit reached|1754000000          (older CLIs: an epoch)

Stdlib only, like the rest of this package: the cloud runner ships it as a bare
directory onto an EC2 box.
"""
from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: A cap is matched by SHAPE — a hit/reached verb with "limit" close behind —
#: because Anthropic words each window (session, daily, weekly) a little
#: differently and adds new ones. Mirrors `cloud_runner._USAGE_CAP_RE`, which
#: keeps its own copy because that runner must recognise a cap even when this
#: package failed to import on the box.
_CAP_RE = re.compile(
    r"\b(?:hit|reached)\b[^.\n]{0,40}?\blimits?\b|\blimits?\s+reached\b",
    re.IGNORECASE,
)

#: "…limit reached|1754000000" — the pre-2026 CLI wrote the reset as an epoch.
_EPOCH_RE = re.compile(r"limit reached\|(\d{10})\b", re.IGNORECASE)

#: "resets 2:30am (America/Denver)", "resets Aug 3, 11pm (UTC)", "resets 3pm".
_RESETS_RE = re.compile(
    r"\bresets?\s+(?:at\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)"
    r"(?:\s*\((?P<tz>[A-Za-z0-9_+\-/]+)\))?",
    re.IGNORECASE,
)

#: "resets in 2h 15m" / "resets in 45 minutes" — not seen yet, cheap to accept.
_RESETS_IN_RE = re.compile(
    r"\bresets?\s+in\s+(?:(?P<h>\d+)\s*h(?:ours?|rs?)?)?\s*(?:(?P<m>\d+)\s*m(?:in(?:utes?)?)?)?",
    re.IGNORECASE,
)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}

#: The longest window Anthropic ships is a week. A parsed reset further out than
#: this is a misread, and parking a box for it would idle it for nothing.
MAX_HORIZON = dt.timedelta(days=8)


def is_usage_limit(text: str) -> bool:
    """Does this text say the subscription is capped?"""
    return bool(text) and bool(_CAP_RE.search(text))


def reset_at(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """When the cap in `text` lifts, as an aware UTC datetime — or None.

    A wall-clock time with no date means the NEXT occurrence of it (a session
    cap at 1:38am that "resets 2:30am" lifts in 52 minutes; one that says
    "resets 1am" at 1:38am lifts tomorrow). With no zone named, the runner's own
    local zone is assumed, because that is the zone the CLI printed it in.
    """
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    if not text:
        return None

    m = _EPOCH_RE.search(text)
    if m:
        return _bounded(dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc), now)

    m = _RESETS_IN_RE.search(text)
    if m and (m.group("h") or m.group("m")):
        delta = dt.timedelta(hours=int(m.group("h") or 0), minutes=int(m.group("m") or 0))
        return _bounded(now + delta, now)

    m = _RESETS_RE.search(text)
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("min") or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if m.group("ampm").lower().startswith("p") else 0)

    tz = _zone(m.group("tz"))
    local_now = now.astimezone(tz)
    if m.group("mon"):
        month = _MONTHS.get(m.group("mon")[:3].lower())
        if month is None:
            return None
        try:
            when = local_now.replace(month=month, day=int(m.group("day")), hour=hour,
                                     minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if when <= local_now - dt.timedelta(days=1):
            # "Jan 2" read on Dec 30 — the date is next year's.
            try:
                when = when.replace(year=when.year + 1)
            except ValueError:
                return None
    else:
        when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= local_now:
            when += dt.timedelta(days=1)
    return _bounded(when.astimezone(dt.timezone.utc), now)


def limit_in_record(record) -> str | None:
    """The cap message, if this transcript record IS Claude Code's cap notice.

    Claude Code writes a cap as a synthetic assistant message flagged
    `isApiErrorMessage` (and, since late 2026, `error: "rate_limit"`) — observed
    2026-10-08 on this fleet: `{"type": "assistant", "isApiErrorMessage": true,
    "error": "rate_limit", "message": {"model": "<synthetic>", "content":
    [{"type": "text", "text": "You've hit your session limit · resets 2:30am
    (America/Denver)"}]}}`.

    Requires the API-error flag, never the text alone: an agent that merely
    WRITES "you've hit your limit" in a reply must not park the box.
    """
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    if not record.get("isApiErrorMessage"):
        return None
    text = _record_text(record)
    if record.get("error") == "rate_limit" or is_usage_limit(text):
        return text or "usage limit reached"
    return None


def limit_at_transcript_end(path) -> tuple[str, str] | None:
    """(record uuid, cap message) when the conversation in this transcript last
    ended on a usage cap, else None. The uuid lets a caller act once per cap
    rather than once per hook that happens to re-read the same tail."""
    from .records import read_tail_records  # noqa: PLC0415

    for record in reversed(read_tail_records(path)):
        if record.get("type") in ("system", "queue-operation", "summary") \
                or record.get("isSidechain"):
            continue
        text = limit_in_record(record)
        return (str(record.get("uuid") or record.get("timestamp") or ""), text) if text else None
    return None


def _record_text(record: dict) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _zone(name: str | None) -> dt.tzinfo:
    if name:
        if name.upper() in ("UTC", "GMT", "Z"):
            return dt.timezone.utc
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def _bounded(when: dt.datetime, now: dt.datetime) -> dt.datetime | None:
    if when <= now or when - now > MAX_HORIZON:
        return None
    return when
