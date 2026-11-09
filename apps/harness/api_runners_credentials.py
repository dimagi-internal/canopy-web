"""/api/harness runner routes: a runner's credentials, logins, vault, GitHub token and attachments.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Status
from ninja.errors import HttpError

from apps.agents.credentials import workspace_vaults
from apps.agents.credentials.zk import ZkError
from apps.agents.holding import runner_may_hold_agent
from apps.agents.route_refusals import zk_problem
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.canopy_sessions.models import Attachment
from apps.passkeys import stepup

from . import (
    credential_sets,
    routing,
    runner_vault_resolve,
    sensitivity,
)
from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_admin_or_404,
    runner_or_404,
)
from .api_runners import _list_admin_or_404
from .models import (
    RunnerSlashCommands,
    Turn,
)
from .schemas import (
    RunnerCredentialOut,
    RunnerCredentialPatchIn,
    RunnerCredentialResolveOut,
    RunnerCredentialStatusOut,
    RunnerGitHubReadinessOut,
    RunnerLoginOrderIn,
    RunnerLoginReportIn,
    RunnerVaultEntryIn,
    RunnerVaultEntryOut,
    RunnerVaultOut,
    RunnerVaultValuesIn,
    RunnerVaultValuesOut,
    SlashCommandsIn,
    TurnGitHubTokenOut,
)


# The fixed path is declared before `/credentials/{key}`, which would match it.
@router.put("/runners/{runner_id}/credentials/order", response=RunnerCredentialStatusOut,
            summary="Set the order a runner tries its Claude credentials in")
@gate("runner-admin", setting=True)
def order_runner_credentials(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerLoginOrderIn):
    """`keys` is every credential on the runner, first to last. The runner uses
    the first that works."""
    runner = _list_admin_or_404(request, runner_id)
    try:
        harness_runners.order_runner_credentials(runner, payload.keys, by=request.user)
    except harness_runners.LoginOrderRefused as exc:
        raise HttpError(422, str(exc)) from exc
    return harness_runners.runner_credential_status(runner)


@router.patch("/runners/{runner_id}/credentials/{key}", response=RunnerCredentialStatusOut,
              summary="Rename a token or API key")
@gate("runner-admin", setting=True)
def update_runner_credential(request: HttpRequest, runner_id: uuid.UUID, key: str,
                             payload: RunnerCredentialPatchIn):
    """Only its name: a new value is sealed into the runner's vault (in the browser,
    or by the box after a sign-in), never sent here."""
    runner = _list_admin_or_404(request, runner_id)
    if not harness_runners.update_runner_credential(runner, key, name=payload.name, by=request.user):
        raise HttpError(404, "no such token or API key on this runner")
    return harness_runners.runner_credential_status(runner)


@router.delete("/runners/{runner_id}/credentials/{key}", response=RunnerCredentialStatusOut,
               summary="Remove a credential from a runner")
@gate("runner-admin", setting=True)
def remove_runner_credential(request: HttpRequest, runner_id: uuid.UUID, key: str):
    """A login is signed out on the box, a token or key forgotten, the next time
    the runner re-reads its credentials — within a few minutes. Refused for the
    runner's last token or API key."""
    runner = _list_admin_or_404(request, runner_id)
    try:
        removed = harness_runners.remove_runner_credential(runner, key, by=request.user)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(422, str(exc)) from exc
    if not removed:
        raise HttpError(404, "no such credential on this runner")
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/credential/swap", response=RunnerCredentialStatusOut,
             summary="Swap the runner's first two tokens")
@gate("runner-admin", setting=True)
def swap_runner_logins(request: HttpRequest, runner_id: uuid.UUID):
    """What a runner from before the credential list asks for when its second
    token ran a turn its first could not."""
    runner = _list_admin_or_404(request, runner_id)
    harness_runners.swap_runner_logins(runner, updated_by=request.user)
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/logins", response=RunnerCredentialStatusOut,
             summary="Add a Claude login to a runner and start signing it in")
@gate("runner-admin", setting=True)
def add_runner_login(request: HttpRequest, runner_id: uuid.UUID):
    """A new full Claude login, last in the runner's cascade. The runner picks up
    the sign-in on its next poll (see `get_runner_mint`); the login reads
    "not signed in" until it finishes."""
    runner = runner_admin_or_404(request, runner_id)
    try:
        harness_runners.add_runner_login(runner, by=request.user)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(409, str(exc)) from exc
    return harness_runners.runner_credential_status(runner)


# The fixed paths are declared before `/logins/{key}`, which would match them.
@router.put("/runners/{runner_id}/logins/order", response=RunnerCredentialStatusOut,
            summary="Set the order a runner tries its Claude logins in")
@gate("runner-admin", setting=True)
def order_runner_logins(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerLoginOrderIn):
    """The old name for `PUT /credentials/order`: every credential, first to last."""
    runner = _list_admin_or_404(request, runner_id)
    try:
        harness_runners.order_runner_logins(runner, payload.keys)
    except harness_runners.LoginOrderRefused as exc:
        raise HttpError(422, str(exc)) from exc
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/logins/report", response=RunnerCredentialStatusOut,
             summary="The runner reports which of its logins are signed in")
@gate("runner", kind="runner-protocol")
def report_runner_logins(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerLoginReportIn):
    """Which logins the box holds and as which account."""
    runner = runner_or_404(request, runner_id)
    harness_runners.report_runner_logins(
        runner, [r.dict() for r in payload.logins], adopt_default=payload.adopt_default)
    return harness_runners.runner_credential_status(runner)


@router.put("/runners/{runner_id}/slash-commands", response={204: None},
            summary="The runner reports the slash commands its sessions can run")
@gate("runner", kind="runner-protocol")
def report_slash_commands(request: HttpRequest, runner_id: uuid.UUID, payload: SlashCommandsIn):
    """The chat composer's `/` autocomplete for sessions on this box. The whole
    list, replacing what was held."""
    runner = runner_or_404(request, runner_id)
    RunnerSlashCommands.objects.update_or_create(
        runner_id=runner.pk, defaults={"commands": [c.model_dump() for c in payload.commands]})
    return Status(204, None)


@router.delete("/runners/{runner_id}/logins/{key}", response=RunnerCredentialStatusOut,
               summary="Remove a Claude login from a runner")
@gate("runner-admin", setting=True)
def remove_runner_login(request: HttpRequest, runner_id: uuid.UUID, key: str):
    """The runner signs the login out on the box the next time it re-reads its
    credentials, within a few minutes."""
    runner = _list_admin_or_404(request, runner_id)
    try:
        removed = harness_runners.remove_runner_credential(runner, key, by=request.user)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(422, str(exc)) from exc
    if not removed:
        raise HttpError(404, "no such login on this runner")
    return harness_runners.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential/status", response=RunnerCredentialStatusOut,
            summary="Which credential slots are set (masked — booleans, never values)")
@gate("runner-admin")
def get_runner_credential_status(request: HttpRequest, runner_id: uuid.UUID):
    """The operator's read: which slots are filled, without putting a secret on a
    screen. Separate from the GET below, which returns REAL VALUES for the runner
    to consume.

    Exists because the only way to read this used to be a no-op POST — and that
    WROTE, bumping `updated_at`/`updated_by` and destroying the one signal that
    says when a credential was last actually rotated."""
    runner = runner_admin_or_404(request, runner_id)
    return harness_runners.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential", response=RunnerCredentialOut,
            summary="Fetch this runner's credential bundle (the runner, via its PAT)")
@gate("runner")  # the box's own list, never a value: those are opened from its vault (…/credential/resolve)
def get_runner_credential(request: HttpRequest, runner_id: uuid.UUID) -> RunnerCredentialOut:
    """The runner's credential list, for the box to stage its credentials: its
    logins (`key`, `account`, `cli`), every entry in order (`cascade`: `key`,
    `kind`, `cli`, and a token's or API key's `vault_secret` name when a vault
    serving the runner holds the value), whether a vault with a key is open
    (`vault_open`) and when the list last changed. It holds NO values:
    canopy-web has none, and the box opens each `vault_secret` with its own
    device key (`…/credential/resolve`). Entries of another CLI go only to a box
    whose code reports that CLI. Gated to the runner protocol: the runner's owner (the
    box calls with its owner's token). A person managing the list reads
    `…/credential/status` instead."""
    runner = runner_or_404(request, runner_id)
    return RunnerCredentialOut(**harness_runners.get_runner_credential(runner))


@router.get("/runners/{runner_id}/credential/resolve", response=RunnerCredentialResolveOut,
            summary="This runner's Claude credentials from its vault, as ciphertext (the box, with its device key)")
@gate("runner", step_up="runner protocol")  # + the box's own runner token and a device proof of a key on it
def resolve_runner_credential(request: HttpRequest, runner_id: uuid.UUID) -> RunnerCredentialResolveOut:
    """Each vault serving this runner, with THIS device's copy of its key (`wrap: null`
    when it holds none) and the `zk1:` values the runner's credential list names
    (`claude-token:<key>`, `anthropic-api-key:<key>`). Only the box's own runner token,
    with a proof of an active device key on this runner, gets an answer: 403
    `runner_only` for a person's token or session, 401 `device_proof_required` without
    a proof. Every value handed out is logged."""
    runner = runner_or_404(request, runner_id)
    out = runner_vault_resolve.resolve(request, runner)
    if isinstance(out, runner_vault_resolve.Refusal):
        raise ProblemError(out.status, "Runner credentials", detail=out.detail, members={"error": out.code})
    return RunnerCredentialResolveOut(vaults=out)


@router.put("/runners/{runner_id}/vault/values", response=RunnerVaultValuesOut,
            summary="The box seals its own credentials into its vault")
@gate("runner", kind="runner-protocol", step_up="runner protocol")  # + the box's own runner token and a device proof
def put_runner_vault_values(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerVaultValuesIn) -> RunnerVaultValuesOut:
    """`{vault_id, kv, values: [{name, value_zk}]}` — `zk1:` ciphertext the box made with
    its copy of the vault key, for names its credential list uses
    (`claude-token:<key>`, `anthropic-api-key:<key>`), into a vault serving it. Each entry
    filled drops its stored copy here. Only the box's own runner token with a device
    proof of a key on this runner (as `…/credential/resolve`)."""
    runner = runner_or_404(request, runner_id)
    refused = runner_vault_resolve.box_refusal(request, runner)
    if refused is not None:
        raise ProblemError(refused.status, "Runner credentials", detail=refused.detail,
                           members={"error": refused.code})
    try:
        names = harness_runners.store_vault_values(
            runner, vault_id=payload.vault_id, kv=payload.kv,
            values={v.name: v.value_zk for v in payload.values})
    except harness_runners.VaultWriteRefused as exc:
        raise HttpError(422, str(exc)) from exc
    except ZkError as exc:
        raise zk_problem(exc) from exc
    return RunnerVaultValuesOut(names=names)


@router.post("/runners/{runner_id}/vault/entries", response=RunnerVaultEntryOut,
             summary="Add a token or API key whose value is sealed into the runner's vault")
@gate("runner-admin", setting=True)
def add_vaulted_runner_credential(request: HttpRequest, runner_id: uuid.UUID,
                                  payload: RunnerVaultEntryIn) -> RunnerVaultEntryOut:
    """The entry, at the end of the list, with no value here: the caller (a browser
    holding the vault's key) then seals the value under `secret_name` with
    `PUT /api/vaults/{vault_id}/zk/values`. 422 when the runner has no vault with a key."""
    runner = _list_admin_or_404(request, runner_id)
    try:
        entry = harness_runners.add_runner_credential(runner, kind=payload.kind, name=payload.name,
                                                      by=request.user, cli=payload.cli)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(422, str(exc)) from exc
    vault = harness_runners.open_runner_vault(runner)
    return RunnerVaultEntryOut(key=entry.key, secret_name=entry.vault_secret_name,
                               vault_id=str(vault.pk), kv=vault.kv)


@router.post("/runners/{runner_id}/vault", response=RunnerVaultOut,
             summary="Give a cloud runner its own vault for its Claude credentials")
@gate("runner-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def create_runner_vault(request: HttpRequest, runner_id: uuid.UUID) -> RunnerVaultOut:
    """The runner's own workspace vault (`<runner> Claude credentials`): its owner
    manages it, the workspace's owners too, and it is attached to this runner — the box
    becomes eligible for a copy of its key once someone makes the key on the vault's
    page. Returns the vault the runner already has, if any. Sharing one vault between
    boxes is an attach (`POST /api/vaults/{id}/attachments`, `{runner}`)."""
    runner = runner_admin_or_404(request, runner_id)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        # A runner on a credential set is served by the set's vault for its tree.
        v = (credential_sets.attach_member(runner.credential_set, runner, by=request.user)
             if runner.credential_set_id else workspace_vaults.create_for_runner(runner, by=request.user))
    except ZkError as exc:
        raise zk_problem(exc) from exc
    return RunnerVaultOut(vault_id=str(v.pk), name=v.name, kv=v.kv)


@router.post("/runners/{runner_id}/turns/{turn_id}/github-token",
             response=TurnGitHubTokenOut,
             summary="One claimed turn's GitHub credential (its agent owner's)")
# turn must be claimed by this box
@gate("runner", "runner-holds-agent", kind="runner-protocol", step_up="runner protocol")
def turn_github_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID):
    """The GitHub token and git identity for ONE turn this runner is executing:
    the turn's agent owner's token for that agent. canopy decides whose — the
    runner only names the turn. Refused (409, with the reason) when the owner
    has lent none or it has expired, and always for a confined turn (read mode or a
    host site); there is no shared fallback."""
    from apps.agents.credentials import delegations

    runner = runner_or_404(request, runner_id)
    # Only a turn this runner holds right now. A finished turn, or one another
    # box claimed, gets the same 404 as one that does not exist.
    turn = (
        Turn.objects.select_related("agent__owner", "chat_session__agent__owner",
                                    "initiator_user", "initiator_contact")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
        .first()
    )
    if turn is None:
        raise HttpError(404, "turn not found")
    # A claim already requires this; re-asked here because a owner can be
    # demoted mid-turn, and this is the owner's GitHub identity.
    agent = delegations.turn_agent(turn)
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "turn not found")
    try:
        issued = delegations.github_token_for_turn(turn)
    except delegations.DelegationError as exc:
        raise ProblemError(409, "No GitHub identity for this turn", detail=str(exc)) from exc
    agent = delegations.turn_agent(turn)
    try:
        from apps.events import services as events

        events.record(
            [{
                "source": "agents.delegations",
                "kind": "agent.github.issued",
                "level": "info",
                "key": f"{agent.slug}:{turn.pk}",
                "summary": f"{agent.slug}: GitHub identity for one turn on {runner.name}",
                "payload": {"agent": agent.slug, "turn": str(turn.pk),
                            "runner": str(runner.pk), "as": issued["github_login"],
                            "delegated": issued["delegated"]},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a turn its token
        pass
    return issued


@router.get("/runners/{runner_id}/turns/{turn_id}/attachments/{attachment_id}/content",
            summary="An attachment from the chat a turn this runner holds belongs to")
@gate("runner", "runner-holds-agent")  # an attachment of the chat a turn this box holds belongs to
def turn_attachment_content(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID,
                            attachment_id: uuid.UUID):
    """The bytes of a file a person sent in the chat this runner is serving, so
    the agent can open it. Allowed while this runner holds the turn, for any
    attachment already sent in that turn's chat — earlier messages included, so
    a box with no local copy (a fresh cloud machine) can still fetch them.
    Everything else is the same 404 as an id that does not exist."""
    # Authorized by the TURN, not by whose token the box carries: the runner
    # speaks as its owner, and its owner need not be able to read the chat (a
    # teammate's private chat with an agent is visible to its creator and
    # participants only, canopy_sessions/access.py). Claiming the turn is what
    # entitles the box to it — the same rule as turn_github_token above.
    from apps.agents.credentials import delegations
    from apps.canopy_sessions import attachment_storage, private_attachments

    runner = runner_or_404(request, runner_id)
    turn = (
        Turn.objects.select_related("agent", "chat_session__agent")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
        .first()
    )
    if turn is None or turn.chat_session_id is None:
        raise HttpError(404, "attachment not found")
    # A claim already requires this; re-asked because an owner can be demoted
    # mid-turn.
    agent = delegations.turn_agent(turn)
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "attachment not found")
    if sensitivity.is_sensitive(turn.chat_session):
        # A private conversation's file is the agent's for ONE turn
        # (`private_attachments`): only a file that turn claimed, only while this box
        # holds it, and only once — the bytes are deleted as they are served. Every
        # other ask, an earlier message's file or a second fetch, is the same 404.
        attachment = Attachment.objects.filter(
            pk=attachment_id, session_id=turn.chat_session_id, sensitive=True, turn=turn,
            sent_at__isnull=False).first()
        response = private_attachments.take_for_runner(attachment) if attachment is not None else None
        if response is None:
            raise HttpError(404, "attachment not found")
        return response
    # Sent only: an unsent attachment is still someone's draft (sent_at, not
    # message, marks a send — a runner-origin session writes no Message row).
    attachment = Attachment.objects.filter(
        pk=attachment_id, session_id=turn.chat_session_id, sent_at__isnull=False, sensitive=False,
    ).first()
    if attachment is None:
        raise HttpError(404, "attachment not found")
    return attachment_storage.http_response(attachment)


@router.get("/runners/{runner_id}/github-readiness",
            response=list[RunnerGitHubReadinessOut],
            summary="Can each agent this runner serves open a pull request?")
@gate("runner")
def runner_github_readiness(request: HttpRequest, runner_id: uuid.UUID):
    """Checked live against GitHub, per agent routed to this runner. A box calls
    this when it boots so a missing, expired or under-scoped token is a health
    check going red, not a 403 in the middle of a turn."""
    from apps.agents.credentials import delegations

    runner = runner_or_404(request, runner_id)
    out = []
    for agent in routing.agents_routed_to(runner):
        delegations.check_github(agent)
        status, detail = delegations.readiness(agent)
        st = delegations.status(agent)
        out.append({"agent_slug": agent.slug, "status": status, "detail": detail,
                    "login": st.get("login", ""), "expires_at": st.get("expires_at")})
    return out


@router.get("/runners/{runner_id}/agents", response=list[str],
            summary="The agents this runner runs: the ones it should prepare")
@gate("runner")
def runner_agents(request: HttpRequest, runner_id: uuid.UUID) -> list[str]:
    """Slugs of the agents this box RUNS (`routing.agents_routed_to`): the
    claim's own static gates, so a box prepares exactly what it can be handed.
    The box's refresh prepares only these, so it never asks for the vault of an
    agent it does not run."""
    from . import device_keys

    runner = runner_or_404(request, runner_id)
    return [a.slug for a in device_keys.agents_served(runner)]
