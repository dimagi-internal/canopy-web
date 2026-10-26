"""A machine that runs an agent: an approved device key with an approved ECDH key."""
from __future__ import annotations

from canopy_sdk.keys import header_jwk
from cryptography.hazmat.primitives.asymmetric import ec
from django.utils import timezone

from apps.harness import device_keys
from apps.harness.models import RunnerDeviceKey
from tests.fixtures.vault_zk import ref


def a_machine(runner, approver) -> tuple[RunnerDeviceKey, ec.EllipticCurvePrivateKey, ec.EllipticCurvePrivateKey]:
    """An approved device key with an approved ECDH key on `runner`: `(key, signing, ecdh)`."""
    sign, ecdh = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    now = timezone.now()
    key = RunnerDeviceKey.objects.create(
        runner=runner, public_jwk=header_jwk(sign), jkt=device_keys.thumbprint(header_jwk(sign)),
        kind=RunnerDeviceKey.SOFTWARE, status=RunnerDeviceKey.ACTIVE, approved_by=approver, approved_at=now,
        ecdh_public_jwk=ref.jwk(ecdh.public_key()), ecdh_jkt=ref.jkt(ecdh.public_key()), ecdh_approved_at=now)
    return key, sign, ecdh
