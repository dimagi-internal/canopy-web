from __future__ import annotations

from django.db import transaction

from apps.harness.models import Runner, RunnerAssignment
from apps.harness.routing import inherited_orders
from apps.workspaces import role_cap

from ..models import Agent, AgentCredential, AgentCredentialRead

#: Where a value comes from: stored here as zero-knowledge ciphertext
#:, the only way canopy-web stores one, or not stored at all.
SOURCE_CANOPY_WEB_ZK = "canopy-web-zk"


SOURCE_UNSET = "unset"


def set_agent_credentials(agent, values: dict[str, str], *, user=None) -> int:
    """Upsert named secrets. NON-CLOBBERING: a ref absent from `values` is left
    alone, so a single-field edit can never wipe the rest (the same rule
    RunnerCredentialIn follows, for the same reason).

    Rejects a blank value rather than storing one: "" is how a UI says "I did not
    type anything", and storing it makes a declared ref read as SET while
    resolving to nothing — the worst of both.

    Zero-knowledge is the agent vault's only mode: every value written in
    plaintext (`canopy vault import`, or one canopy-web made itself: the agent's canopy login,
    its rotation) is sealed to every reader of the
    agent (`zks1`, `zk_seal.py`) and the plaintext forgotten. Raises ZkError
    `zk_no_recipients` when nobody could read it yet; nothing is written then.
    """

    blank = sorted(k for k, v in values.items() if not (v or "").strip())
    if blank:
        raise ValueError(f"blank value for: {', '.join(blank)}")

    by = user if getattr(user, "is_authenticated", False) else None
    from django.db import transaction

    from ..credentials import (  # cycle: credentials.* -> services  # noqa: PLC0415
        accounts,
        zk,
        zk_seal,
    )

    accounts.refuse_delegated_names(agent, values)
    # Under the agent row lock, like every ZK write: a settle's
    # compare-and-swap (`expect`) must see this write or come after it.
    with transaction.atomic():
        agent = zk._locked(agent)
        sealed = {name.strip(): zk_seal.seal_for_agent(agent, name.strip(), value.strip())
                  for name, value in values.items()}
        for name, value_zk in sealed.items():
            AgentCredential.objects.update_or_create(
                agent=agent, name=name,
                defaults={"value_zk": value_zk, "kv": agent.zk_kv, "updated_by": by})
    return len(sealed)


def delete_agent_credential(agent, name: str) -> int:

    deleted, _ = AgentCredential.objects.filter(agent=agent, name=name).delete()
    return deleted


def agent_credential_status(agent) -> list[dict]:
    """Every ref the agent DECLARES, against what is actually set — booleans and
    timestamps, never values.

    This screen shows a dead mailbox, which otherwise takes
    SSH-ing to a box and running `gog auth list` to see. A credential that lives only in a vault and a
    keyring is a credential nobody is watching.

    Declared-but-unset rows appear (that is the question the page answers), and
    so do stored-but-undeclared ones — an orphan left behind when a ref was
    removed from its declaration is a live secret nothing accounts for, and hiding
    it is how it stays that way.
    """

    from django.db.models import Count, Max

    from ..credentials import accounts  # cycle: credentials.accounts -> services  # noqa: PLC0415

    stored = {c.name: c for c in AgentCredential.objects.filter(agent=agent)
              .select_related("updated_by", "delegated_by", "delegated_by_agent")}
    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    reads = {
        r["name"]: r for r in AgentCredentialRead.objects.filter(agent=agent)
        .values("name").annotate(last=Max("created_at"), n=Count("id"))
    }

    rows: list[dict] = []
    for name in declared + [n for n in sorted(stored) if n not in declared]:
        cred = stored.get(name)
        read = reads.get(name) or {}
        rows.append({
            "name": name,
            "declared": name in declared,
            "set": cred is not None,
            "source": SOURCE_CANOPY_WEB_ZK if cred else SOURCE_UNSET,
            "updated_at": cred.updated_at if cred else None,
            "updated_by_email": (cred.updated_by.email if cred and cred.updated_by_id else None),
            "last_read_at": read.get("last"),
            "read_count": read.get("n", 0),
            "account": cred.account if cred else "agent",
            "account_label": accounts.label(cred, agent) if cred else "",
        })
    return rows


def credential_reads(agent, name: str, *, limit: int = 100) -> list[dict]:
    """The read history of one credential, newest first."""

    qs = (AgentCredentialRead.objects.filter(agent=agent, name=name)
          .select_related("user", "runner").order_by("-created_at")[:limit])
    return [{
        "name": r.name,
        "user_email": r.user.email if r.user_id else "",
        "runner": str(r.runner_id) if r.runner_id else "",
        "runner_name": r.runner.name if r.runner_id else "",
        "via": r.via, "turn_id": r.turn_id, "created_at": r.created_at,
    } for r in qs]


def declared_credential_names(agent) -> set[str]:
    """Every ref the agent declares: `runtime_secrets` (the names, written by
    `canopy agent vault`)
    plus `runtime_sources` (where each lives). The set `?refs=` is scoped to."""
    names = {str(n).strip() for n in (agent.runtime_secrets or []) if str(n).strip()}
    names |= {str(n).strip() for n in (agent.runtime_sources or {}) if str(n).strip()}
    return names


def declare_credential_names(agent, names) -> list[str]:
    """Add `names` to the agent's declared refs (`runtime_secrets`); return the
    ones that were new.

    Called when the agent's owner or an admin stores a value: someone who may
    write the list anyway is not widening anything by naming a secret, and a
    stored-but-undeclared value is one the agent's scoped reads never get —
    the Security tab showed it "not declared" with no way to fix it there."""
    # Under the agent's row lock: two writes naming different secrets at once would each
    # read the list before the other saved it, and one name would be lost.
    with transaction.atomic():
        locked = Agent.objects.select_for_update().get(pk=agent.pk)
        current = [str(n) for n in (locked.runtime_secrets or [])]
        known = set(current) | {str(n) for n in (locked.runtime_sources or {})}
        added = sorted({str(n).strip() for n in names if str(n).strip()} - known)
        if added:
            locked.runtime_secrets = current + added
            locked.save(update_fields=["runtime_secrets"])
        agent.runtime_secrets = locked.runtime_secrets
    return added


def record_credential_reads(agent, names, *, user=None, runner=None, via="", turn_id="",
                            license=None) -> None:
    """One `AgentCredentialRead` row per ref handed out."""

    AgentCredentialRead.objects.bulk_create([
        AgentCredentialRead(
            agent=agent, name=str(n)[:120],
            user=user if getattr(user, "is_authenticated", False) else None,
            runner=runner, via=via[:16], turn_id=(turn_id or "")[:120], license=license,
        )
        for n in sorted(set(names))
    ])


def caller_runs_agent(user, agent) -> bool:
    """True when `user` pairs a live runner that this agent's routing could
    actually send work to.

    Tighter than workspace membership, deliberately: plaintext should reach a box
    that runs the agent, not everyone who can see it. Mirrors the runner
    credential fetch, whose trust boundary is "the caller who can claim turns as
    this runner".
    """

    if role_cap.cap_of(user):
        return False  # acting as a lower role: reads go through its license instead

    # ASSIGNED, not necessarily ENABLED. `enabled=False` removes a runner from
    # the automatic rotation — it stops being a fallback — but it does NOT stop
    # work being directed there: `claim_next_turn` checks `pinned_runner` first
    # and returns before any assignment lookup ("a pin trumps everything below
    # it"). That combination IS an explicit-only runner: it never fires the
    # agent's commands as a fallback, only when explicitly triggered.
    #
    # So requiring `enabled=True` here contradicted the claim path: a pinned turn
    # would land on the box, which would then be refused the very secrets it
    # needs to run the agent — a failure at the far end of a long round trip,
    # reading as a broken agent rather than a routing rule.
    #
    # The trust boundary is unchanged in substance: an assignment row at all
    # means this agent's work may be directed at this runner, and the caller must
    # still PAIR it. Disabling governs automatic routing, not trust.
    #
    # AND the owner must be one of the agent's admins (`runner_may_hold_agent`).
    # An assignment row alone is written by the EDITOR tier, which an
    # auto-approved access request hands out — so without this, an editor paired a box, added it to the list
    # (disabled, at the bottom) and read every secret, both vault keys and the
    # owner's GitHub token through `/credentials/resolve`.
    if not agent.is_admin(user):
        return False
    if RunnerAssignment.objects.filter(
        agent=agent, runner__owner=user,
    ).exclude(runner__status=Runner.RETIRED).exists():
        return True
    # An agent with no order of its own follows its workspace's: a box of this
    # user's in that order is one the agent's work is directed at.

    inh = inherited_orders([agent.id]).get(agent.id)
    return bool(inh) and any(r.owner_id == user.pk for r in inh.runners)


# ---- what the BOX observed ---------------------------------
