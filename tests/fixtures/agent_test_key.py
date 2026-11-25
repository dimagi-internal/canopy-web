"""An agent login's test vault key, as a harness's browser makes one (shared by the
test-key suites)."""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric import ec

from tests.fixtures.vault_zk import ref

PASSPHRASE = "a test passphrase for a harness"


def passphrase_keyset(user, *, prf=False):
    """What a harness's browser (or the CLI) makes: a key pair, its private scalar sealed
    with a passphrase + Secret Key (2SKD). Returns (body, private key)."""
    priv = ec.generate_private_key(ec.SECP256R1())
    scalar = priv.private_numbers().private_value.to_bytes(32, "big")
    seals = [ref.seal_user_key_2skd(scalar, str(user.pk), PASSPHRASE, os.urandom(16), os.urandom(16),
                                    os.urandom(12))]
    if prf:
        seals.append(ref.seal_user_key(scalar, str(user.pk), ref.b64u(b"cred"), os.urandom(32),
                                       os.urandom(16), os.urandom(12)))
    return {"public_jwk": ref.jwk(priv.public_key()), "seals": seals}, priv
