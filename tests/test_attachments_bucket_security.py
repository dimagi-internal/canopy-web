"""The attachments bucket must stay private.

This file previously also guarded `render_taskdef.py`, which built the container
env by reading the CloudFormation template OUTSIDE CloudFormation and silently
dropped any value that was an intrinsic. CloudFormation now registers the task
definition itself and resolves intrinsics natively, so that failure mode is gone
along with the script. These assertions are not — "nobody quietly opens this
bucket later" is a permanent requirement, and it holds user-uploaded content.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "deploy" / "aws" / "canopy-web.cfn.yaml"


class _Loader(yaml.SafeLoader):
    """Tolerates CloudFormation's !Ref/!Sub/!GetAtt tags."""


_Loader.add_multi_constructor("!", lambda loader, suffix, node: {"__intrinsic__": True})


@pytest.fixture(scope="module")
def doc():
    return yaml.load(TEMPLATE.read_text(), Loader=_Loader)


def test_every_public_access_door_is_shut(doc):
    bucket = doc["Resources"]["AttachmentsBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values()), "all four blocks must be on"


def test_acls_are_disabled_outright(doc):
    """PublicAccessBlock alone does NOT disable ACLs — it only stops them
    granting PUBLIC access, so per-object ACLs still function and can still grant
    to other principals. BucketOwnerEnforced removes the mechanism, leaving the
    bucket policy as the single auditable authority."""
    bucket = doc["Resources"]["AttachmentsBucket"]["Properties"]
    assert bucket["OwnershipControls"]["Rules"][0]["ObjectOwnership"] == "BucketOwnerEnforced"


def test_encryption_at_rest_is_configured(doc):
    assert doc["Resources"]["AttachmentsBucket"]["Properties"]["BucketEncryption"]


def test_the_bucket_survives_a_stack_teardown(doc):
    """It holds user data. CloudFormation cannot delete a non-empty bucket
    anyway, so without Retain a teardown fails noisily instead of cleanly."""
    bucket = doc["Resources"]["AttachmentsBucket"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert bucket["UpdateReplacePolicy"] == "Retain"


def _statements(doc):
    return doc["Resources"]["AttachmentsBucketPolicy"]["Properties"]["PolicyDocument"]["Statement"]


def test_no_allow_grants_a_wildcard_principal(doc):
    allows = [s for s in _statements(doc) if s["Effect"] == "Allow"]
    assert allows, "the task role must still be able to read and write"
    for s in allows:
        principal = s["Principal"]
        assert principal != "*", "an Allow to everyone would make the bucket public"
        assert isinstance(principal, dict) and "AWS" in principal


def test_plaintext_http_is_denied(doc):
    """A Deny with Principal "*" is not a public grant (only Allow is), which is
    why BlockPublicPolicy accepts it."""
    denies = [s for s in _statements(doc) if s["Effect"] == "Deny"]
    assert any("SecureTransport" in str(s.get("Condition", {})) for s in denies)
