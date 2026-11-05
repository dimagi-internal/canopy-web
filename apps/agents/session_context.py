"""Session Context: the durable facts of a conversation, whoever is asking.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 3.1. One half of what
`turn_context.resolve_turn_context` reads, and a frozen read-model over rows that already exist: no
table, no write, nothing about the asker.

The turn is an input because some facts only a turn holds: a sessionless turn dispatched from a
sensitive parent is sensitive through its ancestors, and its conversation can be `canopy_session`
rather than `chat_session`. Each fact below asks the function that already answers it, the same way
every entry point asks it today.

The facts the rule always reads are read at build. The ones it reads only sometimes, or only a
screen wants, are read when first asked (`cached_property`), so asking the rule about a turn costs
the queries it always cost.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property

from apps.harness import clis, sensitivity

from . import interface as iface_mod
from .refusals import Code, Refusal
from .refusals import make as refusal_of

#: The one kind of session grant there is (§ 3.4): `interface.session_writers`.
SESSION_WRITER = "session_writer"

#: Who a send block stops (§ 3.3, stage 0): everyone, or only some people.
ANYONE, SOME_PEOPLE = "anyone", "some_people"

#: Stage 0's refusal codes. Every code, and its words, is in `refusals.Code`.
CONTENT_DELETED = Code.CONTENT_DELETED
CANNOT_WRITE = Code.CANNOT_WRITE
CLI_NOT_OFFERED = Code.CLI_NOT_OFFERED
NOT_IN_SENSITIVE = Code.NOT_IN_SENSITIVE


@dataclass(frozen=True)
class SessionGrant:
    """Being in this session gives `user_id` more than their own standing does."""

    kind: str
    user_id: int
    role: str
    #: Where it comes from, in words a person can check.
    source: str


@dataclass(frozen=True)
class SendBlock:
    """Why this session takes no message from anyone, or from some people. `applies` is the same
    check the send route makes, asked of one asker, and `refusal` is what that route says."""

    refusal: Refusal
    scope: str
    applies: Callable = field(compare=False, repr=False)

    @property
    def code(self) -> str:
        return self.refusal.code

    @property
    def why(self) -> str:
        return self.refusal.why


@dataclass(frozen=True)
class SessionContext:
    session_id: object | None = None
    #: The agent's slug, or None for a person's own repo or project session.
    agent: str | None = None
    workspace: str | None = None
    #: `turn.origin` for a turn, which is what turn mode reads. For a preview, where the conversation
    #: lives. Never `Session.origin`, which only says which side created the row.
    channel: str = ""
    runner_requested: object | None = None
    #: The host site this conversation is held on and the resource its page declared, if any.
    site: str = ""
    page: str = ""
    #: Once true it does not revert: requirements only grow.
    sensitive: bool = False
    #: The session a review chat is about, when this is one. `is_review` is the rule's question.
    is_review: bool = False
    review_of: object | None = None
    # What the read-when-asked facts below are read from. Not facts themselves.
    _session: object = field(default=None, compare=False, repr=False)
    _turn: object = field(default=None, compare=False, repr=False)
    _agent: object = field(default=None, compare=False, repr=False)

    @cached_property
    def runner_bound(self):
        """The runner backing this conversation now, by pk."""
        binding = getattr(self._session, "runner_binding", None) if self._session is not None else None
        return getattr(binding, "runner_id", None)

    @cached_property
    def sensitive_requirement(self) -> str:
        """The requirement that makes this conversation sensitive, "" when it is not."""
        from apps.harness import runner_requirements as rr  # noqa: PLC0415 — only a screen asks this

        if not self.sensitive:
            return ""
        reqs = (rr.requirements_of(self._turn) if self._turn is not None
                else rr.requirements_of_session(self._session))
        return rr.SENSITIVE_DATA if rr.SENSITIVE_DATA in reqs else rr.UNSATISFIABLE

    @cached_property
    def audience(self) -> dict:
        """Who can see this session (`canopy_sessions.audience`), {} with no session."""
        from apps.canopy_sessions import audience  # noqa: PLC0415 — audience imports the agents app

        return audience.pieces(self._session) if self._saved_session else {}

    @cached_property
    def participants(self) -> tuple:
        """`(user_id, role)` rows."""
        if not self._saved_session:
            return ()
        return tuple(self._session.participants.order_by("user_id").values_list("user_id", "role"))

    @cached_property
    def session_grants(self) -> tuple[SessionGrant, ...]:
        """§ 3.4. Exactly one kind today, and adding a kind is adding one builder here."""
        return _session_writer_grants(self._grant_session, self._agent)

    @cached_property
    def send_blocks(self) -> tuple[SendBlock, ...]:
        """§ 3.3, stage 0, in the order the send route asks them."""
        return _send_blocks(self._session) if self._saved_session else ()

    @property
    def _saved_session(self) -> bool:
        return self._session is not None and not self._session._state.adding

    @property
    def _grant_session(self):
        # A chat turn only: the rule has never read a turn's `canopy_session`, or `origin_ref`'s chat.
        if self._turn is not None:
            return self._turn.chat_session if self._turn.chat_session_id else None
        return self._session


def empty(*, channel: str = "") -> SessionContext:
    """For a question asked with no conversation: the roster, topology, licenses, `may_edit_agent`."""
    return SessionContext(channel=channel)


def build(session, turn=None, *, agent=None) -> SessionContext:
    """The facts of `session`, or of `turn`'s conversation when it is given one. A turn with no
    session gets `session_id=None` and the turn's own channel and site. `agent`, when the caller holds
    it, is who the session's grants are about; else the turn's or session's own."""
    if turn is not None:
        return _of_turn(session, turn, agent)
    if session is None:
        return empty()
    from apps.canopy_sessions import access as session_access  # noqa: PLC0415 — access imports the agents app

    agent = agent if agent is not None else session.agent
    return SessionContext(
        session_id=session.pk, agent=_slug(agent), workspace=session.workspace_id,
        channel=_where_it_lives(session),
        runner_requested=(session.metadata or {}).get("requested_runner_id"),
        site=iface_mod.session_site(session), page=iface_mod.session_resource(session),
        sensitive=sensitivity.any_sensitive(session),
        is_review=session_access.is_review_conversation(session),
        review_of=_reviewed_session(session),
        _session=session, _agent=agent)


def _of_turn(session, turn, agent) -> SessionContext:
    session = session if session is not None else sensitivity.conversation_of(turn)
    agent = agent if agent is not None else turn.acting_agent
    is_review = iface_mod.is_review_turn(turn)
    return SessionContext(
        session_id=getattr(session, "pk", None), agent=_slug(agent),
        workspace=agent.workspace_id if agent is not None else getattr(session, "workspace_id", None),
        channel=turn.origin, runner_requested=turn.pinned_runner_id,
        site=iface_mod._conversation_site(turn), page=iface_mod._declared_resource(turn),
        sensitive=sensitivity.is_sensitive(turn), is_review=is_review,
        review_of=_reviewed_session(session) if is_review else None,
        _session=session, _turn=turn, _agent=agent)


def _slug(agent) -> str | None:
    return agent.slug if agent is not None else None


def _reviewed_session(session):
    from apps.canopy_sessions.models import REVIEW_METADATA_KEY  # noqa: PLC0415 — models read this app

    return (getattr(session, "metadata", None) or {}).get(REVIEW_METADATA_KEY)


def _where_it_lives(session) -> str:
    """The channel of a conversation with no turn in hand: the origin of its first turn, as the
    session panel derives it. Empty for a session no turn has run in."""
    if session._state.adding:
        return ""
    return session.turns.order_by("created_at").values_list("origin", flat=True).first() or ""


def _session_writer_grants(session, agent) -> tuple[SessionGrant, ...]:
    if session is None or agent is None:
        return ()
    return tuple(SessionGrant(kind=SESSION_WRITER, user_id=user_id, role=role,
                              source=f"a {role} in a session the agent's owner or an admin started")
                 for user_id, role in iface_mod.session_writers(session, agent))


def _send_blocks(session) -> tuple[SendBlock, ...]:
    from apps.canopy_sessions import access as session_access  # noqa: PLC0415 — access imports the agents app

    agent = session.agent
    blocks = []
    if session_access.is_content_deleted(session):
        blocks.append(SendBlock(refusal_of(CONTENT_DELETED, agent=agent, session=session), ANYONE,
                                lambda asker: True))
    # Only a signed-in person: a contact's send goes through its own route, with its own ACL.
    blocks.append(SendBlock(refusal_of(CANNOT_WRITE, agent=agent, session=session), SOME_PEOPLE,
                            lambda asker: asker.user is not None
                            and not session_access.can_write(asker.user, session)))
    if (refused := clis.conversation_refusal_of(session)) is not None:
        blocks.append(SendBlock(refused, ANYONE, lambda asker: True))
    if sensitivity.is_sensitive(session):
        blocks.append(SendBlock(
            refusal_of(NOT_IN_SENSITIVE, agent=agent, session=session), SOME_PEOPLE,
            lambda asker: not sensitivity.reads_content(session, chat_surface=asker.chat_surface,
                                                        user=asker.user, contact=asker.contact)))
    return tuple(blocks)
