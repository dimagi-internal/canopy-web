"""A Slack message becomes a turn on a chat session. Nothing here talks HTTP.

The shape is the email path's (``harness.services.email_thread_session``): a
Slack thread is a conversation, so it maps to one ``canopy_sessions.Session``
and every message in it is a ``send_message`` on that session. That buys the
whole existing machinery for free — runner routing (``origin="slack"`` is
already a routable source), actor rules (``enqueued_by`` is the linked user),
the session UI, interjection — with no Slack-specific execution path.

What this module decides, and nothing else:

* **who** — the linked canopy user, who must be a member of the installation's
  workspace. An unlinked or non-member Slack user gets words back and nothing
  queued: being in the Slack workspace grants nothing in canopy.
* **which agent** — named first word, else the thread's existing agent, else the
  only enabled one. Only agents their owner turned on for Slack are candidates.
* **which session** — keyed on (team, channel, thread), per agent.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing

from apps.harness import initiator as who
from apps.agents.models import Agent
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.canopy_sessions.participants import ensure_participant
from apps.harness.models import Turn
from apps.workspaces import services as wsvc

from .models import SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

SLACK_THREAD_KEY = "slack_thread"
LINK_SALT = "canopy.slack.link"
LINK_MAX_AGE = 30 * 60
# A top-level DM to the bot has no thread; the whole DM is one conversation.
DM_ANCHOR = "dm"

_MENTION = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


def _setting(name: str) -> str:
    value = (getattr(settings, name, "") or "").strip()
    # The CFN secret containers are born holding "PLACEHOLDER". Reading that as
    # configured would verify every request against a public string.
    return "" if value == "PLACEHOLDER" else value


def signing_secret() -> str:
    return _setting("SLACK_SIGNING_SECRET")


def client_id() -> str:
    return _setting("SLACK_CLIENT_ID")


def client_secret() -> str:
    return _setting("SLACK_CLIENT_SECRET")


def is_configured() -> bool:
    return bool(signing_secret() and client_id() and client_secret())


def public_url(path: str) -> str:
    return settings.CANOPY_PUBLIC_BASE_URL.rstrip("/") + path


def session_url(session: Session) -> str:
    return public_url(f"/w/{session.workspace_id}/chat/{session.id}")


def link_url(team_id: str, slack_user_id: str) -> str:
    token = signing.dumps({"t": team_id, "u": slack_user_id}, salt=LINK_SALT)
    return public_url("/auth/slack/link/?" + urlencode({"token": token}))


def read_link_token(token: str) -> tuple[str, str]:
    """(team_id, slack_user_id). Raises ``signing.BadSignature`` (incl. expiry)."""
    data = signing.loads(token, salt=LINK_SALT, max_age=LINK_MAX_AGE)
    return str(data["t"]), str(data["u"])


def thread_key(team_id: str, channel_id: str, anchor: str) -> str:
    return f"slack:{team_id}:{channel_id}:{anchor}"


@dataclass
class Inbound:
    """One human message, from an event or a slash command, already verified."""

    team_id: str
    channel_id: str
    slack_user_id: str
    text: str
    ts: str
    thread_ts: str = ""
    is_dm: bool = False

    @property
    def anchor(self) -> str:
        """The thread this message belongs to — and where the answer goes."""
        if self.thread_ts:
            return self.thread_ts
        return DM_ANCHOR if self.is_dm else self.ts

    @property
    def reply_thread_ts(self) -> str:
        return "" if self.anchor == DM_ANCHOR else self.anchor


SENT, NOT_INSTALLED, UNLINKED, NOT_MEMBER, NO_AGENT, EMPTY = (
    "sent", "not_installed", "unlinked", "not_member", "no_agent", "empty")


@dataclass
class Outcome:
    status: str
    message: str
    session: Session | None = None
    turn: Turn | None = None
    agent: Agent | None = None
    extra: dict = field(default_factory=dict)


def installation_for(team_id: str) -> SlackInstallation | None:
    return SlackInstallation.objects.select_related("workspace").filter(team_id=team_id).first()


def enabled_agents(installation: SlackInstallation):
    return Agent.objects.filter(workspace=installation.workspace, slack_enabled=True).order_by("slug")


def strip_mentions(text: str, bot_user_id: str) -> str:
    """Drop the bot's own mention; keep anyone else's, rendered readably."""
    def repl(m: re.Match) -> str:
        return "" if m.group(1) == bot_user_id else f"@{m.group(1)}"
    return re.sub(r"\s+", " ", _MENTION.sub(repl, text or "")).strip()


def agent_list(installation: SlackInstallation) -> str:
    slugs = [a.slug for a in enabled_agents(installation)]
    if not slugs:
        return "No agents in this workspace are turned on for Slack yet."
    return "Agents you can talk to here: " + ", ".join(f"`{s}`" for s in slugs) + \
        ". Start your message with one, e.g. `@canopy " + slugs[0] + " summarise this thread`."


def authorize(installation: SlackInstallation, slack_user_id: str) -> tuple[object | None, Outcome | None]:
    """The linked canopy user, or the Outcome that says why there isn't one."""
    link = (SlackUserLink.objects.select_related("user")
            .filter(installation=installation, slack_user_id=slack_user_id).first())
    if link is None:
        return None, Outcome(UNLINKED, (
            "I don't know who you are in canopy yet. Link your account (sign in with "
            f"the same email as your Slack account), then send that again: "
            f"{link_url(installation.team_id, slack_user_id)}"))
    if not wsvc.is_member(link.user, installation.workspace_id):
        return None, Outcome(NOT_MEMBER, (
            f"Your canopy account isn't a member of the `{installation.workspace_id}` "
            "workspace, which is the one this Slack is connected to. Ask an owner to invite you."))
    return link.user, None


def resolve_agent(installation: SlackInstallation, text: str, key: str) -> tuple[Agent | None, str]:
    """(agent, the prompt with the agent's name removed)."""
    agents = {a.slug.lower(): a for a in enabled_agents(installation)}
    first, _, rest = text.partition(" ")
    named = agents.get(first.lower().rstrip(":,"))
    if named is not None:
        return named, rest.strip()
    existing = (Session.objects.filter(workspace=installation.workspace,
                                       agent__slack_enabled=True,
                                       **{f"metadata__{SLACK_THREAD_KEY}": key})
                .select_related("agent").order_by("-created_at").first())
    if existing is not None:
        return existing.agent, text
    if len(agents) == 1:
        return next(iter(agents.values())), text
    return None, text


def thread_session(*, agent: Agent, user, key: str, inbound: Inbound, title: str) -> Session:
    """The Session for this (agent, Slack thread) — found, or created once.

    Private to whoever started it (``created_by`` + ``origin=web``, so
    ``visible_session_q`` shows it to them and nobody else). Someone else who
    joins the same Slack thread is added as a participant: they can already read
    the thread in Slack, and the agent is answering there.
    """
    existing = (Session.objects.filter(agent=agent, **{f"metadata__{SLACK_THREAD_KEY}": key})
                .order_by("created_at").first())
    if existing is not None:
        if existing.created_by_id != user.pk:
            ensure_participant(existing, user, SessionParticipant.EDITOR)
        return existing
    return session_services.create_session(
        workspace=agent.workspace, created_by=user, agent=agent, title=title[:200],
        metadata={
            SLACK_THREAD_KEY: key,
            "slack_team": inbound.team_id,
            "slack_channel": inbound.channel_id,
            "slack_thread_ts": inbound.reply_thread_ts,
        },
    )


def handle_message(inbound: Inbound) -> Outcome:
    installation = installation_for(inbound.team_id)
    if installation is None:
        return Outcome(NOT_INSTALLED, "This Slack workspace isn't connected to canopy.")
    user, refusal = authorize(installation, inbound.slack_user_id)
    if refusal is not None:
        return refusal
    text = strip_mentions(inbound.text, installation.bot_user_id)
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    agent, prompt = resolve_agent(installation, text, key)
    if agent is None:
        return Outcome(NO_AGENT, agent_list(installation))
    if not prompt:
        return Outcome(EMPTY, f"What would you like `{agent.slug}` to do?", agent=agent)
    session = thread_session(agent=agent, user=user, key=key, inbound=inbound, title=prompt)
    _message, turn = session_services.send_message(
        session=session,
        text=prompt,
        user=user,
        # Slack redelivers an event it thinks we missed; the same message must
        # collapse onto the same turn rather than asking the agent twice.
        client_id=f"slack:{inbound.channel_id}:{inbound.ts}",
        origin=Turn.ORIGIN_SLACK,
        # The one Slack line the who-is-asking work touches, deliberately: the
        # linked canopy user who sent THIS message (not the thread's starter,
        # and not the channel history the bot reads in as context).
        initiator=who.for_user(user, via=f"slack:{inbound.team_id}",
                               assurance=who.SLACK_LINKED),
    )
    return Outcome(SENT, f"Sent to `{agent.slug}` — follow along: {session_url(session)}",
                   session=session, turn=turn, agent=agent)
