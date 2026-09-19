"""A Slack message becomes a turn on a chat session.

The shape is the email path's (``harness.services.email_thread_session``): a
Slack thread is a conversation, so it maps to one ``canopy_sessions.Session``
and every message in it is a ``send_message`` on that session. That buys the
whole existing machinery for free — runner routing (``origin="slack"`` is
already a routable source), actor rules (``enqueued_by`` is the linked user),
the session UI, interjection — with no Slack-specific execution path.

What this module decides, and nothing else:

* **who** — a workspace member (linked, or matched by Slack profile email),
  who acts as their own account; or anyone else, recorded as a CONTACT and
  answered as one, like an email sender. Being in the Slack workspace grants
  nothing in canopy either way.
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
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core import signing

from apps.agents.models import Agent
from apps.canopy_sessions import services as session_services
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.canopy_sessions.participants import ensure_participant
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.workspaces import services as wsvc

from . import client
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
    #: A plain reply (no mention) inside a thread canopy is already in.
    follow: bool = False

    @property
    def anchor(self) -> str:
        """The thread this message belongs to — and where the answer goes."""
        if self.thread_ts:
            return self.thread_ts
        return DM_ANCHOR if self.is_dm else self.ts

    @property
    def reply_thread_ts(self) -> str:
        return "" if self.anchor == DM_ANCHOR else self.anchor


SENT, NOT_INSTALLED, BLOCKED, NO_AGENT, EMPTY = (
    "sent", "not_installed", "blocked", "no_agent", "empty")
# A reply to a question the agent is blocked on, and the ways that can go.
ANSWERED, NOT_AN_ANSWER, ANSWER_UNDELIVERABLE = "answered", "not_an_answer", "answer_undeliverable"
STALE = "stale"
#: Outcomes that are the system working, not refusing — nothing to log.
OK_STATUSES = {SENT, ANSWERED, STALE}


@dataclass
class Outcome:
    status: str
    message: str
    session: Session | None = None
    turn: Turn | None = None
    agent: Agent | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Principal:
    """Who is speaking: a workspace member, or a contact. Never both, never neither.

    The same split the web surface makes (`request.user` vs `request.contact`),
    for the same reason: a contact arriving through a user-shaped path would be
    an unspecified permission, not a smaller one.
    """

    user: object = None
    contact: object = None
    assurance: str = ""

    def initiator(self, team_id: str):
        via = f"slack:{team_id}"
        if self.contact is not None:
            return who.for_contact(self.contact, via=via)
        return who.for_user(self.user, via=via, assurance=self.assurance)


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


def slack_profile(installation: SlackInstallation, slack_user_id: str) -> dict:
    """`users.info` for this person, or {} if Slack cannot be reached."""
    try:
        return client.user_info(installation.bot_token, slack_user_id)
    except Exception:  # noqa: BLE001 — unreachable Slack degrades to "a contact we know less about"
        logger.exception("slack users.info failed")
        return {}


def _is_guest(info: dict) -> bool:
    return bool(info.get("is_restricted") or info.get("is_ultra_restricted"))


def auto_link(installation: SlackInstallation, slack_user_id: str, info: dict):
    """Link a Slack user to the canopy user with the same email, if exactly one.

    The manual link page proves exactly this — Slack email equals canopy email —
    plus a click. The click added friction and no assurance, so it is now the
    fallback for someone whose two emails differ. Requiring it first is what
    made the first live mention on labs (2026-09-19) look like silence.

    Not for a guest, a bot or a deactivated account: a guest is someone the
    organisation invited in, and is answered as a CONTACT whatever their email
    says. Not on an ambiguous match either — two canopy accounts sharing an
    address is not something to resolve by picking one.

    Grants nothing: membership is still checked on every message.
    """
    if not info or info.get("is_bot") or info.get("deleted") or _is_guest(info):
        return None
    email = str((info.get("profile") or {}).get("email") or "").strip()
    if not email:
        return None
    matches = list(get_user_model().objects.filter(email__iexact=email, is_active=True)[:2])
    if len(matches) != 1:
        return None
    link, _ = SlackUserLink.objects.get_or_create(
        installation=installation, slack_user_id=slack_user_id, defaults={"user": matches[0]},
    )
    return link.user


def resolve_principal(installation: SlackInstallation, slack_user_id: str) -> tuple[Principal | None, Outcome | None]:
    """A member if we can establish one, otherwise a contact. Only a blocked
    contact is turned away.

    Everyone who can post where an agent is invited gets an answer, the way
    anyone who can send an email gets one. What differs is WHAT they are to
    canopy: a member acts as their own account (their session, their routing
    rules); anyone else — a Slack guest, a colleague with no canopy membership —
    is recorded as a contact, whose conversation no member can open and who is
    granted nothing.
    """
    from apps.contacts import services as contacts

    link = (SlackUserLink.objects.select_related("user")
            .filter(installation=installation, slack_user_id=slack_user_id).first())
    info: dict | None = None
    user, assurance = (link.user, who.SLACK_LINKED) if link is not None else (None, "")
    if user is None:
        info = slack_profile(installation, slack_user_id)
        user, assurance = auto_link(installation, slack_user_id, info), who.SLACK_EMAIL
    if user is not None and wsvc.is_member(user, installation.workspace_id):
        return Principal(user=user, assurance=assurance), None

    if info is None:
        info = slack_profile(installation, slack_user_id)
    profile = info.get("profile") or {}
    contact = contacts.record_slack_user(
        workspace=installation.workspace,
        team_id=installation.team_id,
        slack_user_id=slack_user_id,
        email=str(profile.get("email") or ""),
        display_name=str(profile.get("real_name") or profile.get("display_name") or info.get("name") or ""),
    )
    if contact is None or contact.is_blocked:
        return None, Outcome(BLOCKED, "You can't reach agents from this Slack.")
    return Principal(contact=contact), None


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


def has_thread_session(installation: SlackInstallation, inbound: Inbound) -> bool:
    """Whether canopy is already in this thread — the gate on a plain reply."""
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    return Session.objects.filter(workspace=installation.workspace,
                                  **{f"metadata__{SLACK_THREAD_KEY}": key}).exists()


def thread_session(*, agent: Agent, principal: Principal, key: str, inbound: Inbound,
                   title: str) -> tuple[Session, bool]:
    """The Session for this (agent, Slack thread) — found, or created once.

    Owned by whoever started the thread. A member owns it as `created_by`, so
    `visible_session_q` shows it to them and nobody else; a contact owns it as
    `contact`, so no member sees it at all. A MEMBER who joins someone else's
    thread becomes a participant — they can already read it in Slack. A contact
    who joins gains nothing in canopy: they see the thread in Slack, which is
    all a contact is ever given.
    """
    existing = (Session.objects.filter(agent=agent, **{f"metadata__{SLACK_THREAD_KEY}": key})
                .order_by("created_at").first())
    if existing is not None:
        if principal.user is not None and existing.created_by_id != principal.user.pk:
            ensure_participant(existing, principal.user, SessionParticipant.EDITOR)
        return existing, False
    metadata = {
        SLACK_THREAD_KEY: key,
        "slack_team": inbound.team_id,
        "slack_channel": inbound.channel_id,
        "slack_thread_ts": inbound.reply_thread_ts,
    }
    if principal.user is not None:
        return session_services.create_session(
            workspace=agent.workspace, created_by=principal.user, agent=agent,
            title=title[:200], metadata=metadata,
        ), True
    # No `created_by`, exactly like a widget contact's session (tokens.contact_api):
    # null is what keeps it out of every member's list.
    if not getattr(settings, "CHAT_STUB_EXECUTOR", True):
        metadata[session_services.TRANSCRIPT_SOURCED] = True
    return Session.objects.create(
        workspace=agent.workspace, agent=agent, contact=principal.contact,
        title=title[:200], metadata=metadata,
    ), True


def handle_message(inbound: Inbound) -> Outcome:
    installation = installation_for(inbound.team_id)
    if installation is None:
        return Outcome(NOT_INSTALLED, "This Slack workspace isn't connected to canopy.")
    principal, refusal = resolve_principal(installation, inbound.slack_user_id)
    if refusal is not None:
        return refusal
    text = strip_mentions(inbound.text, installation.bot_user_id)
    key = thread_key(inbound.team_id, inbound.channel_id, inbound.anchor)
    agent, prompt = resolve_agent(installation, text, key)
    if agent is None:
        return Outcome(NO_AGENT, agent_list(installation))
    if not prompt:
        return Outcome(EMPTY, f"What would you like `{agent.slug}` to do?", agent=agent)
    session, created = thread_session(agent=agent, principal=principal, key=key, inbound=inbound,
                                      title=prompt)
    if not created:
        answered = _answer_if_waiting(session, agent, prompt)
        if answered is not None:
            return answered
    _message, turn = session_services.send_message(
        session=session,
        text=prompt,
        # A contact has no account; enqueue_turn ignores an anonymous user, so
        # the turn carries no `enqueued_by` and the initiator names the contact.
        user=principal.user or AnonymousUser(),
        # Slack redelivers an event it thinks we missed; the same message must
        # collapse onto the same turn rather than asking the agent twice.
        client_id=f"slack:{inbound.channel_id}:{inbound.ts}",
        origin=Turn.ORIGIN_SLACK,
        initiator=principal.initiator(inbound.team_id),
    )
    # The thread's status card: posted on the first message, updated on every
    # later one (a new turn is queued, so "Done" must stop saying so).
    from .status import refresh

    card_state = refresh(session)
    if principal.user is None:
        # A contact cannot open canopy, so a link would be a dead end.
        note = f"Sent to `{agent.slug}` — the reply will come back here."
    else:
        note = f"Sent to `{agent.slug}` — the reply will come back here. Also on canopy: {session_url(session)}"
    return Outcome(SENT, note, session=session, turn=turn, agent=agent,
                   extra={"new_session": created, "card": card_state is not None})


def _answer_if_waiting(session: Session, agent: Agent, reply: str) -> Outcome | None:
    """If the agent is blocked on a question, treat the reply as the answer.

    Returns None when there is no answerable question, and the message is then
    an ordinary send. While there IS one, a reply that is not an answer is NOT
    sent to the agent — the dialog is drawn where the prompt would be, so the
    message would bounce, and canopy-web refuses the same send for the same
    reason. Saying how to answer beats a message that silently goes nowhere.
    """
    from apps.canopy_sessions.serializers import pending_menu

    from . import menus

    menu = pending_menu(session)
    if not menus.answerable(menu):
        return None
    choice = menus.parse_answer(reply, menu)
    if choice is None:
        return Outcome(NOT_AN_ANSWER, (
            f"`{agent.slug}` is waiting on its question above — reply with the option number, "
            "or `cancel`."), session=session, agent=agent)
    if choice == menus.CANCEL:
        result = session_services.answer_menu(session=session, option=None)
        said = "Dismissed the question."
    else:
        result = session_services.answer_menu(session=session, option=choice[0][0], selections=choice)
        said = f"Answered: {menus.describe(choice, menu)}"
    if result != "sent":
        return Outcome(ANSWER_UNDELIVERABLE, (
            "Couldn't deliver that answer — the runner holding this session is offline. "
            "It will need answering once it is back."), session=session, agent=agent)
    return Outcome(ANSWERED, said, session=session, agent=agent)


def answer_from_click(installation: SlackInstallation, *, slack_user_id: str, channel_id: str,
                      message_ts: str, action: dict, state: dict) -> Outcome:
    """A button press (or Submit) on a question post -> the answer, or why not.

    Everything the click names is re-checked against what is true NOW, because
    a question post can outlive its question by hours: the session must be this
    workspace's and this channel's Slack session, and the dialog it is blocked
    on must still be the one the buttons were drawn for (content key). A stale
    click answers nothing — pressing a number at whatever the agent shows now
    is precisely the failure the phone path spent an incident removing.
    """
    import json

    from apps.canopy_sessions.serializers import pending_menu

    from . import menus
    from .models import SlackMenuPost

    try:
        value = json.loads(action.get("value") or "{}")
    except ValueError:
        value = {}
    session = (Session.objects.select_related("agent")
               .filter(pk=value.get("s"), workspace=installation.workspace,
                       metadata__slack_team=installation.team_id,
                       metadata__slack_channel=channel_id)
               .first()) if value.get("s") else None
    post = SlackMenuPost.objects.filter(session=session, slack_ts=message_ts).first() if session else None
    menu = pending_menu(session) if session else None
    if session is None or not menus.answerable(menu) or menus.content_key(menu) != value.get("k"):
        if post is not None:
            _resolve_post(installation, post, ":heavy_minus_sign: This question is no longer open.")
        return Outcome(STALE, "That question is no longer open.")

    _principal, refusal = resolve_principal(installation, slack_user_id)
    if refusal is not None:
        return refusal

    action_id = str(action.get("action_id") or "")
    if action_id == menus.DISMISS:
        choice = menus.CANCEL
    elif action_id.startswith(menus.PICK):
        choice = value.get("sel")
    else:
        choice = menus.selections_from_state(state, menu)
    if choice != menus.CANCEL and not menus.valid(choice, menu):
        return Outcome(NOT_AN_ANSWER, "Pick an option for each question, then press Submit.",
                       session=session, agent=session.agent)

    if choice == menus.CANCEL:
        result = session_services.answer_menu(session=session, option=None)
        said = f":heavy_minus_sign: Dismissed by <@{slack_user_id}>"
    else:
        result = session_services.answer_menu(session=session, option=choice[0][0], selections=choice)
        said = f":white_check_mark: Answered by <@{slack_user_id}>: {menus.describe(choice, menu)}"
    if result != "sent":
        return Outcome(ANSWER_UNDELIVERABLE, (
            "Couldn't deliver that answer — the runner holding this session is offline."),
            session=session, agent=session.agent)
    if post is not None:
        _resolve_post(installation, post, said)
    return Outcome(ANSWERED, said, session=session, agent=session.agent)


def _resolve_post(installation: SlackInstallation, post, outcome: str) -> None:
    """Rewrite a question post without its buttons, so it cannot be pressed again."""
    from . import menus

    post.resolved = True
    post.save(update_fields=["resolved"])
    try:
        client.update_message(installation.bot_token, channel=post.channel_id, ts=post.slack_ts,
                              text=outcome, blocks=menus.resolved_blocks(post.question, outcome))
    except Exception:  # noqa: BLE001 — the answer landed; a stale-looking post is cosmetic
        logger.exception("could not rewrite a Slack question post")
