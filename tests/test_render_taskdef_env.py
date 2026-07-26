"""The deploy renders the container's env FROM the CloudFormation template, and
silently drops any entry whose value is a CFN intrinsic (`!Ref`, `!Sub`, …)
because those cannot be resolved outside CloudFormation.

That silence is the hazard: the deploy reports success and the setting is simply
absent at runtime. It has bitten twice — AUTH_ALLOWED_EMAIL_DOMAIN unset in prod
while the template said otherwise, and (caught pre-merge)
CANOPY_ATTACHMENTS_BUCKET written as `!Ref AttachmentsBucket`, which would have
left every upload 503-ing against a bucket the app could not name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "deploy" / "aws" / "canopy-web.cfn.yaml"
sys.path.insert(0, str(REPO / "deploy" / "aws"))

from render_taskdef import REQUIRED_ENV, template_plain_env  # noqa: E402


@pytest.fixture(scope="module")
def plain_env() -> dict[str, str]:
    return {e["name"]: e["value"] for e in template_plain_env(str(TEMPLATE))}


def test_every_required_env_var_survives_rendering(plain_env):
    """REQUIRED_ENV is the fail-loud list. If a key is there but renders away,
    the guard only fires at deploy time, in CI, on main."""
    missing = REQUIRED_ENV - set(plain_env)
    assert not missing, f"dropped from the rendered task def (intrinsic value?): {sorted(missing)}"


def test_the_attachments_bucket_is_a_plain_string(plain_env):
    value = plain_env.get("CANOPY_ATTACHMENTS_BUCKET")
    assert isinstance(value, str) and value, "must be a literal, never !Ref/!Sub"


def test_the_env_value_matches_the_bucket_the_stack_creates(plain_env):
    """Two copies of one name — the resource and the env var — because neither
    can be an intrinsic. Nothing but this test keeps them honest: drift means the
    app politely reads and writes a bucket that does not exist.
    """
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: {"__intrinsic__": True})
    doc = yaml.load(TEMPLATE.read_text(), Loader=_Loader)

    declared = doc["Resources"]["AttachmentsBucket"]["Properties"]["BucketName"]
    assert isinstance(declared, str), "BucketName must be a literal too"
    assert declared == plain_env["CANOPY_ATTACHMENTS_BUCKET"]


def test_the_bucket_is_not_publicly_exposed():
    """Every public-access door shut, ACLs disabled outright, and no Allow
    statement granting a wildcard principal."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: {"__intrinsic__": True})
    doc = yaml.load(TEMPLATE.read_text(), Loader=_Loader)
    bucket = doc["Resources"]["AttachmentsBucket"]["Properties"]

    assert all(bucket["PublicAccessBlockConfiguration"].values()), "all four blocks must be on"
    # PublicAccessBlock alone does NOT disable ACLs — it only stops them granting
    # PUBLIC access. BucketOwnerEnforced removes the mechanism entirely.
    assert bucket["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"
    assert bucket["BucketEncryption"], "encryption at rest must be configured"

    statements = doc["Resources"]["AttachmentsBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert allows, "the task role must still be able to read and write"
    for s in allows:
        principal = s["Principal"]
        assert principal != "*", "an Allow to everyone would make the bucket public"
        assert isinstance(principal, dict) and "AWS" in principal

    # A Deny with Principal "*" is not a public grant, and is what forces TLS.
    denies = [s for s in statements if s["Effect"] == "Deny"]
    assert any(
        "SecureTransport" in str(s.get("Condition", {})) for s in denies
    ), "plaintext HTTP must be denied"
