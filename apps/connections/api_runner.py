"""A box's refresh lease and write-back, under `/api/harness/runners/{rid}/connections/{id}/`
(contract §0.6 route 12), and its keeper's connection work, re-seal and revoke
confirmation (routes 13-15, `keeper.py`).

Mounted at `/harness` (`apps/api/api.py`) so the box's runner token reaches them,
while the code stays here: harness and agents never import connections. Every
call needs the box's runner token AND a device proof by its own approved key,
and the box must serve an agent the connection is in force for now
(`tokens.admits_runner`: `recipients`, which asks `runner_may_hold_agent`) — not
merely be one it was once sealed to. The keeper routes instead need the box to
HOLD it (`keeper`): a revoking row is in force for nobody, yet its holders revoke it.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Status

from apps.api.route_gates import gate
from apps.harness.runner_queries import runner_or_404
from apps.harness.models import RunnerDeviceKey

from . import keeper, proofs, tokens
from .models import Connection
from .problems import problem_for_wire
from .schemas import (
    ConnectionKeeperOut,
    LeaseIn,
    LeaseOut,
    ReconnectRequiredIn,
    ResealIn,
    ResealOut,
    RevokedIn,
    WriteBackIn,
    WriteBackOut,
)
from .wire import WireError

router = Router(tags=["connections"])


def _box_connection(request: HttpRequest, runner_id: uuid.UUID,
                    connection_id: uuid.UUID) -> tuple[Connection, RunnerDeviceKey]:
    runner = runner_or_404(request, runner_id)
    try:
        key = proofs.proving_device(request, runner)
        conn = get_object_or_404(Connection.objects.select_related("app", "agent", "user"), pk=connection_id)
        tokens.admits_runner(conn, key)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return conn, key


@router.post("/runners/{runner_id}/connections/{connection_id}/refresh-lease", response=LeaseOut,
             summary="A box takes a connection's refresh lease")
@gate("runner", kind="runner-protocol", step_up="runner protocol: a box's own token and device proof")
def take_refresh_lease(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                       payload: LeaseIn) -> LeaseOut:
    """Take the one refresh lease (60 s; send `lease_id` to renew the one this box
    holds) before refreshing at the provider. 409 `lease_held` with `retry_after`
    while another machine (or another process here, without the id) holds it: wait
    for its new access token instead. Renew on `ttl_seconds`, not `expires_at`. 403
    `not_a_recipient` unless this box serves an agent the connection is lent to;
    401 `device_proof_required`; 409 `revoking`."""
    conn, key = _box_connection(request, runner_id, connection_id)
    try:
        return LeaseOut(**tokens.take_lease(conn, key, payload.lease_id))
    except WireError as exc:
        raise problem_for_wire(exc) from None


@router.delete("/runners/{runner_id}/connections/{connection_id}/refresh-lease/{lease_id}",
               response={204: None}, summary="A box gives up a connection's refresh lease")
@gate("runner", kind="runner-protocol", step_up="runner protocol: a box's own token and device proof")
def release_refresh_lease(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                          lease_id: uuid.UUID) -> Status:
    """Release the refresh lease this box holds; harmless when it holds none."""
    conn, key = _box_connection(request, runner_id, connection_id)
    tokens.release_lease(conn, key, lease_id)
    return Status(204, None)


@router.put("/runners/{runner_id}/connections/{connection_id}/sealed", response=WriteBackOut,
            summary="A box writes back a connection's refreshed envelopes")
@gate("runner", kind="runner-protocol", step_up="runner protocol: ciphertext only machines open")
def write_back_connection(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                          payload: WriteBackIn) -> WriteBackOut:
    """Store the access token this box just minted, sealed to exactly who may open
    the connection now, and the refresh token when it rotated, against the
    `sealed_digest` it read. 409 `lease_lost` (another machine took the lease),
    `zk_value_changed`, `revoking` (disconnected mid-refresh: nothing stored; stage
    nothing); 422 `foreign_reader`, `zk_rewrap_required`, `bad_writer_sig`. Stage the
    token only after this succeeds. `expect` is the digest of the refresh envelope
    as read (`sealed_digest`); the access envelope's writer MAC is bound to the SHA-256
    (hex) of the stored `access_sealed` instead (empty string when there is none)."""
    conn, key = _box_connection(request, runner_id, connection_id)
    try:
        digest = tokens.write_back(conn, device_key=key, **payload.model_dump())
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return WriteBackOut(sealed_digest=digest)


@router.post("/runners/{runner_id}/connections/{connection_id}/reconnect-required", response={204: None},
             summary="A box reports a connection's refresh refused")
@gate("runner", kind="runner-protocol", step_up="runner protocol: a box's own token and device proof")
def connection_reconnect_required(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                                  payload: ReconnectRequiredIn) -> Status:
    """The provider refused the refresh made under `lease_id`. `invalid_grant` marks
    the connection `needs_reconnect` (its owner connects again); any other error is
    only noted. 409 `lease_lost` unless this box holds the lease."""
    conn, key = _box_connection(request, runner_id, connection_id)
    try:
        tokens.mark_reconnect_required(conn, key, payload.lease_id, payload.error)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return Status(204, None)


# ---- the keeper (routes 13-15) ------------------------------------------------------

def _keeper_box(request: HttpRequest, runner_id: uuid.UUID):
    runner = runner_or_404(request, runner_id)
    try:
        return runner, proofs.proving_device(request, runner)
    except WireError as exc:
        raise problem_for_wire(exc) from None


@router.get("/runners/{runner_id}/vault-keeper/connections", response=ConnectionKeeperOut,
            summary="Connections this box holds that need its keeper")
@gate("runner", step_up="runner protocol: ciphertext only machines open")
def get_connection_keeper_work(request: HttpRequest, runner_id: uuid.UUID) -> ConnectionKeeperOut:
    """Each connection this cloud box holds that machines are waiting for (`waiting`:
    re-seal the same plaintext for those the signed statement allows, at
    `reseal_path`) or that is being disconnected (`revoking`: revoke it — at the
    provider only when `revoke_at_provider` — and confirm at `revoked_path`). Empty
    for a machine that is not a cloud box. 401 `device_proof_required`."""
    runner, _ = _keeper_box(request, runner_id)
    return ConnectionKeeperOut(connections=keeper.work(runner))


@router.put("/runners/{runner_id}/vault-keeper/connections/{connection_id}", response=ResealOut,
            summary="A keeper re-seals a connection it holds for machines that were waiting")
@gate("runner", setting=True, step_up="runner protocol: ciphertext only machines open")
def put_connection_reseal(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                          payload: ResealIn) -> ResealOut:
    """The refresh envelope (and the access one, when stored) re-sealed by this box
    to the owner and the machines the statement allows, against the `sealed_digest`
    it read. 403 `zk_not_a_holder` unless this cloud box held it and still does; 409
    `zk_value_changed`, `revoking`; 422 `foreign_reader`, `zk_rewrap_required`,
    `bad_writer_sig`."""
    runner, key = _keeper_box(request, runner_id)
    conn = get_object_or_404(Connection, pk=connection_id)
    try:
        return ResealOut(machines=keeper.reseal(conn, runner, key, **payload.model_dump()))
    except WireError as exc:
        raise problem_for_wire(exc) from None


@router.post("/runners/{runner_id}/connections/{connection_id}/revoked", response={204: None},
             summary="A holder confirms it revoked a disconnected connection")
@gate("runner", kind="runner-protocol", step_up="runner protocol: a box's own token and device proof")
def post_connection_revoked(request: HttpRequest, runner_id: uuid.UUID, connection_id: uuid.UUID,
                            payload: RevokedIn) -> Status:
    """This box revoked the disconnected connection (`revoked`), or tried and failed
    (`error`); either way the row and its ciphertext are deleted. 404 once another
    holder confirmed; 409 `not_revoking` unless it is being disconnected; 403
    `zk_not_a_holder` unless this cloud box holds it."""
    runner, key = _keeper_box(request, runner_id)
    conn = get_object_or_404(Connection, pk=connection_id)
    try:
        keeper.confirm_revoked(conn, runner, key, revoked=payload.revoked, error=payload.error)
    except WireError as exc:
        raise problem_for_wire(exc) from None
    return Status(204, None)
