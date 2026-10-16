"""Where in Slack a session's conversation is — for the agent having it.

The caller envelope's `conversation.slack` (apps/harness/caller_context.py) is
built from this: the channel id and name, the thread's ts and its permalink. An
agent asked to "post a clean thread in the channel" must not have to ask which
channel it is in (2026-10-16, an ACE turn).

**Read from the session, never fetched while building.** The envelope is built on
every claim and every `who_is_asking`, so `location_of` makes no Slack call. The
two facts Slack has to be asked for — the channel's NAME (`conversations.info`,
which needs `channels:read` / `groups:read`) and the thread's PERMALINK
(`chat.getPermalink`) — are fetched once per thread by `fill` when a message
arrives or a thread is bound, and cached on the session under `LOCATION_KEY`,
keyed by the thread it describes so a move or a re-bind can never serve the old
thread's name. Without `channels:read` the name stays null (the id is always
there, and `<#C…>` renders as the name in Slack); without a permalink the
web-client link to the same thread stands in.
"""
from __future__ import annotations

import logging
import time

from django.db import transaction

logger = logging.getLogger(__name__)

#: The cache on `Session.metadata`: {thread, channel_name, permalink, checked_at}.
LOCATION_KEY = "slack_location"
#: How long a lookup that could not name the channel is trusted before asking again
#: (an install that has since been re-approved with `channels:read`).
RETRY_SECONDS = 6 * 3600
#: Slack lookups are cosmetic here: never hold a message up for long over one.
LOOKUP_TIMEOUT = 3


def _binding(session) -> tuple[str, str, str, str] | None:
    from .services import DM_ANCHOR, SLACK_THREAD_KEY

    meta = (getattr(session, "metadata", None) or {}) if session is not None else {}
    key = meta.get(SLACK_THREAD_KEY)
    channel = str(meta.get("slack_channel") or "")
    if not key or not channel:
        return None
    thread_ts = str(meta.get("slack_thread_ts") or "")
    if thread_ts == DM_ANCHOR:
        thread_ts = ""
    return str(key), str(meta.get("slack_team") or ""), channel, thread_ts


def _is_dm(channel_id: str, thread_ts: str, key: str) -> bool:
    from .services import DM_ANCHOR

    return channel_id.startswith("D") or key.endswith(f":{DM_ANCHOR}")


def web_url(team: str, channel: str, thread_ts: str) -> str:
    """The web-client link to a channel or thread — needs no API call."""
    if not (team and channel):
        return ""
    url = f"https://app.slack.com/client/{team}/{channel}"
    return f"{url}/thread/{channel}-{thread_ts}" if thread_ts else url


def location_of(session) -> dict | None:
    """`{team_id, channel_id, channel_name, thread_ts, permalink, is_dm}`, or None
    when the session holds no Slack thread. A pure read — no Slack call."""
    bound = _binding(session)
    if bound is None:
        return None
    key, team, channel, thread_ts = bound
    cached = (session.metadata or {}).get(LOCATION_KEY) or {}
    if not isinstance(cached, dict) or cached.get("thread") != key:
        cached = {}
    dm = _is_dm(channel, thread_ts, key)
    permalink = str(cached.get("permalink") or "") or web_url(team, channel, thread_ts)
    return {
        "team_id": team or None,
        "channel_id": channel,
        "channel_name": (str(cached.get("channel_name") or "") or None) if not dm else None,
        "thread_ts": thread_ts or None,
        "permalink": permalink or None,
        "is_dm": dm,
    }


def remember(session, *, channel_name: str = "", permalink: str = "") -> None:
    """Cache what is already known about the session's CURRENT thread (no Slack call).

    Keeps what the cache already holds for the same thread; a different thread
    starts a fresh cache."""
    bound = _binding(session)
    if bound is None or not (channel_name or permalink):
        return
    _write(session, bound[0], channel_name=channel_name, permalink=permalink, checked=False)


def _write(session, key: str, *, channel_name: str, permalink: str, checked: bool) -> None:
    from apps.canopy_sessions.models import Session

    with transaction.atomic():
        locked = Session.objects.select_for_update().filter(pk=session.pk).first()
        if locked is None:
            return
        meta = dict(locked.metadata or {})
        from .services import SLACK_THREAD_KEY

        if meta.get(SLACK_THREAD_KEY) != key:
            return      # the thread moved while we were asking Slack about the old one
        cached = meta.get(LOCATION_KEY) if isinstance(meta.get(LOCATION_KEY), dict) else {}
        if cached.get("thread") != key:
            cached = {}
        cached = {**cached, "thread": key}
        if channel_name:
            cached["channel_name"] = channel_name
        if permalink:
            cached["permalink"] = permalink
        if checked:
            cached["checked_at"] = time.time()
        meta[LOCATION_KEY] = cached
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = meta


def fill(session, installation=None) -> None:
    """Ask Slack for what the cache lacks about this session's thread. Best-effort.

    Called when a message arrives in, or a share binds, a thread — never while an
    envelope is being built. Asks at most once per `RETRY_SECONDS` when Slack
    could not answer (an install without `channels:read`), and never again once
    both facts are cached.
    """
    from . import client
    from .services import installation_for

    bound = _binding(session)
    if bound is None:
        return
    key, team, channel, thread_ts = bound
    cached = (session.metadata or {}).get(LOCATION_KEY) or {}
    if not isinstance(cached, dict) or cached.get("thread") != key:
        cached = {}
    dm = _is_dm(channel, thread_ts, key)
    need_name = not dm and not cached.get("channel_name")
    need_link = bool(thread_ts) and not cached.get("permalink")
    if not (need_name or need_link):
        return
    if time.time() - float(cached.get("checked_at") or 0) < RETRY_SECONDS:
        return
    installation = installation or installation_for(team)
    if installation is None:
        return
    name = link = ""
    try:
        if need_name:
            name = client.channel_name(installation.bot_token, channel, timeout=LOOKUP_TIMEOUT)
        if need_link:
            link = client.permalink(installation.bot_token, channel=channel, ts=thread_ts,
                                    timeout=LOOKUP_TIMEOUT)
    except Exception:  # noqa: BLE001 — a name we could not learn is not worth failing a message over
        logger.exception("could not look up a Slack thread's location")
    _write(session, key, channel_name=name, permalink=link, checked=True)
