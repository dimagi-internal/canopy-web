"""Who is in a Slack-bound session: the people in the Slack conversation it lives in.

A session is visible to the people who were in the conversation where it happened
(docs/architecture/access.md § Who is in a session). In Slack that is everyone in
the channel (for a DM or group DM, its people): any of them can read the thread and
write into it there, so in canopy each linked canopy MEMBER of the session's
workspace is an EDITOR participant — full access, as in Slack. Contacts and guests
stay as they are: they see the thread in Slack only.

Kept in step with Slack, cheaply:

* **On each inbound message** into a session's thread (`sync`, from
  `services.thread_session` and a shared session's reply): the poster joins, as
  before, and so does every linked member Slack lists in the conversation.
* **A periodic sweep** (`sweep`, on the off-request clock every
  `SWEEP_EVERY_S`) does the same for sessions active in the last `ACTIVE_DAYS`, so
  someone who joins or leaves the channel without posting is caught too.
* Slack is asked once per conversation per `MEMBERS_TTL_S` (`conversations.members`,
  cached), and the team directory (`users.list`, to link members who never posted
  by their verified email, `services.auto_link`) once per `DIRECTORY_TTL_S`, and
  only when the channel holds someone not yet linked.

Leaving: a participant the CHANNEL brought in (`SlackAudienceMember`) is removed
when Slack no longer lists them in the conversation. Someone the owner shared the
session with is never touched, and nothing is removed when Slack could not answer
(an error, a missing scope, or a list without the bot in it): unknown is not "gone".
Never into a sensitive conversation (`ensure_participant` refuses one; this says
so first, and leaves its people alone).
"""
from __future__ import annotations

import datetime as dt
import logging

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.canopy_sessions.participants import ensure_participant
from apps.harness import sensitivity
from apps.workspaces import services as wsvc

from . import client
from .models import SlackAudienceMember, SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

MEMBERS_TTL_S = 5 * 60
DIRECTORY_TTL_S = 60 * 60
SWEEP_EVERY_S = 10 * 60
ACTIVE_DAYS = 14
#: At most this many conversations asked of Slack per sweep (most recently active first).
SWEEP_MAX_CHANNELS = 200
_SLACK_THREAD_KEY = "slack_thread"   # services.SLACK_THREAD_KEY (services imports this module)


def _members_key(team_id: str, channel_id: str) -> str:
    return f"slack:audience:{team_id}:{channel_id}"


def forget(team_id: str, channel_id: str) -> None:
    """Drop the cached people of a conversation, so the next sync asks Slack."""
    cache.delete(_members_key(team_id, channel_id))


def channel_people(installation: SlackInstallation, channel_id: str) -> set[int] | None:
    """The canopy users linked to someone in this Slack conversation now — or None
    when Slack cannot say (then nobody is added from the list and nobody removed).

    Cached per conversation for `MEMBERS_TTL_S`; an unknown answer is not cached."""
    if not channel_id:
        return None
    key = _members_key(installation.team_id, channel_id)
    hit = cache.get(key)
    if hit is not None:
        return set(hit)
    try:
        ids = client.conversation_members(installation.bot_token, channel_id)
    except Exception:  # noqa: BLE001 — Slack unreachable or a missing scope: unknown
        logger.info("slack audience: conversations.members failed for %s", channel_id, exc_info=True)
        return None
    if installation.bot_user_id not in ids:
        # The bot is in every conversation it hears from; a list without it is
        # not one to act on (and removing people on it would be wrong).
        return None
    linked = dict(SlackUserLink.objects.filter(installation=installation, slack_user_id__in=ids)
                  .values_list("slack_user_id", "user_id"))
    unlinked = [i for i in ids if i not in linked and i != installation.bot_user_id]
    if unlinked:
        linked.update(_link_from_directory(installation, unlinked))
    people = set(linked.values())
    cache.set(key, sorted(people), MEMBERS_TTL_S)
    return people


def _link_from_directory(installation: SlackInstallation, slack_ids: list[str]) -> dict[str, int]:
    """Link the full members of this Slack among `slack_ids` to the canopy user who
    provably holds their profile email — the same `auto_link` a first message makes."""
    from .services import auto_link  # noqa: PLC0415 — cycle: services imports this module

    directory = _directory(installation)
    out = {}
    for sid in slack_ids:
        info = directory.get(sid)
        if not info:
            continue
        user = auto_link(installation, sid, info)
        if user is not None:
            out[sid] = user.pk
    return out


def _directory(installation: SlackInstallation) -> dict[str, dict]:
    """{slack user id: user object} for the whole Slack, cached; {} when unreachable."""
    key = f"slack:directory:{installation.team_id}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        users = client.team_users(installation.bot_token)
    except Exception:  # noqa: BLE001 — degrade to "only people already linked"
        logger.info("slack audience: users.list failed for %s", installation.team_id, exc_info=True)
        return {}
    found = {str(u.get("id")): u for u in users if u.get("id")}
    cache.set(key, found, DIRECTORY_TTL_S)
    return found


def _people_of(workspace_id: str, user_ids: set[int]) -> set[int]:
    """The members of `workspace_id` among `user_ids` who are people (not an agent's
    login), read once for all of them."""
    if not user_ids:
        return set()
    users = list(get_user_model().objects.filter(pk__in=user_ids, is_active=True))
    roles = wsvc.member_roles_of(users)
    bots = set(Agent.objects.filter(user_id__in=user_ids).values_list("user_id", flat=True))
    return {u.pk for u in users if workspace_id in roles.get(u.pk, {}) and u.pk not in bots}


def sync(session: Session | None, installation: SlackInstallation | None, *, poster=None) -> None:
    """Bring `session`'s participants in step with its Slack conversation: the poster
    (who is plainly in it) and every linked member Slack lists join as editors; a
    member the channel brought in who is no longer in it leaves."""
    if session is None or installation is None or sensitivity.is_sensitive(session):
        return
    meta = session.metadata or {}
    people = channel_people(installation, str(meta.get("slack_channel") or ""))
    wanted = set(people or ())
    if poster is not None and getattr(poster, "pk", None):
        wanted.add(poster.pk)
    wanted.discard(session.created_by_id)
    members = _people_of(session.workspace_id, wanted)
    present = set(SessionParticipant.objects.filter(session=session).values_list("user_id", flat=True))
    held = set(SlackAudienceMember.objects.filter(session=session).values_list("user_id", flat=True))
    users = {u.pk: u for u in get_user_model().objects.filter(pk__in=members - present)}
    for uid, user in users.items():
        if ensure_participant(session, user, SessionParticipant.EDITOR) is not None:
            SlackAudienceMember.objects.get_or_create(session=session, user_id=uid)
    if people is None:
        return
    gone = held - people - ({poster.pk} if poster is not None else set())
    if gone:
        (SessionParticipant.objects.filter(session=session, user_id__in=gone)
         .exclude(role=SessionParticipant.OWNER).delete())
        SlackAudienceMember.objects.filter(session=session, user_id__in=gone).delete()


def sweep() -> int:
    """Sync every Slack-bound session active in the last `ACTIVE_DAYS`: one Slack
    question per conversation (cached), the most recently active conversations first,
    at most `SWEEP_MAX_CHANNELS` of them. Returns the sessions synced."""
    since = timezone.now() - dt.timedelta(days=ACTIVE_DAYS)
    sessions = (Session.objects.filter(metadata__has_key=_SLACK_THREAD_KEY, updated_at__gte=since)
                .order_by("-updated_at").only("pk", "metadata", "workspace_id", "created_by_id"))
    installs = {i.team_id: i for i in SlackInstallation.objects.all()}
    channels: set[tuple[str, str]] = set()
    synced = 0
    for session in sessions.iterator():
        meta = session.metadata or {}
        team, channel = str(meta.get("slack_team") or ""), str(meta.get("slack_channel") or "")
        installation = installs.get(team)
        if installation is None or not channel:
            continue
        if (team, channel) not in channels:
            if len(channels) >= SWEEP_MAX_CHANNELS:
                continue
            channels.add((team, channel))
        try:
            sync(session, installation)
            synced += 1
        except Exception:  # noqa: BLE001 — one session must not stop the sweep
            logger.exception("slack audience: could not sync session %s", session.pk)
    return synced
