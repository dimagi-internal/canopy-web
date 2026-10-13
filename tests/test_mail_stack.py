"""The AWS side's invariants, checked on the template so a hand-edit cannot drop one."""
from __future__ import annotations

from pathlib import Path

import yaml

STACK = Path(__file__).resolve().parent.parent / "deploy" / "aws" / "canopy-mail.cfn.yaml"


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: None)


def _res():
    return yaml.load(STACK.read_text(), Loader=_L)["Resources"]


def test_incoming_expires_after_7_days_and_kept_does_not():
    rules = _res()["InboundBucket"]["Properties"]["LifecycleConfiguration"]["Rules"]
    incoming = [r for r in rules if r["Prefix"] == "incoming/"]
    assert incoming and incoming[0]["ExpirationInDays"] == 7
    assert not [r for r in rules if r["Prefix"] == "kept/"]


def test_receipt_rule_scans_requires_tls_and_has_no_recipient_filter():
    rule = _res()["ReceiptRule"]["Properties"]["Rule"]
    assert rule["ScanEnabled"] is True and rule["TlsPolicy"] == "Require"
    assert "Recipients" not in rule


def test_tenant_sends_cannot_use_dimagi_addresses():
    doc = _res()["MailPolicy"]["Properties"]["PolicyDocument"]
    send = next(s for s in doc["Statement"] if "ses:SendRawEmail" in s["Action"])
    assert "StringNotLike" in send["Condition"]
    assert "*@dimagi.com" in send["Condition"]["StringNotLike"]["ses:FromAddress"]
