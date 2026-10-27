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
"""
from __future__ import annotations

import re
from types import SimpleNamespace

from django.db import transaction
from django.utils import timezone

from apps.harness.models import Runner

from .. import services
from ..models import AgentGoogleMint
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


class MintRefused(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def no_runner_detail(agent) -> str:
    return (f"No runner that may hold {agent.slug} is online to finish the Google sign-in. "
            f"The sign-in finishes on one of {agent.slug}'s own runners, so that canopy never "
            "sees the token — start one (or wait for one to come back online), then try again.")


def eligible_runners(agent) -> list[Runner]:
    """Online runners that run `agent`, may hold it, have a key that reads its vault, and
    can finish a Google sign-in — those holding a copy of its vault key first."""
    keys = [k for k in zk.eligible_device_keys(agent)]
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
        for old in AgentGoogleMint.objects.select_for_update().filter(agent=agent).exclude(
                status__in=AgentGoogleMint.FINISHED):
            _fail(old, "superseded", "superseded by a newer sign-in")
        return AgentGoogleMint.objects.create(agent=agent, runner=runners[0], requested_by=user,
                                              login_hint=login_hint[:254])


def get(agent, mint_id) -> AgentGoogleMint | None:
    return _expire(AgentGoogleMint.objects.filter(agent=agent, pk=mint_id).select_related("runner").first())


# ---- the runner's side ------------------------------------------------------------

def claim(runner) -> tuple[AgentGoogleMint | None, str]:
    """The sign-in this runner owes work on — asked for a challenge, or handed a code —
    and the code, CONSUMED: it is spent on first use, and one left in a row is a
    credential nobody accounts for."""
    mint = _expire(AgentGoogleMint.objects.filter(runner=runner).exclude(
        status__in=AgentGoogleMint.FINISHED).select_related("agent").order_by("-created_at").first())
    if mint is None or mint.status not in (AgentGoogleMint.REQUESTED, AgentGoogleMint.COMPLETING):
        return None, ""
    # A taken code is the runner's to finish: it is not handed out twice.
    if mint.status == AgentGoogleMint.COMPLETING and not mint.code:
        return None, ""
    if not services.runner_may_hold_agent(runner, mint.agent):
        _fail(mint, "no-runner", f"{runner.name} may no longer hold {mint.agent.slug}")
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
                   expected_account=google_oauth.expected_account(agent),
                   agent_id=str(agent.pk), kv=agent.zk_kv if agent.zk_enabled else 0,
                   recipients=[{"kind": kind, "target": target, "public_jwk": jwk}
                               for kind, target, jwk in zk_seal.recipients_for(agent)])
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
    mint.code_challenge = challenge
    mint.authorize_url = google_oauth.authorize_url(client_id=client_id, state=state, login_hint=hint,
                                                    scopes=google_oauth.scopes_for(agent),
                                                    code_challenge=challenge)
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


def finish(runner, mint_id, *, kv: int, value_zk: str, account: str) -> AgentGoogleMint:
    """Store the token the runner encrypted for the agent — ciphertext only, checked to
    be for this agent's readers — once the account it names is the one the agent needs."""
    mint = _runner_mint(runner, mint_id, AgentGoogleMint.COMPLETING)
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
