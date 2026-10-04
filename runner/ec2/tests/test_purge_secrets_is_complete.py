"""`--purge-secrets` must actually purge every secret this stack owns.

Found by reading before destroying, ahead of a deliberate recycle: `down.sh
--purge-secrets` deleted five of the six secrets under `canopy/cloud-runner/`
and silently left `gog-keyring-password` behind — a secret `secrets.sh` manages,
`runner.cfn.yaml` grants the instance role read on, and which was live in
Secrets Manager (last changed 2026-07-26).

That is worse than a leak. The whole point of a purge-then-rebuild is to prove
the box can be reconstructed from nothing; a surviving secret means the rebuild
can pass BECAUSE of leftover state, which is the exact false green the exercise
exists to rule out.

The check derives the expected set from the other files rather than restating it,
so a secret added to the stack later cannot be forgotten here — restating it is
how the two lists drifted in the first place.
"""
from __future__ import annotations

import pathlib
import re

EC2 = pathlib.Path(__file__).resolve().parent.parent
PREFIX = "canopy/cloud-runner/"

# Where a secret name can legitimately be introduced. `down.sh` is excluded: it
# is the file under test, so including it would let the purge list vacuously
# satisfy itself.
SOURCES = ["secrets.sh", "up.sh", "wire.sh", "runner.cfn.yaml", "_lib.sh"]

#: The single-value seed format stacks created before 2026-10-04 still read
#: (canopy-fetch-env baked into their UserData). Nothing current declares them —
#: up.sh stopped writing them when cloud_runner.py outgrew Secrets Manager's 64 KB
#: value cap — but a purge must still remove them, or it leaves state behind.
LEGACY = {"runner-code", "runner-code-sha"}

#: A name ending in "-" is a FAMILY (e.g. the seed's "runner-seed-part-<i>"),
#: purged by prefix rather than by name.
NAME = re.compile(re.escape(PREFIX) + r"([a-z0-9-]+)")


def _names_in(path: pathlib.Path) -> set[str]:
    return set(NAME.findall(path.read_text()))


def _purged_by_down_sh() -> set[str]:
    """The names inside down.sh's `--purge-secrets` loops (by name, and by prefix
    for families), not the whole file."""
    text = (EC2 / "down.sh").read_text()
    loop = re.search(r"for s in (.+?);?\s*do", text, re.S)
    assert loop, "down.sh no longer has a `for s in ...; do` purge loop"
    names = set(NAME.findall(loop.group(1)))
    prefixes = re.search(r"for prefix in (.+?);?\s*do", text, re.S)
    assert prefixes, "down.sh no longer purges secret families by prefix"
    return names | set(NAME.findall(prefixes.group(1)))


def _owned() -> set[str]:
    owned: set[str] = set()
    for f in SOURCES:
        owned |= _names_in(EC2 / f)
    return owned | LEGACY


def test_purge_deletes_every_secret_the_stack_owns():
    missed = _owned() - _purged_by_down_sh()
    assert not missed, (
        f"--purge-secrets leaves {sorted(missed)} behind. A rebuild after a "
        f"'full' purge would inherit them, so it could pass on leftover state."
    )


def test_purge_does_not_delete_secrets_nothing_else_declares():
    """The converse: a name only down.sh knows about is a typo or a leftover —
    it would silently delete nothing, or something it should not own."""
    unknown = _purged_by_down_sh() - _owned()
    assert not unknown, f"down.sh purges {sorted(unknown)}, which nothing else declares"


def test_the_four_staged_secrets_are_all_purgeable():
    """Belt and braces on the specific set a human stages by hand — these are the
    ones whose loss is felt, and the ones a 'from scratch' claim rests on."""
    staged = _names_in(EC2 / "secrets.sh")
    assert staged == {
        "canopy-pat",
        "claude-oauth-token",
        "op-service-account-token",
        "gog-keyring-password",
    }, f"secrets.sh's managed set changed: {sorted(staged)}"
    assert staged <= _purged_by_down_sh()


def test_the_seed_and_its_parts_are_purged():
    """The new first-boot seed is a manifest + a family of parts; both must go."""
    purged = _purged_by_down_sh()
    assert {"runner-seed", "runner-seed-part-"} <= purged
