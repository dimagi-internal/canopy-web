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


def test_bucket_policy_names_the_same_rule_set_and_rule():
    res = _res()
    assert res["RuleSet"]["Properties"]["RuleSetName"] == "canopy-inbound"
    assert res["ReceiptRule"]["Properties"]["Rule"]["Name"] == "canopy-all"
    # !Sub is dropped by the tag-tolerant loader, so read the raw text.
    assert "receipt-rule-set/canopy-inbound:receipt-rule/canopy-all" in STACK.read_text()


def test_receipt_rule_waits_for_both_policies():
    assert set(_res()["ReceiptRule"]["DependsOn"]) >= {"InboundBucketPolicy", "InboundTopicPolicy"}


def test_events_topic_policy_precedes_the_event_destination():
    res = _res()
    assert res["EventsTopicPolicy"]["Type"] == "AWS::SNS::TopicPolicy"
    deps = res["TenantConfigSetEvents"]["DependsOn"]
    assert "EventsTopicPolicy" in ([deps] if isinstance(deps, str) else deps)


def test_no_iam_condition_uses_the_configuration_set_key():
    # Not a documented SES condition key: an Allow keyed on it would never match.
    for res in _res().values():
        doc = res["Properties"].get("PolicyDocument", {}) if isinstance(res.get("Properties"), dict) else {}
        for st in doc.get("Statement", []):
            for cond in (st.get("Condition") or {}).values():
                assert "ses:ConfigurationSet" not in cond


def test_canopy_web_mail_is_off_by_default():
    web = STACK.parent / "canopy-web.cfn.yaml"
    params = yaml.load(web.read_text(), Loader=_L)["Parameters"]
    assert params["MailEnabled"]["Default"] == "false"
    assert "Description" in params["DesiredCount"]
