# shellcheck shell=bash disable=SC2034  # sourced; the constants are read by the sourcer
# runner/ec2/_lib.sh — what up.sh, wire.sh and down.sh must agree on, in ONE place.
# Sourced, never executed. Pure functions + constants: no AWS or network calls.
#
# The runner NAME is the identity everything else derives from:
#   - Runner.name in canopy-web (what wire.sh discovers and down.sh retires),
#   - the CloudFormation stack (one box per stack),
#   - the per-agent bootstrap readiness rows (keyed by runner name).
# Two boxes must never share a name unless one is replacing the other.

#: The live box. No-argument up.sh / wire.sh / down.sh mean exactly this, as they
#: always have — changing either default would point a bare command at a
#: different stack than the one in production.
DEFAULT_RUNNER_NAME="cloud-ec2-1"
DEFAULT_STACK="canopy-cloud-runner"

#: The first-boot seed (shared by every runner stack, like all canopy/cloud-runner/*
#: secrets — see down.sh --purge-secrets) of cloud_runner.py: a JSON manifest plus N parts named
#: "<manifest>-part-<i>". Chunked because Secrets Manager caps a value at 65536
#: bytes and cloud_runner.py stopped fitting in one (2026-10-04: 91 KB gz+b64).
SEED_SECRET="canopy/cloud-runner/runner-seed"
SEED_PART_PREFIX="canopy/cloud-runner/runner-seed-part-"

# runner_name_ok <name> — a name usable as a Runner.name AND inside a stack name
# (CloudFormation: [A-Za-z][-A-Za-z0-9]*, <=128 chars).
runner_name_ok() {
  [[ "$1" =~ ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

# stack_for_name <runner-name> — the default stack for a runner name.
#   cloud-ec2-1    -> canopy-cloud-runner          (the live box, unchanged)
#   cloud-ec2-test -> canopy-cloud-runner-test     (a leading "cloud-ec2-" is dropped)
#   standby        -> canopy-cloud-runner-standby
stack_for_name() {
  local name="$1"
  if [[ "$name" == "$DEFAULT_RUNNER_NAME" ]]; then
    printf '%s\n' "$DEFAULT_STACK"
    return
  fi
  printf '%s-%s\n' "$DEFAULT_STACK" "${name#cloud-ec2-}"
}
