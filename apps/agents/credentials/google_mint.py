""""Connect Google mailbox", finished on one of the agent's runners — canopy-web never
sees the Google token (docs/architecture/vault.md § Connect Google mailbox).

The sign-in is an `AgentGoogleMint`, relayed between a person's browser and a runner
that may hold the agent (`services.runner_may_hold_agent`):

1. A person starts it; canopy-web picks an online runner that runs the agent, can read
   its vault, and says it can finish a Google sign-in (`capabilities.google_mint`).
   None online → refused plainly; there is no server fallback.
2. The runner (polling `mint/claim`) makes a PKCE verifier, keeps it, and posts only
   the challenge. canopy-web builds the consent link around it (its client id, the
   scopes the agent needs, the one callback, signed state naming the mint).
3. Google returns to canopy-web's callback with a single-use code. canopy-web holds it
   only until the runner takes it — and without the verifier it cannot redeem it.
4. The runner exchanges code + verifier + the OAuth client's secret (from the agent's
   vault) at Google, checks the account, builds the gog token, encrypts it for the agent
   (`zk1` under the vault key, else `zks1` to its readers) and posts ciphertext only.

The runner side is `canopy vault google-mint` (dimagi-internal/canopy) driven by
`runner/ec2/cloud_runner.py::_drain_google_mint`.

A PERSON can run the same sign-in for their OWN Google account (`start_delegated`): the
token is then a credential they delegated to the agent (`credentials/accounts.py`), named
`google-delegated-<account>`, sealed (`zks1`) only to the machines that may hold a
credential of theirs and to their own vault key — never under the vault key — and it
reaches a turn only through a runner resolve's `delegated` field. The person must be one
of the agent's admins: `accounts.serves` hands a delegated entry out only while they are.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from apps.harness.models import Runner

from .. import services
from ..models import AgentCredential, AgentGoogleMint
from . import google_oauth, zk, zk_seal

#: The credential slot the fleet already names in every agent's declaration: a minted
#: token lands where a hand-minted one does, so a box never has two sources of truth.
GOG_TOKEN_REF = "gog-token"
#: What a runner declares (`Runner.capabilities`) when it can finish a Google sign-in.
CAPABILITY = "google_mint"
#: An unfinished sign-in this old is dead: the signed state (and so the consent link)
#: is only good for `google_oauth.STATE_MAX_AGE`.
TTL_SECONDS = google_oauth.STATE_MAX_AGE
#: The outcomes a runner may report — the ones the settings page explains (`?google=`).
RUNNER_OUTCOMES = frozenset({"exchange-failed", "no-refresh-token", "no-email", "wrong-account",
                             "no-client", "zk-no-recipients", "bad-request", "interrupted"})
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_ACCOUNT = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: A delegated Google entry's name: one per Google account, so a person can delegate
#: their Gmail and another person theirs, and a turn asks for it by the account.
DELEGATED_PREFIX = "google-delegated-"


def delegated_entry_name(account: str) -> str:
    return f"{DELEGATED_PREFIX}{(account or '').strip().lower()}"


def expected_for(mint: AgentGoogleMint) -> str:
    """The account this sign-in must come from: the one a person named for their own
    delegated account, else the agent's own rule."""
    if mint.delegated_by_id:
        return mint.expected_account
    return google_oauth.expected_account(mint.agent)


def _accounts():
    from . import accounts  # cycle: accounts -> services  # noqa: PLC0415

    return accounts


class MintRefused(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def no_runner_detail(agent) -> str:
    return (f"No runner that may hold {agent.slug} is online to finish the Google sign-in. "
            f"The sign-in finishes on one of {agent.slug}'s own runners, so that canopy never "
            "sees the token — start one (or wait for one to come back online), then try again.")


def eligible_runners(agent, person=None) -> list[Runner]:
    """Online runners that run `agent`, may hold it, have a key that reads its vault, and
    can finish a Google sign-in — those holding a copy of its vault key first. With
    `person` (their own account, delegated): only runners with a machine that may hold a
    credential of theirs (`accounts.delegated_devices`: a cloud box, or their own)."""
    holders = None
    if person is not None:
        holders = {k.runner_id for k in _accounts().delegated_devices(agent, person)}
    keys = [k for k in zk.eligible_device_keys(agent) if holders is None or k.runner_id in holders]
    waiting = zk.devices_waiting(agent, keys)
    keys.sort(key=lambda k: k.pk in waiting)
    out: list[Runner] = []
    for key in keys:
        runner = key.runner
        if runner in out or runner.live_status not in (Runner.ONLINE, Runner.DEGRADED):
            continue
        if not (runner.capabilities or {}).get(CAPABILITY):
            continue
        if services.runner_may_hold_agent(runner, agent):
            out.append(runner)
    return out


def _expire(mint: AgentGoogleMint | None) -> AgentGoogleMint | None:
    if mint is None or mint.status in AgentGoogleMint.FINISHED:
        return mint
    if (timezone.now() - mint.updated_at).total_seconds() <= TTL_SECONDS:
        return mint
    return _fail(mint, "expired", "This sign-in expired before it finished — start another one.")


def _fail(mint: AgentGoogleMint, outcome: str, detail: str, *, account: str = "") -> AgentGoogleMint:
    mint.status, mint.outcome, mint.detail, mint.code = AgentGoogleMint.FAILED, outcome, detail[:900], ""
    if account:
        mint.account = account[:254]
    mint.save(update_fields=["status", "outcome", "detail", "code", "account", "updated_at"])
    return mint


def start(agent, *, user, login_hint: str = "") -> AgentGoogleMint:
    """A new sign-in for `agent`, handed to an eligible runner; supersedes any unfinished one."""
    client_id = google_oauth.google_client_id()
    if not client_id:
        raise MintRefused(503, "no_google_client", "this deployment has no Google OAuth client configured")
    runners = eligible_runners(agent)
    if not runners:
        raise MintRefused(409, "no_mint_runner", no_runner_detail(agent))
    with transaction.atomic():
        for old in AgentGoogleMint.objects.select_for_update().filter(
                agent=agent, delegated_by__isnull=True).exclude(status__in=AgentGoogleMint.FINISHED):
            _fail(old, "superseded", "superseded by a newer sign-in")
        return AgentGoogleMint.objects.create(agent=agent, runner=runners[0], requested_by=user,
                                              login_hint=login_hint[:254])


def start_delegated(agent, *, user, account: str, services=None) -> AgentGoogleMint:
    """A new sign-in for `user`'s OWN Google `account`, to delegate to `agent`; supersedes
    their unfinished one for it. Refused unless they are one of the agent's admins — a
    delegated entry is in force only while its person is (`accounts.serves`) — and unless
    a runner with a machine that may hold a credential of theirs is online."""
    account = (account or "").strip().lower()
    if not _ACCOUNT.match(account) or len(account) > 200:
        raise MintRefused(422, "bad_account", "name the Google account you will sign in with (you@gmail.com)")
    try:
        services = google_oauth.delegated_services(services)
    except ValueError as exc:
        raise MintRefused(422, "bad_services", str(exc)) from exc
    if not agent.is_admin(user):
        raise MintRefused(403, "not_an_admin", (
            f"only {agent.slug}'s owner or one of its admins can delegate an account of their own to it: "
            f"a delegated credential is used only while its person is an admin. Ask {agent.slug}'s owner "
            "to make you an admin of it."))
    name = delegated_entry_name(account)
    clash = agent.credentials.filter(name=name).first()
    if clash is not None and (clash.account != "delegated" or clash.delegated_by_id != user.pk):
        raise MintRefused(409, "entry_taken", (
            f"{agent.slug} already holds {name} from someone else — only they can replace or withdraw it"))
    if not google_oauth.google_client_id():
        raise MintRefused(503, "no_google_client", "this deployment has no Google OAuth client configured")
    runners = eligible_runners(agent, user)
    if not runners:
        raise MintRefused(409, "no_mint_runner", (
            f"No runner that may hold a credential of yours for {agent.slug} is online to finish the "
            f"sign-in. Your account can be opened only by a cloud runner that runs {agent.slug}, or a "
            "runner of your own — never another person's laptop. Start one (or wait for one to come "
            "back online), then try again."))
    with transaction.atomic():
        for old in AgentGoogleMint.objects.select_for_update().filter(agent=agent, delegated_by=user).exclude(
                status__in=AgentGoogleMint.FINISHED):
            _fail(old, "superseded", "superseded by a newer sign-in")
        return AgentGoogleMint.objects.create(agent=agent, runner=runners[0], requested_by=user,
                                              delegated_by=user, expected_account=account,
                                              services=services, login_hint=account)


def get(agent, mint_id, *, delegated_by=None) -> AgentGoogleMint | None:
    """The sign-in by id — with `delegated_by`, only that person's own delegated one."""
    rows = AgentGoogleMint.objects.filter(agent=agent, pk=mint_id)
    rows = rows.filter(delegated_by=delegated_by) if delegated_by is not None else rows.filter(
        delegated_by__isnull=True)
    return _expire(rows.select_related("runner").first())


# ---- the runner's side ------------------------------------------------------------

def claim(runner) -> tuple[AgentGoogleMint | None, str]:
    """The sign-in this runner owes work on — asked for a challenge, or handed a code —
    and the code, CONSUMED: it is spent on first use, and one left in a row is a
    credential nobody accounts for."""
    mint = _expire(AgentGoogleMint.objects.filter(runner=runner).exclude(
        status__in=AgentGoogleMint.FINISHED).select_related("agent", "delegated_by").order_by(
        "-created_at").first())
    if mint is None or mint.status not in (AgentGoogleMint.REQUESTED, AgentGoogleMint.COMPLETING):
        return None, ""
    # A taken code is the runner's to finish: it is not handed out twice.
    if mint.status == AgentGoogleMint.COMPLETING and not mint.code:
        return None, ""
    if not services.runner_may_hold_agent(runner, mint.agent):
        _fail(mint, "no-runner", f"{runner.name} may no longer hold {mint.agent.slug}")
        return None, ""
    if mint.delegated_by_id and runner.pk not in {
            k.runner_id for k in _accounts().delegated_devices(mint.agent, mint.delegated_by)}:
        _fail(mint, "no-runner", f"{runner.name} may no longer hold a credential of {mint.delegated_by.email}")
        return None, ""
    code = ""
    if mint.status == AgentGoogleMint.COMPLETING:
        code, mint.code = mint.code, ""
        mint.save(update_fields=["code", "updated_at"])
    return mint, code


def work(mint: AgentGoogleMint, code: str) -> dict:
    """What the runner needs for the step it owes: a challenge needs nothing; the
    exchange needs the code, the client, the account to insist on, and who to encrypt for."""
    agent = mint.agent
    out = {"id": str(mint.id), "agent": agent.slug, "status": mint.status}
    if mint.status == AgentGoogleMint.COMPLETING:
        client_id = google_oauth.google_client_id()
        out.update(code=code, redirect_uri=google_oauth.callback_url(), client_id=client_id,
                   client=google_oauth.MINTING_CLIENT,
                   expected_account=expected_for(mint),
                   agent_id=str(agent.pk), kv=agent.zk_kv if agent.zk_enabled else 0,
                   recipients=[{"kind": kind, "target": target, "public_jwk": jwk}
                               for kind, target, jwk in zk_seal.recipients_for(agent)])
        if mint.delegated_by_id:
            # A person's own account: sealed to the machines that may hold a credential
            # of theirs and to their own vault key — never under the vault key, so the
            # box must not use one it holds (`delegated`), and under its own name.
            out.update(delegated=True, name=delegated_entry_name(mint.expected_account), kv=agent.zk_kv,
                       recipients=[{"kind": kind, "target": target, "public_jwk": jwk}
                                   for kind, target, jwk in _accounts().recipients(agent, mint.delegated_by)])
    return out


def _runner_mint(runner, mint_id, status: str) -> AgentGoogleMint:
    mint = _expire(AgentGoogleMint.objects.filter(pk=mint_id, runner=runner).select_related("agent").first())
    if mint is None or mint.status != status:
        raise MintRefused(409, "not_waiting", "this runner owes no such step on that sign-in")
    return mint


def record_challenge(runner, mint_id, challenge: str) -> AgentGoogleMint:
    """The runner made (and kept) its verifier: build the consent link around its challenge."""
    mint = _runner_mint(runner, mint_id, AgentGoogleMint.REQUESTED)
    if not _CHALLENGE.match(challenge or ""):
        raise MintRefused(422, "bad_challenge", "code_challenge must be base64url (S256), 43-128 characters")
    agent = mint.agent
    client_id = google_oauth.google_client_id()
    state = google_oauth.sign_state(agent_slug=agent.slug, user_pk=mint.requested_by_id, mint=str(mint.id))
    hint = mint.login_hint or google_oauth.expected_account(agent) or f"{agent.slug}@dimagi-ai.com"
    scopes = (google_oauth.delegated_scopes(mint.services) if mint.delegated_by_id
              else google_oauth.scopes_for(agent))
    mint.code_challenge = challenge
    mint.authorize_url = google_oauth.authorize_url(client_id=client_id, state=state, login_hint=hint,
                                                    scopes=scopes, code_challenge=challenge)
    mint.status = AgentGoogleMint.AWAITING
    mint.save(update_fields=["code_challenge", "authorize_url", "status", "updated_at"])
    return mint


def runner_failed(runner, mint_id, *, outcome: str, detail: str, account: str = "") -> AgentGoogleMint:
    """The runner could not do its step (no challenge, or the exchange refused)."""
    mint = AgentGoogleMint.objects.filter(pk=mint_id, runner=runner).first()
    if mint is None or mint.status in AgentGoogleMint.FINISHED:
        raise MintRefused(409, "not_waiting", "no unfinished sign-in by that id on this runner")
    return _fail(mint, outcome if outcome in RUNNER_OUTCOMES else "exchange-failed",
                 detail or "the runner could not finish the sign-in", account=account)


def finish(runner, mint_id, *, kv: int, value_zk: str, account: str, name: str = "") -> AgentGoogleMint:
    """Store the token the runner encrypted for the agent — ciphertext only, checked to
    be for this agent's readers — once the account it names is the one the agent needs."""
    mint = _runner_mint(runner, mint_id, AgentGoogleMint.COMPLETING)
    if mint.delegated_by_id:
        return _finish_delegated(runner, mint, value_zk=value_zk, account=account, name=name)
    agent = mint.agent
    if not services.runner_may_hold_agent(runner, agent):
        return _fail(mint, "no-runner", f"{runner.name} may no longer hold {agent.slug}")
    expected = google_oauth.expected_account(agent)
    account = (account or "").strip()
    if not account:
        return _fail(mint, "no-email", "the runner did not say which Google account consented")
    if expected and account.lower() != expected:
        return _fail(mint, "wrong-account", f"Google signed in as {account}, not {expected}", account=account)
    if not (value_zk.startswith(zk_seal.SEALED_PREFIX) or value_zk.startswith("zk1:")):
        raise MintRefused(422, "not_ciphertext", "value_zk must be zk1 or zks1 ciphertext — never a token")
    allowed = {(kind, target) for kind, target, _ in zk_seal.recipients_for(agent)}
    if zk_seal.sealed_targets(value_zk) - allowed:
        raise MintRefused(422, "foreign_reader", "the token is sealed to a reader that may not read this agent")
    from .accounts import refuse_delegated_names  # cycle: accounts -> services  # noqa: PLC0415

    try:
        refuse_delegated_names(agent, [GOG_TOKEN_REF])
        with transaction.atomic():
            locked = zk._locked(agent)
            services.start_zero_knowledge(locked)
            zk.set_values(locked, kv=kv, values=[SimpleNamespace(name=GOG_TOKEN_REF, value_zk=value_zk)],
                          user=mint.requested_by)
    except zk.ZkError as exc:
        outcome = "zk-no-recipients" if exc.code in ("zk_no_recipients", "zk_no_vault_key") else "store-failed"
        return _fail(mint, outcome, f"the token could not be stored: {exc}", account=account)
    mint.status, mint.outcome, mint.account, mint.detail = AgentGoogleMint.DONE, "ok", account[:254], ""
    mint.save(update_fields=["status", "outcome", "account", "detail", "updated_at"])
    return mint


# ---- the browser's return trip -----------------------------------------------------

def receive_code(mint: AgentGoogleMint, *, code: str, error: str) -> AgentGoogleMint:
    """Google sent the person back: hold the code for the runner, or record the refusal."""
    mint = _expire(mint)
    if mint.status != AgentGoogleMint.AWAITING:
        return mint
    if error or not code:
        return _fail(mint, "denied", "the sign-in was cancelled on Google's side")
    mint.code, mint.status = code.strip(), AgentGoogleMint.COMPLETING
    mint.save(update_fields=["code", "status", "updated_at"])
    return mint


def _finish_delegated(runner, mint: AgentGoogleMint, *, value_zk: str, account: str, name: str) -> AgentGoogleMint:
    """Store a person's own Google token as a credential they delegated to the agent: only
    a `zks1` value sealed to the machines that may hold a credential of theirs (and their
    own vault key), under the name it was sealed with, from the account they named."""
    accounts = _accounts()
    agent, person = mint.agent, mint.delegated_by
    if not services.runner_may_hold_agent(runner, agent):
        return _fail(mint, "no-runner", f"{runner.name} may no longer hold {agent.slug}")
    if not agent.is_admin(person):
        return _fail(mint, "not-admin", f"{person.email} is no longer an admin of {agent.slug}")
    account = (account or "").strip()
    if not account:
        return _fail(mint, "no-email", "the runner did not say which Google account consented")
    if account.lower() != mint.expected_account:
        return _fail(mint, "wrong-account", f"Google signed in as {account}, not {mint.expected_account}",
                     account=account)
    entry_name = delegated_entry_name(mint.expected_account)
    if (name or "").strip() != entry_name:
        # An older canopy CLI seals every Google token as the agent's own `gog-token`
        # (under its name, maybe under the vault key): stored here, no machine could open it.
        return _fail(mint, "outdated-runner", (
            f"{runner.name}'s canopy CLI cannot seal a delegated Google account yet — it updates "
            "itself; try again later"), account=account)
    if not value_zk.startswith(zk_seal.SEALED_PREFIX):
        raise MintRefused(422, "not_sealed", "a delegated credential is sealed (zks1) to the machines that "
                          "may hold it — never a token, never under the vault key")
    try:
        zk_seal.check_sealed_value(value_zk, entry_name)
    except Exception as exc:  # noqa: BLE001 — the shape error names what is wrong
        raise MintRefused(422, "not_ciphertext", str(exc)) from exc
    allowed = {(kind, target) for kind, target, _ in accounts.recipients(agent, person)}
    targets = zk_seal.sealed_targets(value_zk)
    if targets - allowed:
        raise MintRefused(422, "foreign_reader", "the token is sealed to a reader that may not hold a "
                          f"credential of {person.email}'s for {agent.slug}")
    if not any(kind == "device" for kind, _ in targets):
        return _fail(mint, "zk-no-recipients", f"no machine that runs {agent.slug} could open it", account=account)
    services_ = list(mint.services or google_oauth.DEFAULT_DELEGATED_SERVICES)
    with transaction.atomic():
        agent = zk._locked(agent)
        clash = AgentCredential.objects.filter(agent=agent, name=entry_name).first()
        if clash is not None and (clash.account != "delegated" or clash.delegated_by_id != person.pk):
            return _fail(mint, "taken", f"{agent.slug} already holds {entry_name} from someone else",
                         account=account)
        AgentCredential.objects.update_or_create(
            agent=agent, name=entry_name,
            defaults={
                "value_zk": value_zk, "kv": agent.zk_kv, "updated_by": person,
                "account": "delegated", "delegated_by": person, "delegated_by_agent": None,
                "provider": "google", "identity": {"email": mint.expected_account},
                "purpose": f"{agent.slug} uses {mint.expected_account}'s {', '.join(services_)}"[:200],
                "scopes": list(google_oauth.delegated_scopes(services_)),
                "provider_expires_at": None, "checks": [], "checked_at": None, "checked_by": "",
            })
    mint.status, mint.outcome, mint.account, mint.detail = AgentGoogleMint.DONE, "ok", account[:254], ""
    mint.save(update_fields=["status", "outcome", "account", "detail", "updated_at"])
    return mint
