"""`POST /api/harness/runners/box-egress` — root on a cloud box reads the egress lock
people set for its runners (`apps/harness/egress.py`).

Anonymous at the door (`auth=None`, public in `apps/common/middleware.py`): root
holds no canopy token — its units do, and a unit must not be the one relaying its
own lock, or a unit could loosen it. Root proves the box's instance role instead
(IMDS is closed to units), and gets the settings of every live runner on that
machine (`Runner.machine_id`, the canonical role). One uniform 401 for any bad
proof; nothing about which runners exist leaks to a caller without the role.
"""
from __future__ import annotations

import logging

from django.http import HttpRequest
from ninja import Router

from apps.api.errors import TYPE_AUTH, ProblemError
from apps.api.route_gates import gate

from . import aws_iam_proof, egress
from .models import Runner
from .schemas import BoxEgressIn, BoxEgressOut, BoxEgressRunnerOut

logger = logging.getLogger(__name__)

box_egress_router = Router(auth=None, tags=["harness"])


@box_egress_router.post("", response=BoxEgressOut, auth=None,
                        summary="A cloud box reads its runners' egress lock (instance role, no token)")
# anonymous door: the box's instance-role proof is the identity; it reads only its
# own runners' settings, and the answer can only add to what the box enforces
@gate("device-proof", kind="runner-protocol")
def box_egress(request: HttpRequest, payload: BoxEgressIn):
    """Prove this box's EC2 instance role (`proof`, bound to this URL): every live
    runner on that machine with its egress lock — `locked` (a ZDR box always is) and
    `allow`, the hosts beyond the box's base set. Root applies it on top of the box's
    own setting, never below it. Any bad proof: one uniform 401."""
    try:
        role_arn = aws_iam_proof.verify(payload.proof, request)
    except aws_iam_proof.AwsProofError as exc:
        logger.info("box-egress refused: %s", exc)
        raise ProblemError(401, "Refused", type_=TYPE_AUTH,
                           detail="no valid instance-role proof for this URL") from exc
    machine = aws_iam_proof.canonical_role_arn(role_arn)
    runners = (Runner.objects.filter(machine_id=machine, kind=Runner.CLOUD)
               .exclude(status=Runner.RETIRED).prefetch_related("declared_flags").order_by("name"))
    out = []
    for r in runners:
        eff = egress.effective(r)
        out.append(BoxEgressRunnerOut(id=r.pk, name=r.name, locked=eff["locked"], allow=eff["allow"]))
    return BoxEgressOut(runners=out)
