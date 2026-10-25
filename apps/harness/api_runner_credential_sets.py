"""Credential sets — the routes (`apps/harness/credential_sets.py` is the mechanism).

* `GET /api/harness/runner-credential-sets/` — your sets, who reads each, and each
  workspace tree's vault with the entries it has no value for yet.
* `POST /api/harness/runner-credential-sets/` — share a cloud runner's own list as a
  new set (its entries and vault move with it).
* `PUT /api/harness/runner-credential-sets/runners/{runner_id}` — which set a runner
  reads: join one, or `null` to read its own list again.

Only a set's owner sees or changes it, and only the owner of a runner puts it on one:
every runner on a set is its owner's, as an isolation boundary is one person's.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.credentials.zk import ZkError
from apps.agents.route_refusals import zk_problem
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.events import audit as events_audit
from apps.passkeys import stepup

from . import credential_sets
from . import runners as harness_runners
from .api import runner_admin_or_404
from .models import Runner, RunnerCredentialSet
from .schemas import (
    RunnerCredentialSetChoiceIn,
    RunnerCredentialSetIn,
    RunnerCredentialSetOut,
    RunnerCredentialStatusOut,
)

router = Router(auth=session_auth, tags=["harness"])


def _own_runner(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    runner = runner_admin_or_404(request, runner_id)
    if runner.owner_id != request.user.pk:
        # An administrator sets its credentials; only its owner decides whose list it reads.
        raise HttpError(403, "only the runner's owner puts it on a credential set")
    return runner


def _refused(exc: credential_sets.SetRefused) -> HttpError:
    return HttpError(exc.status, str(exc))


@router.get("/", response=list[RunnerCredentialSetOut], summary="Your runner credential sets")
@gate("self")  # the sets the caller owns
def list_runner_credential_sets(request: HttpRequest):
    """Every credential set you own: the runners reading it, and the vault each
    workspace tree's runners read its values from, with the entries that vault has no
    value for yet."""
    return [credential_sets.summary(s) for s in credential_sets.owned_by(request.user)]


@router.post("/", response={201: RunnerCredentialSetOut},
             summary="Share a runner's credential list as a set other runners can read")
# the runner's owner; its vault becomes the set's for its tree
@gate("runner-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def create_runner_credential_set(request: HttpRequest, payload: RunnerCredentialSetIn):
    """The runner's own tokens and API keys become a set named `name`, in the same
    order, and its vault the set's vault for its workspace tree — nothing it reads
    changes. Your other cloud runners then read the same list (`PUT …/runners/{id}`);
    one in another workspace tree gets that tree's own vault, whose values someone
    seals there. 409 while the list holds a full login, which cannot be shared."""
    runner = _own_runner(request, payload.runner)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        cset = credential_sets.share(runner, name=payload.name, by=request.user)
    except credential_sets.SetRefused as exc:
        raise _refused(exc) from exc
    except ZkError as exc:
        raise zk_problem(exc) from exc
    events_audit.note_change(scope=f"runner:{runner.name}:credential-set", before={"credential_set": None},
                             after={"credential_set": cset.name}, workspace=runner.workspace)
    return Status(201, credential_sets.summary(cset))


@router.put("/runners/{runner_id}", response=RunnerCredentialStatusOut,
            summary="Choose which credential set a runner reads")
# the runner's owner, and the set's; attaching a vault — the same act as making a runner's own
@gate("runner-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def set_runner_credential_set(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCredentialSetChoiceIn):
    """`credential_set`: one of your sets — the runner reads its list, and its values
    from the set's vault for the runner's workspace tree (made now if it is the set's
    first runner there: make its key and seal the values on this runner's Credentials
    tab). `null`: the runner reads its own list again, and the set's vault stops
    serving it. 409 for a fleet member, or a runner with credentials of its own;
    422 `vault_crosses_isolation` when the tree's vault serves runners of another
    isolation boundary."""
    runner = _own_runner(request, runner_id)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    before = runner.credential_set.name if runner.credential_set_id else None
    try:
        if payload.credential_set is None:
            credential_sets.leave(runner, by=request.user)
        else:
            cset = RunnerCredentialSet.objects.filter(pk=payload.credential_set, owner=request.user).first()
            if cset is None:  # someone else's is "not found"
                raise HttpError(404, "credential set not found")
            credential_sets.join(runner, cset, by=request.user)
    except credential_sets.SetRefused as exc:
        raise _refused(exc) from exc
    except ZkError as exc:
        raise zk_problem(exc) from exc
    runner.refresh_from_db()
    after = runner.credential_set.name if runner.credential_set_id else None
    events_audit.note_change(scope=f"runner:{runner.name}:credential-set", before={"credential_set": before},
                             after={"credential_set": after}, workspace=runner.workspace)
    return harness_runners.runner_credential_status(runner)
