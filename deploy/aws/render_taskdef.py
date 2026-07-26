#!/usr/bin/env python3
"""Render the ECS task definition the deploy will register.

Single source of truth for the container's NON-SECRET environment is the
committed CloudFormation template (deploy/aws/canopy-web.cfn.yaml). This script
takes the live, CFN-provisioned task definition, swaps the image, and
*re-asserts* the template's plain-string `Environment` block over it — so env
can never silently drift from the template again (the bug that let
AUTH_ALLOWED_EMAIL_DOMAIN go unset in prod while the template "said" it was
allowed).

What comes from where:
  - environment (plain string values) — AUTHORITATIVE from the template. An
    entry deleted from the template is deleted from the running def; an entry
    changed in the template is changed on the next deploy. No carry-forward.
  - environment (CloudFormation intrinsics, e.g. AWS_REGION=!Ref AWS::Region,
    PORT=!Ref ContainerPort) — carried forward from the live def, since their
    values only exist after CloudFormation resolves them. Listed in CARRY_ENV.
  - secrets / execution+task roles / cpu / memory / log config — carried forward
    from the live def. These are CFN-resolved ARNs and rarely-changed resource
    settings; they stay owned by the stack, updated via a stack update.
  - image — swapped to the freshly built tag.

Fail-loud: if template extraction yields an unexpectedly small or incomplete
env, the script exits non-zero rather than registering a task def that would
crash-loop (a worse failure than the drift it replaces).
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml


# Env vars whose template value is a CloudFormation intrinsic (!Ref/!Sub/…),
# so they can't be read as plain strings here — carry them forward from the
# live task def instead. Keep this list minimal and explicit.
CARRY_ENV = {"AWS_REGION", "PORT"}

# Sanity floor + must-haves for the fail-loud guard.
MIN_PLAIN_ENV = 6
REQUIRED_ENV = {
    "DJANGO_SETTINGS_MODULE",
    "AUTH_ALLOWED_EMAIL_DOMAIN",
    # Added after nearly shipping it as `!Ref AttachmentsBucket`, which this
    # script drops silently (intrinsics are not str) — the deploy would have
    # reported success while every upload 503'd. Listing it here turns that
    # whole class of mistake into a failed deploy instead of a quiet no-op.
    "CANOPY_ATTACHMENTS_BUCKET",
}

# Read-only fields register-task-definition rejects.
READONLY_FIELDS = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
)


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's custom !Ref/!Sub/… tags.

    Any tagged scalar/sequence/mapping becomes a sentinel dict, so a plain
    string env value stays `str` and an intrinsic one does not — that's exactly
    the distinction we filter on.
    """


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: {"__cfn_intrinsic__": True})


def template_plain_env(cfn_path: str) -> list[dict]:
    """Return the template's plain-string Environment entries as [{name,value}]."""
    with open(cfn_path) as fh:
        cfn = yaml.load(fh, Loader=_CfnLoader)
    env = cfn["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]["Environment"]
    return [{"name": e["Name"], "value": e["Value"]} for e in env if isinstance(e["Value"], str)]


def render(live: dict, cfn_path: str, image: str, container: str) -> dict:
    plain = template_plain_env(cfn_path)
    names = {p["name"] for p in plain}

    missing = REQUIRED_ENV - names
    if missing:
        sys.exit(f"render_taskdef: template env missing required keys: {sorted(missing)}")
    if len(plain) < MIN_PLAIN_ENV:
        sys.exit(f"render_taskdef: only {len(plain)} plain env entries (< {MIN_PLAIN_ENV}) — refusing")

    try:
        cdef = next(c for c in live["containerDefinitions"] if c["name"] == container)
    except StopIteration:
        sys.exit(f"render_taskdef: container '{container}' not found in live task def")

    carried = [e for e in cdef.get("environment", []) if e["name"] in CARRY_ENV]
    missing_carry = CARRY_ENV - {e["name"] for e in carried}
    if missing_carry:
        sys.exit(f"render_taskdef: expected carry-forward env absent from live def: {sorted(missing_carry)}")

    cdef["environment"] = plain + carried
    cdef["image"] = image

    for field in READONLY_FIELDS:
        live.pop(field, None)
    return live


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", required=True, help="live task def JSON (aws ecs describe-task-definition)")
    ap.add_argument("--template", required=True, help="CloudFormation template (source of truth for env)")
    ap.add_argument("--image", required=True, help="image URI to run")
    ap.add_argument("--container", required=True, help="container name to patch")
    ap.add_argument("--out", required=True, help="where to write the rendered task def JSON")
    args = ap.parse_args()

    with open(args.live) as fh:
        live = json.load(fh)
    rendered = render(live, args.template, args.image, args.container)
    with open(args.out, "w") as fh:
        json.dump(rendered, fh)

    env_names = [e["name"] for e in rendered["containerDefinitions"][0]["environment"] if e["name"] not in ()]
    print(f"render_taskdef: rendered {len(env_names)} env vars, image={args.image}", file=sys.stderr)


if __name__ == "__main__":
    main()
