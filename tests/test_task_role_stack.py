"""canopy-web's own task role (deploy/aws/canopy-web-task-role.cfn.yaml) and the
stacks that attach to it. Checked on the templates, because none of this is
deployed by the pipeline — a hand-edit is the only way it changes."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

AWS = Path(__file__).resolve().parent.parent / "deploy" / "aws"
ROLE = "canopy-web-task-role"


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: {
    suffix: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
    else loader.construct_sequence(node, deep=True) if isinstance(node, yaml.SequenceNode)
    else loader.construct_mapping(node, deep=True)})


def _load(name: str) -> dict:
    return yaml.load((AWS / name).read_text(), Loader=_L)


def _statements(role_props: dict) -> dict:
    return {s["Sid"]: s for p in role_props["Policies"] for s in p["PolicyDocument"]["Statement"]}


def test_the_role_is_named_and_only_ecs_tasks_in_this_account_assume_it():
    t = _load("canopy-web-task-role.cfn.yaml")
    assert t["Parameters"]["RoleName"]["Default"] == ROLE
    props = t["Resources"]["TaskRole"]["Properties"]
    assert props["RoleName"] == {"Ref": "RoleName"}
    (trust,) = props["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}


def test_the_role_carries_ecs_exec_and_the_attachments_objects_only():
    t = _load("canopy-web-task-role.cfn.yaml")
    st = _statements(t["Resources"]["TaskRole"]["Properties"])
    assert set(st) == {"EcsExec", "AttachmentsObjects"}
    assert all(a.startswith("ssmmessages:") for a in st["EcsExec"]["Action"])
    assert set(st["AttachmentsObjects"]["Action"]) == {"s3:PutObject", "s3:GetObject", "s3:DeleteObject"}


def test_attachments_bucket_is_the_one_canopy_web_writes():
    role = _load("canopy-web-task-role.cfn.yaml")
    web = _load("canopy-web.cfn.yaml")
    bucket = web["Resources"]["AttachmentsBucket"]["Properties"]["BucketName"]
    assert role["Parameters"]["AttachmentsBucketName"]["Default"] == bucket
    # `"Name" in e` skips a conditional (`!If`) entry such as VAULT_KMS_KEY_ID.
    env = {e["Name"]: e["Value"] for e in
           web["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
           if "Name" in e}
    assert env["CANOPY_ATTACHMENTS_BUCKET"] == bucket


def test_ses_and_mail_policies_can_attach_to_both_roles_during_the_cutover():
    # A list, so the grants can sit on the shared role AND canopy-web-task-role
    # while the task moves; a String would force a gap in mail.
    email = _load("canopy-email.cfn.yaml")
    assert email["Parameters"]["TaskRoleName"]["Type"] == "CommaDelimitedList"
    assert email["Resources"]["SendEmailPolicy"]["Properties"]["Roles"] == {"Ref": "TaskRoleName"}
    mail = _load("canopy-mail.cfn.yaml")
    assert mail["Parameters"]["TaskRoleName"]["Type"] == "CommaDelimitedList"
    assert mail["Resources"]["MailPolicy"]["Properties"]["Roles"] == {"Ref": "TaskRoleName"}


def test_the_canopy_web_deploy_role_may_pass_the_new_role():
    spec = importlib.util.spec_from_file_location("deploy_roles", AWS / "deploy_roles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # no side effects: main() runs only as a script
    pol = mod.policy_for("canopy-web", mod.APPS["canopy-web"])
    passable = [r for s in pol["Statement"] if s["Action"] == "iam:PassRole" for r in s["Resource"]]
    assert f"arn:aws:iam::{mod.ACCT}:role/{ROLE}" in passable
    # ...and NOT the shared task role any more: the cutover is done (2026-10-15),
    # and a pipeline that could pass it could move canopy-web back onto it.
    assert f"arn:aws:iam::{mod.ACCT}:role/labs-jj-ecs-task-role" not in passable
    assert f"arn:aws:iam::{mod.ACCT}:role/labs-jj-ecs-task-execution-role" in passable
    # The other apps still run as the shared task role and must keep passing it.
    for app in ("ace-web", "connect-labs"):
        other = mod.policy_for(app, mod.APPS[app])
        other_pass = [r for s in other["Statement"] if s["Action"] == "iam:PassRole" for r in s["Resource"]]
        assert f"arn:aws:iam::{mod.ACCT}:role/labs-jj-ecs-task-role" in other_pass, app
    # No other app may pass canopy-web's role.
    for app in ("ace-web", "connect-labs"):
        other = mod.policy_for(app, mod.APPS[app])
        assert ROLE not in str(other), app


def test_the_canopy_web_deploy_role_may_log_in_to_public_ecr():
    # deploy-labs.yml logs in to public ECR so the Dockerfile's public.ecr.aws
    # base images pull authenticated (anonymous pulls hit 429 Data limit
    # exceeded). The login needs both actions; neither takes a resource.
    spec = importlib.util.spec_from_file_location("deploy_roles", AWS / "deploy_roles.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    pol = mod.policy_for("canopy-web", mod.APPS["canopy-web"])
    stmt = {s["Sid"]: s for s in pol["Statement"]}["PublicEcrLoginTakesNoResource"]
    assert set(stmt["Action"]) == {"ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"}
    assert stmt["Resource"] == "*"
    # Read-only login: nothing that could push to a public repository.
    assert not any(a.startswith("ecr-public:") and a != "ecr-public:GetAuthorizationToken"
                   for s in pol["Statement"] for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]))
    workflow = (AWS.parent.parent / ".github/workflows/deploy-labs.yml").read_text()
    assert "registry-type: public" in workflow
