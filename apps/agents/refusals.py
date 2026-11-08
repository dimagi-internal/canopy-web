"""Why a turn is not run, and how to fix it: the closed set of refusal codes and the one place their words are written.

The owner's principle: if someone can reach a
session but the Turn Context would not let them take a turn, canopy says why and how to fix it. So every refusal is
`{code, why, how_to_fix, who_can_fix}`:

* `code` is one of `Code`, and nothing else: `Refusal` will not be made with any other.
* `why` is plain words, starting lower-case so it reads after "not run: ".
* `how_to_fix` is one concrete action, as a sentence.
* `who_can_fix` names the real people or roles who can do it.

Each code has exactly one builder in `_WORDS` (a test holds the two sets equal), so the words a refusal is given at
enqueue, in a preview, on a turn's note, in a Slack reply and in a route's 403 are the same words. None of them names
read mode or a host site by an old name, and none carries a prompt, a fact about a person or a secret: a builder is handed only
the agent, the session and the small facts below, never the turn's words.

A refusal is about "you" unless it is made `about` someone else: the notice that tells whoever added a person to a
session that they can read it but not take a turn uses the same words, about them.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum


class Code(StrEnum):
    """Every reason canopy refuses a turn, before it runs, because of who is asking. Stored on `Turn.resolved_context`
    and served by the context routes, so a value is never renamed."""

    #: The rule: nothing on the agent's access list names them.
    NOT_ON_ACCESS_LIST = "not_on_access_list"
    #: The rule: a line names their address, but only for a message that proves it, and this one did not.
    SENDER_NOT_PROVEN = "sender_not_proven"
    #: The rule: they hold something, but not what this conversation would run in.
    PROFILE_NOT_OPEN = "profile_not_open"
    #: The rule: a sensitive conversation runs only in its sensitive entry, and that is not open to them.
    SENSITIVE_NOT_OPEN = "sensitive_not_open"
    #: The mail channel, before the rule: the sender is blocked.
    SENDER_BLOCKED = "sender_blocked"
    #: Stage 0 (a preview, and the send paths): the session's content was deleted.
    CONTENT_DELETED = "content_deleted"
    #: Stage 0: they may read the session but not write in it.
    CANNOT_WRITE = "cannot_write"
    #: Stage 0: the session is held to an agent CLI its agent no longer runs.
    CLI_NOT_OFFERED = "cli_not_offered"
    #: Stage 0: a sensitive conversation takes messages only from its own people, at its chat surfaces.
    NOT_IN_SENSITIVE = "not_in_sensitive"
    #: The claim: the agent this turn was sent to was deleted while it waited.
    AGENT_GONE = "agent_gone"


#: How a refused turn's note begins (`Turn.result_note`). Slack's status line and `turn_context.was_refused` read it.
REFUSED_NOTE_PREFIX = "not run:"

#: What a turn refused at its claim adds to its refusal: it was let in when it was sent, and the facts changed while it
#: waited in the queue (`harness.claim`, spec § 3.3 "Two moments").
CHANGED_WHILE_QUEUED = "This changed after it was sent: it was let in then, and not by the time it would have run."


@dataclass(frozen=True)
class Refusal:
    """Why a turn is not run. Non-null on a Turn Context exactly when it is refused."""

    code: str
    why: str
    how_to_fix: str = ""
    who_can_fix: str = ""
    #: What it was built from, so it can be said `about` someone else. Never shown.
    _inputs: tuple = field(default=(), compare=False, repr=False)

    def __post_init__(self):
        if self.code not in _CODES:
            raise ValueError(f"{self.code!r} is not a refusal code: add it to refusals.Code, with its words")

    @property
    def message(self) -> str:
        """The refusal as one or two sentences: what a route's 403 says, and what a reader sees beside a turn."""
        why = f"{self.why[:1].upper()}{self.why[1:]}"
        return f"{why}. {self.how_to_fix}" if self.how_to_fix else f"{why}."

    @property
    def text(self) -> str:
        """"<why>. <how_to_fix>", to follow a colon: what a turn's note and a Slack reply say."""
        return f"{self.why}. {self.how_to_fix}".rstrip()

    @property
    def result_note(self) -> str:
        """`Turn.result_note` for a turn refused before it ran: "not run: <why>. <how_to_fix>"."""
        return f"{REFUSED_NOTE_PREFIX} {self.text}"

    @property
    def queued_text(self) -> str:
        """`text`, for a turn let in when it was sent and refused when it was claimed."""
        return f"{self.text} {CHANGED_WHILE_QUEUED}"

    @property
    def queued_result_note(self) -> str:
        """`result_note` for a turn refused at its claim: why, how to fix it, and that it changed while it waited."""
        return f"{REFUSED_NOTE_PREFIX} {self.queued_text}"

    def about(self, name: str) -> Refusal:
        """The same refusal, said about `name` rather than to "you"."""
        if not self._inputs:
            return self
        agent, session, facts = self._inputs
        return make(self.code, agent=agent, session=session, subject=name, **dict(facts))


_CODES = frozenset(Code)


@dataclass(frozen=True)
class _Say:
    """Who a refusal is said to: "you", or a person by name."""

    name: str = ""

    @property
    def you(self) -> str:
        return self.name or "you"

    @property
    def you_are(self) -> str:
        return f"{self.name} is" if self.name else "you are"

    @property
    def your(self) -> str:
        return f"{self.name}'s" if self.name else "your"


@dataclass(frozen=True)
class _In:
    agent: object
    session: object
    say: _Say
    facts: dict

    @property
    def slug(self) -> str:
        return getattr(self.agent, "slug", "") or "this agent"

    @property
    def admins(self) -> str:
        """The agent's owner and admins: who changes its access list and its settings."""
        agent = self.agent
        owner = agent.owner.email if agent is not None and agent.owner_id and agent.owner else "its owner"
        return f"{owner} or an admin of {self.slug}"

    @property
    def _session_owner_email(self) -> str:
        creator = getattr(self.session, "created_by", None) if self.session is not None else None
        return getattr(creator, "email", "") if creator is not None else ""

    @property
    def session_owner(self) -> str:
        """Who owns the session, as the subject of a sentence."""
        return self._session_owner_email or "the person who owns this session"

    @property
    def session_owner_role(self) -> str:
        """Who owns the session, as `who_can_fix` names them."""
        email = self._session_owner_email
        return f"{email}, who owns this session" if email else "the person who owns this session"

    @property
    def start_own(self) -> str:
        return f"start {self.say.your} own conversation with {self.slug}" if self.agent is not None \
            else f"start {self.say.your} own session"


# --- one builder per code ---------------------------------------------------------------------------
# Each returns (why, how_to_fix, who_can_fix).

def _not_on_access_list(i: _In):
    how = (f"Have {i.admins} add {i.say.you} to its access list as read only (to put questions to it), read and "
           "write (to use all of it) or editor (to also change it).")
    if i.facts.get("automated"):
        # A proven sender nobody listed is, far more often than a stranger, an automated one someone subscribed
        # on purpose: CloudWatch alarms went unanswered for nine days this way (canopy-web#1253).
        how += (f" If this is an automated sender (alarms, CI, a monitor), an admin of {i.slug} can instead bind "
                "its address to an inbound-mail trigger on the agent's Triggers tab.")
    return f"{i.say.you_are} not on {i.slug}'s access list", how, i.admins


def _sender_not_proven(i: _In):
    why = (f"{i.slug}'s access list takes this sender's address only from mail that proves who sent it, and this "
           "message did not prove it")
    how = ("Send from mail that is signed for the sender's own domain (DMARC, or DKIM signed by that domain), or "
           "sign in to canopy and write there.")
    return why, how, f"{i.say.you}, or whoever runs mail for the sender's domain"


def _profile_not_open(i: _In):
    from . import published_profiles# noqa: PLC0415 — interface reads the agents' models

    name = i.facts.get("name")
    if name == published_profiles.SESSION_REVIEW:
        why = f"a review chat needs read access to {i.slug}, and {i.say.your} place on its access list does not give it"
        how = f"Have {i.admins} give {i.say.you} the read only role on {i.slug}'s access list."
    elif name and published_profiles.is_read_mode(name):
        why = f"{i.say.your} place on {i.slug}'s access list does not open read mode"
        how = f"Have {i.admins} give {i.say.you} the read only role on {i.slug}'s access list."
    elif name and not i.facts.get("published"):
        why = f"{i.slug} has no host site called '{name}' for {i.say.your} place on its access list to open"
        how = f"Send without naming '{name}', so {i.slug} uses what {i.say.your} access opens."
    elif name:
        why = f"{i.say.your} place on {i.slug}'s access list does not open the host site '{name}'"
        how = f"Have {i.admins} give {i.say.you} a line on {i.slug}'s access list that opens '{name}'."
    else:
        why = (f"{i.say.your} place on {i.slug}'s access list opens neither read mode nor anything this page or site "
               "uses")
        how = f"Have {i.admins} give {i.say.you} the read only role on {i.slug}'s access list."
    return why, how, i.admins


def _sensitive_not_open(i: _In):
    entry = i.facts.get("entry")
    if entry is None:
        why = (f"this conversation holds a site's sensitive data, so it runs only in a host site that handles "
               f"sensitive data, and {i.slug} has none for it")
        how = f"Have {i.admins} set up a host site on {i.slug} that handles this site's sensitive data."
    else:
        why = (f"this conversation holds a site's sensitive data, which only the host site '{entry}' handles, "
               f"and '{entry}' does not list {i.say.you}")
        how = f"Have {i.admins} give {i.say.you} a line on {i.slug}'s access list that opens '{entry}'."
    return why, how, i.admins


def _sender_blocked(i: _In):
    # Never why they were blocked: that is an admin's note about a person, and it stays on the contact.
    workspace = getattr(i.agent, "workspace_id", "") or "the workspace"
    return ("the sender is blocked", f"An editor of {workspace} can unblock this sender on its contact page.",
            f"an editor or owner of {workspace}")


def _content_deleted(i: _In):
    # Said exactly as the send paths have always said it (`canopy_sessions.access.CONTENT_DELETED_REFUSAL`).
    return ("this session's content was permanently deleted, so it takes nothing more",
            "Start a new session to carry on.", i.say.you)


def _cannot_write(i: _In):
    how = f"Have {i.session_owner} make {i.say.you} an editor of it, or {i.start_own}."
    return f"{i.say.you} can read this session but not act in it", how, i.session_owner_role


def _cli_not_offered(i: _In):
    cli, name = i.facts.get("cli", "its agent CLI"), getattr(i.agent, "name", "") or "its agent"
    why = f"this conversation runs on {cli}, and {name} is no longer set up for {cli}"
    how = f"Start a new conversation with {name}, or have {i.admins} set {name} up for {cli} again."
    return why, how, f"{i.say.you}, or {i.admins}"


def _not_in_sensitive(i: _In):
    why = ("this conversation holds a host's sensitive data, so only the people in it can send into it, from "
           "canopy's chat or the site's panel")
    how = (f"Have {i.session_owner} add {i.say.you} to it and write from canopy's chat or the site's panel, or "
           f"{i.start_own}.")
    return why, how, i.session_owner_role


def _agent_gone(i: _In):
    # Asked only of a turn whose conversation lost its agent while it waited: there is no agent left to name.
    return ("the agent this was sent to was deleted before it ran",
            "Start a new conversation with an agent that is still here.", i.say.you)


_WORDS: dict[Code, Callable[[_In], tuple[str, str, str]]] = {
    Code.NOT_ON_ACCESS_LIST: _not_on_access_list,
    Code.SENDER_NOT_PROVEN: _sender_not_proven,
    Code.PROFILE_NOT_OPEN: _profile_not_open,
    Code.SENSITIVE_NOT_OPEN: _sensitive_not_open,
    Code.SENDER_BLOCKED: _sender_blocked,
    Code.CONTENT_DELETED: _content_deleted,
    Code.CANNOT_WRITE: _cannot_write,
    Code.CLI_NOT_OFFERED: _cli_not_offered,
    Code.NOT_IN_SENSITIVE: _not_in_sensitive,
    Code.AGENT_GONE: _agent_gone,
}


def make(code: str, *, agent=None, session=None, subject: str = "", **facts) -> Refusal:
    """The refusal for `code`, said to "you" or, with `subject`, about that person. `facts` are the few things a
    code's words name (which entry, which CLI), never anything a person wrote."""
    words = _WORDS[Code(code)]
    why, how, who = words(_In(agent=agent, session=session, say=_Say(subject), facts=facts))
    return Refusal(code=Code(code).value, why=why, how_to_fix=how, who_can_fix=who,
                   _inputs=(agent, session, tuple(sorted(facts.items()))))


def cannot_write_message(session) -> str:
    """What every send path says to someone who may read `session` but not write in it."""
    return make(Code.CANNOT_WRITE, agent=getattr(session, "agent", None), session=session).message
