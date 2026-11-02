"""A box's refresh lease and write-back, under `/api/harness/runners/{rid}/connections/{id}/`
(contract §0.6 route 12).

Mounted at `/harness` (`apps/api/api.py`) so the box's runner token reaches them,
while the code stays here: harness and agents never import connections. Every
call needs the box's runner token AND a device proof by its own approved key,
and the box must serve an agent the connection is in force for now
(`tokens.admits_runner`: `recipients`, which asks `runner_may_hold_agent`) — not
merely be one it was once sealed to.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Status

from apps.api.route_gates import gate
from apps.harness.api import runner_or_404
from apps.harness.models import RunnerDeviceKey

from . import proofs, tokens
from .models import Connection
from .problems import problem_for_wire
from .schemas import LeaseIn, LeaseOut, ReconnectRequiredIn, WriteBackIn, WriteBackOut
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
    token only after this succeeds."""
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
