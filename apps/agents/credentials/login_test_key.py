"""An agent login's TEST vault key: the one exception to "only people hold a vault key".

An agent's own canopy login (`Agent.user`) is not a person
(`vault_members.not_a_person`): it is never a vault member and holds no vault key. A headless test harness signed in as an agent still needs to take part in that
agent's own SENSITIVE conversations, which are sealed to their people's vault keys
(`apps/canopy_sessions/conversation_keys.py`). So, per agent and off by default:

* **Who turns it on.** The agent's own login, for itself (`PUT /api/vault/test-key`,
  `set_own`, with its own session, PAT or OAuth token: never a delegated, caller,
  act-as, contact or runner token), or an admin of the agent from a browser behind a
  passkey step-up (`PUT /api/agents/{slug}/test-vault-key`). Both are audited as a
  setting. Anyone who may turn it on may turn it off, from anywhere; an admin's off is
  the kill switch.
* **Why no passkey for the login itself.** The key receives only copies of conversations
  the agent is already IN, wrapped to it by a participant's browser, and the conversation's
  people list marks it "agent test key": no reader is added that the conversation's people
  did not add. Its connections are for provider logins its harness already holds. What
  keeps an agent login from a person's powers is elsewhere and unchanged (below and
  `passkeys.stepup.require_a_person`).
* **What it allows.** While it is on, that login may make a personal vault key, by the
  passphrase path only (a `2skd` seal, optionally a recovery seal; never a passkey seal)
  and without a step-up, since a login has no passkey (`check_keyset_write`). A second key
  is a `replace`: the old key's conversation copies, page permissions and connections are
  dropped first (`drop_held`), so the new key starts with nothing.
* **What the key may receive.** Only copies of conversation keys for sensitive
  conversations the login is itself IN (its creator or a participant,
  `conversation_keys.add_wrap`). Never a vault: it is not a vault member by any
  source, and every wrap, sealed value, endorsement and keeper grant refuses a login
  that is not a person (`vault_engine`, `zk_endorse`), as do a passkey step-up and
  passkey registration (`passkeys.stepup.require_a_person`), so it can approve no
  device key and register no passkey.
* **The one other act it allows: confirming its own page permission.** An act-as-you
  permission on a host site's page (`tokens.HostGrant`) is usable only once its person
  confirmed it with their passkey, so a headless test signed in to a site AS the agent
  could never get past the panel's "Confirm with passkey". While the test key is on, that
  login may confirm its OWN waiting grant without a passkey
  (`POST /api/me/host-grants/{id}/confirm-as-agent`, `may_confirm_own_host_grant`), with
  its own canopy credential: a PAT or OAuth token, or its own browser session; never a
  delegated (panel) token, which the host site holds, a caller, act-as, contact or runner
  token, or a request carrying a host grant id. The grant must be the login's own (user =
  the login, no contact), unexpired, still unconfirmed, and for the login's own agent, for
  no agent in particular, or for a sibling agent: another agent in the same workspace
  whose own test key is also on (the login lends only its own identity to that page,
  `_may_lend_to`). It lasts the usual window, 30 minutes at most (`host_grants.confirm`).
  It confirms no person's grant and acts as no person: the permission is to act as the
  agent's own login on that page.
* **The other: its own connection, for its own screens.** While the test key is on, that
  login may connect a provider as itself (a `user`-owned `Connection`, sealed to its
  test key by `connections.services.owner_keyset`, the same connect as a person's) and
  share it without a passkey (`PUT /api/connections/{id}/share`,
  `may_share_own_connection`), with its own canopy credential, only when the statement
  lends it to nobody: no agent, no computer, no endorser. At most it names canopy-web
  (the "canopy-web can use this" switch), which is what a screen's `as: viewer` call
  needs (`connections.call_service`). So a headless test signed in as an agent uses a
  browser extension's or app's screens with its own provider login, exactly as a person
  does. Anything broader needs a person's passkey, which the login has none of.
* **It never** registers a passkey or steps up: the login has none, and both refuse it
  (`passkeys.stepup.require_a_person`).
* **Turning it off** deletes the key and every conversation-key copy wrapped to it,
  stops every unexpired host grant the login holds (`host_grants.revoke`) and
  disconnects every connection the login owns (registered by `connections`, `on_disable`), so
  nothing lingers (`disable`, through `drop_held`). A login cannot confirm a grant any other way, so every confirmed
  grant it holds came through the test key.

Docs: `docs/architecture/vault.md` § An agent login's test vault key;
`docs/architecture/access-confinement-and-act-as.md` § Acting as the person on a host site.
"""
from __future__ import annotations

from collections.abc import Callable

from django.db import transaction

from apps.canopy_sessions.models import SessionKeyWrap
from apps.common import ecdh_es
from apps.passkeys.models import UserVaultKeyset
from apps.tokens import kinds

from ..models import Agent, AgentVaultEndorsement
from . import zk
from .vault_members import not_a_person

#: The flag is per agent; this is what a person is told when they meet it.
LABEL = "agent test key"

#: The credentials that are the login itself, for `may_confirm_own_host_grant`. Not a
#: delegated token (the host site holds the panel's), a caller or act-as token (a turn
#: speaking for a person), a contact's, or a runner's.
OWN_CREDENTIALS = frozenset({kinds.SESSION, kinds.PAT, kinds.OAUTH})


class NotAPerson(Exception):
    """A refused act by (or for) a login that is not a person. `code` is the `error`."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code = status, code


def _uid(user_or_id):
    return getattr(user_or_id, "pk", user_or_id)


def is_person(user_or_id) -> bool:
    uid = _uid(user_or_id)
    return uid is not None and not not_a_person([uid])


def agent_of_login(user_or_id) -> Agent | None:
    uid = _uid(user_or_id)
    if uid is None:
        return None
    return Agent.objects.filter(user_id=uid).first()


def logins_with_test_key(user_ids) -> set[int]:
    """Of `user_ids`, the agent logins whose agent has its test vault key turned ON.
    One query, for a list (`ParticipantOut.test_key`)."""
    ids = {u for u in user_ids if u is not None}
    if not ids:
        return set()
    return set(Agent.objects.filter(user_id__in=ids, test_vault_key=True).values_list("user_id", flat=True))


def is_test_key_login(user_or_id) -> bool:
    uid = _uid(user_or_id)
    return uid is not None and uid in logins_with_test_key([uid])


def may_hold_vault_key(user_or_id) -> bool:
    """Whether this login's vault key is one canopy-web uses at all: a person's, or an
    agent login's while its agent's test vault key is on."""
    uid = _uid(user_or_id)
    if uid is None:
        return False
    return is_person(uid) or is_test_key_login(uid)


def check_keyset_write(user, *, seals: list, exists: bool, replace: bool = False) -> None:
    """Whether `user` may write their own vault keyset (`zk.put_keyset`). A person:
    always. An agent login: only while its agent's test vault key is on, and only
    passphrase (+ recovery) seals; a second write must be a `replace`, which makes a
    new key and drops what the old one held (`drop_held`, from `zk.put_keyset`)."""
    if is_person(user):
        return
    if not is_test_key_login(user):
        raise NotAPerson(403, "not_a_person",
                         "an agent's login holds no vault key until its test vault key is on "
                         "(PUT /api/vault/test-key as the login, or agent Settings → Test vault key)")
    if any(isinstance(s, dict) and s.get("alg") == "prf" for s in seals):
        raise NotAPerson(422, "test_key_passphrase_only",
                         "an agent's test vault key is sealed with a passphrase only, never a passkey")
    if exists and not replace:
        raise NotAPerson(409, "test_key_exists",
                         "this agent login already has a test vault key; send replace: true to make a "
                         "new one (the old key's conversation copies, page permissions and connections "
                         "are dropped)")


def state(agent: Agent) -> dict:
    """What the agent's Settings page shows: the switch, and the key if one was made."""
    ks = UserVaultKeyset.objects.filter(user_id=agent.user_id).first() if agent.user_id else None
    return {
        "enabled": agent.test_vault_key,
        "login_email": agent.user.email if agent.user_id else "",
        "has_key": ks is not None,
        "key_jkt": ecdh_es.jkt(ks.public_jwk) if ks is not None else "",
        "key_made_at": ks.created_at if ks is not None else None,
        "conversations": (SessionKeyWrap.objects.filter(user_id=agent.user_id).count()
                          if agent.user_id else 0),
    }


def enable(agent: Agent) -> None:
    if agent.user_id is None:
        raise NotAPerson(409, "no_agent_login", f"{agent.slug} has no canopy login of its own to hold a key")
    agent.test_vault_key = True
    agent.save(update_fields=["test_vault_key", "updated_at"])


def disable(agent: Agent) -> int:
    """Turn it off: the login's vault key and every conversation-key copy wrapped to it
    are deleted, every unexpired host grant the login holds (confirmed or waiting) is
    stopped, and every connection it owns is disconnected (revoked at the provider by
    the sweep when canopy-web holds it), so nothing it was given outlives the switch.
    Returns the copies deleted. Also clears any member wrap or endorsement a key of this
    login could have reached, though none is ever written."""
    n = 0
    with transaction.atomic():
        agent.test_vault_key = False
        agent.save(update_fields=["test_vault_key", "updated_at"])
        if agent.user_id is not None:
            n = drop_held(agent.user)
            UserVaultKeyset.objects.filter(user_id=agent.user_id).delete()
    return n


def drop_held(login) -> int:
    """Everything a test key's login was given, gone: every conversation-key copy wrapped
    to its key, any member wrap or endorsement (none is ever written), every unexpired host
    grant it holds (confirmed or waiting), and every connection it owns (`on_disable`).
    What turning the key off does, and what making a new key does to the old one, so a
    new key starts with nothing. Returns the copies deleted. Run inside a transaction."""
    from apps.host_grants import host_grants  # noqa: PLC0415 — host_grants imports agents
    from apps.tokens.models import HostGrant  # noqa: PLC0415 — beside the host_grants import

    n = SessionKeyWrap.objects.filter(user_id=login.pk).delete()[0]
    zk.forget_member_wraps(login)
    AgentVaultEndorsement.objects.filter(endorser_id=login.pk).delete()
    for grant in host_grants.unexpired(HostGrant.objects.filter(user_id=login.pk)).order_by("pk"):
        host_grants.revoke(grant)
    for drop in _ON_DISABLE:
        drop(login)
    return n


#: What else a login holds while its test key is on, each dropped by `disable` in its
#: transaction: an app above this layer registers it (`on_disable`) from its `ready()`,
#: as `connections` does to disconnect the login's own connections.
_ON_DISABLE: list[Callable] = []


def on_disable(drop: Callable) -> None:
    """Register `drop(login)`, run by `disable` with the agent's login."""
    if drop not in _ON_DISABLE:
        _ON_DISABLE.append(drop)


def own_agent(request) -> Agent:
    """The agent whose own login `request` is, with its own credential (a session, PAT
    or OAuth token: never a delegated panel token, a caller's, act-as, a contact's or a
    runner's, nor a request carrying a host grant). `NotAPerson` otherwise."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise NotAPerson(401, "not_signed_in", "sign in as the agent's own canopy login")
    if is_person(user):
        raise NotAPerson(403, "a_person", "a person holds a vault key of their own, not an agent test key")
    agent = agent_of_login(user)
    if agent is None:
        raise NotAPerson(403, "not_an_agent_login", "only an agent's own canopy login may use this route")
    kind = kinds.of_request(request)
    if kind is None or kind.name not in OWN_CREDENTIALS or getattr(request, "host_grant_id", None):
        raise NotAPerson(403, "not_own_credential",
                         "use the agent's own canopy login (its token or its own browser session)")
    return agent


def set_own(request, enabled: bool) -> Agent:
    """The agent's own login turns its own test vault key on or off (`own_agent`). No
    passkey: the key receives nothing its agent isn't already given (see the module
    docstring), and turning it off only takes away."""
    agent = own_agent(request)
    if enabled:
        enable(agent)
    else:
        disable(agent)
    return agent


def may_share_own_connection(request, conn, statement) -> None:
    """Raise `NotAPerson` unless `request` is an agent's own login, with its own
    credential, whose agent's test vault key is on, sharing a connection it owns itself
    with a statement that lends it to nobody (no agent, no computer, no endorser; it may
    name canopy-web). What a person does with a passkey, narrowed to the one share a
    test needs: the login's own account, for its own screens."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise NotAPerson(401, "not_signed_in", "sign in as the agent's own canopy login")
    if is_person(user):
        raise NotAPerson(403, "a_person", "a person shares a connection with their passkey")
    agent = agent_of_login(user)
    if agent is None or not agent.test_vault_key:
        raise NotAPerson(403, "not_a_person",
                         "sharing a connection is for a person; an agent's login may share only its own, "
                         "while an admin of the agent has its agent test key on (agent Settings → Security "
                         "→ Test vault key)")
    kind = kinds.of_request(request)
    if kind is None or kind.name not in OWN_CREDENTIALS or getattr(request, "host_grant_id", None):
        raise NotAPerson(403, "not_own_credential",
                         "share with the agent's own canopy login (its token or its own browser session)")
    if conn.user_id != user.pk:
        raise NotAPerson(404, "not_found", "no such connection")
    st = statement if isinstance(statement, dict) else {}
    if st.get("agents") or st.get("my_computers") or st.get("endorsers"):
        raise NotAPerson(403, "share_too_broad",
                         "an agent's test login shares its own connection with nobody: no agent, no "
                         "computer, no endorser; it may only turn on \"canopy-web can use this\"")


def _may_lend_to(agent, page_agent) -> bool:
    """May `agent`'s own login confirm its OWN permission on `page_agent`'s page? Only when
    both agents have their test key on and share a workspace: the login lends only its own
    identity to a sibling agent's page under test, never anyone else's."""
    return bool(page_agent.test_vault_key) and page_agent.workspace_id == agent.workspace_id


def may_confirm_own_host_grant(request, grant) -> None:
    """Raise `NotAPerson` unless `request` is an agent's own login, with its own
    credential, whose agent's test vault key is on, confirming its own waiting grant."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise NotAPerson(401, "not_signed_in", "sign in as the agent's own canopy login")
    if is_person(user):
        raise NotAPerson(403, "a_person",
                         "a person confirms an act-as-you permission with their passkey, not this route")
    agent = agent_of_login(user)
    if agent is None:
        raise NotAPerson(403, "not_an_agent_login", "only an agent's own canopy login may use this route")
    if not agent.test_vault_key:
        raise NotAPerson(403, "test_key_off",
                         f"{agent.slug}'s agent test key is off; an admin of the agent can turn it on "
                         "(agent Settings → Security → Test vault key)")
    kind = kinds.of_request(request)
    if kind is None or kind.name not in OWN_CREDENTIALS or getattr(request, "host_grant_id", None):
        raise NotAPerson(403, "not_own_credential",
                         "confirm with the agent's own canopy login (its token or its own browser "
                         "session), never a site's panel token")
    if grant.user_id != user.pk or grant.contact_id is not None:
        raise NotAPerson(404, "not_found", "no such permission")
    if grant.agent_id is not None and grant.agent_id != agent.pk and not _may_lend_to(agent, grant.agent):
        raise NotAPerson(403, "another_agents_page",
                         f"this page's permission is for another agent ({grant.agent.slug}); the login "
                         "confirms it only when that agent also has its agent test key on and is in "
                         f"{agent.slug}'s own workspace")
    if grant.confirmed_at is not None:
        raise NotAPerson(409, "already_confirmed", "this permission is already confirmed")
