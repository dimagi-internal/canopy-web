"""An agent login's TEST vault key: the one exception to "only people hold a vault key".

An agent's own canopy login (`Agent.user`) is not a person
(`vault_members.not_a_person`): it is never a vault member and holds no vault key. A headless test harness signed in as an agent still needs to take part in that
agent's own SENSITIVE conversations, which are sealed to their people's vault keys
(`apps/canopy_sessions/conversation_keys.py`). So, per agent and off by default:

* **Who turns it on.** An admin of the agent, from a browser, behind a fresh passkey
  step-up (`PUT /api/agents/{slug}/test-vault-key`, audited as a setting). A token
  cannot turn it on. Anyone who may turn it on may turn it off, from anywhere.
* **What it allows.** While it is on, that login may make ONE personal vault key, by
  the passphrase path only (a `2skd` seal, optionally a recovery seal; never a passkey
  seal) and without a step-up, since a login has no passkey (`check_keyset_write`).
  Replacing it is refused: turn the flag off and on again.
* **What the key may receive.** Only copies of conversation keys for sensitive
  conversations the login is itself IN (its creator or a participant,
  `conversation_keys.add_wrap`). Never a vault: it is not a vault member by any
  source, and every wrap, sealed value, endorsement and keeper grant refuses a login
  that is not a person (`vault_engine`, `zk_endorse`), as do a passkey step-up and
  passkey registration (`passkeys.stepup.require_a_person`), so it can approve no
  device key and register no passkey.
* **Turning it off** deletes the key and every conversation-key copy wrapped to it
  (`disable`), so nothing lingers.

Docs: `docs/architecture/vault.md` § An agent login's test vault key.
"""
from __future__ import annotations

from django.db import transaction

from apps.canopy_sessions.models import SessionKeyWrap
from apps.common import ecdh_es
from apps.passkeys.models import UserVaultKeyset

from ..models import Agent, AgentVaultEndorsement
from . import zk
from .vault_members import not_a_person

#: The flag is per agent; this is what a person is told when they meet it.
LABEL = "agent test key"


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


def check_keyset_write(user, *, seals: list, exists: bool) -> None:
    """Whether `user` may write their own vault keyset (`zk.put_keyset`). A person:
    always. An agent login: only while its agent's test vault key is on, a first key
    only, and only passphrase (+ recovery) seals."""
    if is_person(user):
        return
    if not is_test_key_login(user):
        raise NotAPerson(403, "not_a_person",
                         "an agent's login holds no vault key; an admin of the "
                         "agent can turn on a test vault key for it (agent Settings → Test vault key)")
    if any(isinstance(s, dict) and s.get("alg") == "prf" for s in seals):
        raise NotAPerson(422, "test_key_passphrase_only",
                         "an agent's test vault key is sealed with a passphrase only, never a passkey")
    if exists:
        raise NotAPerson(409, "test_key_exists",
                         "this agent login already has a test vault key; an admin turns the test "
                         "key off and on again to start over")


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
    are deleted, so nothing it was given outlives the switch. Returns the copies
    deleted. Also clears any member wrap or endorsement a key of this login could have
    reached, though none is ever written."""
    n = 0
    with transaction.atomic():
        agent.test_vault_key = False
        agent.save(update_fields=["test_vault_key", "updated_at"])
        if agent.user_id is not None:
            n = SessionKeyWrap.objects.filter(user_id=agent.user_id).delete()[0]
            zk.forget_member_wraps(agent.user)
            AgentVaultEndorsement.objects.filter(endorser_id=agent.user_id).delete()
            UserVaultKeyset.objects.filter(user_id=agent.user_id).delete()
    return n
