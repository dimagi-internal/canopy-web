#!/usr/bin/env bash
# Spin UP a canopy cloud runner via CloudFormation. Idempotent (create or update).
#
#   aws sso login --profile labs
#   ./up.sh                                  # the live box: stack canopy-cloud-runner, runner cloud-ec2-1
#   ./up.sh --name cloud-ec2-test --standby  # a second box: stack canopy-cloud-runner-test
#   ./wire.sh --name <same> [--standby --agents a,b] --drill   # then wire it
#   ./down.sh --name <same>                  # tear it down (stack + canopy-web runner row)
#
# Everything a human must have done ONCE is checked before anything is written —
# see preflight below; each failure names the one command that fixes it.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source ./_lib.sh

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")
BASE_URL="https://labs.connect.dimagi.com/canopy"
NAME="$DEFAULT_RUNNER_NAME"
STACK_ARG=""
STANDBY=0
VOLUME_SIZE=""
INSTANCE_TYPE=""
ALLOW_INSTANCE_CHANGE=0
ALLOW_DIRTY=0

usage() {
  cat <<'USAGE'
usage: ./up.sh [options]

  --name <runner-name>      Runner.name the box pairs as (default cloud-ec2-1, the live box)
  --stack <stack>           CloudFormation stack (default: derived from --name —
                            cloud-ec2-1 -> canopy-cloud-runner, cloud-ec2-X -> canopy-cloud-runner-X)
  --standby                 a box that claims NOTHING it is not explicitly given: no repo
                            (RunnerProjects='') and no chat-session turns (RunnerSessions=0).
                            Agent work reaches it only through assignment rows / drills.
  --volume-size <GiB>       root volume (template default 40)
  --instance-type <type>    EC2 type (template default t3.medium)
  --base-url <url>          canopy-web (default https://labs.connect.dimagi.com/canopy)
  --allow-instance-change   proceed when an UPDATE would stop/start or REPLACE the instance
                            (refused by default: on the live stack that is an outage)
  --allow-dirty             publish a seed from uncommitted runner code (its provenance stamp
                            will not describe the bytes; the box reads as stale until it updates)
  -h, --help                this

Other template parameters: EXTRA_PARAMS='Key=Val Key=Val' ./up.sh ...
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --stack) STACK_ARG="$2"; shift 2 ;;
    --standby) STANDBY=1; shift ;;
    --volume-size) VOLUME_SIZE="$2"; shift 2 ;;
    --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --allow-instance-change) ALLOW_INSTANCE_CHANGE=1; shift ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

runner_name_ok "$NAME" || { echo "!! invalid runner name '$NAME' (lowercase letters, digits, '-')" >&2; exit 1; }
# STACK env is honored for backwards compatibility; --stack wins over it.
STACK="${STACK_ARG:-${STACK:-$(stack_for_name "$NAME")}}"
echo ">> runner '$NAME' -> stack '$STACK'"

die() { echo "!! $*" >&2; exit 1; }

# ── preflight: every one-time human step, checked before anything is written ────
for tool in aws python3 git curl; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not on PATH"
done

echo ">> account"
"${AWS[@]}" sts get-caller-identity --query Account --output text \
  || die "AWS credentials for profile '$PROFILE' are not valid — run: aws sso login --profile $PROFILE"

# The secrets every box reads at boot. They are SHARED by every runner stack —
# a second box reuses the live box's — so a missing one is a one-time human step.
for pair in "canopy/cloud-runner/canopy-pat|./secrets.sh canopy <file>   (a canopy-web PAT for the user the runner pairs as)" \
            "canopy/cloud-runner/claude-oauth-token|./secrets.sh claude <file>   (\`claude setup-token\` as the subscription account — a HUMAN step)" \
            "canopy/cloud-runner/gog-keyring-password|./secrets.sh gog <file>   (any strong password; without it no agent has Gmail)"; do
  s="${pair%%|*}"; fix="${pair#*|}"
  "${AWS[@]}" secretsmanager describe-secret --secret-id "$s" >/dev/null 2>&1 \
    || die "missing secret '$s' — stage it: $fix"
done

# The PAT must still authenticate, or the box pairs with nothing and sits there.
PAT_CODE=$("${AWS[@]}" secretsmanager get-secret-value --secret-id canopy/cloud-runner/canopy-pat \
  --query SecretString --output text | python3 -c "
import sys, urllib.request, urllib.error
req = urllib.request.Request('$BASE_URL/api/harness/runners/',
                             headers={'Authorization': 'Bearer ' + sys.stdin.read().strip()})
try:
    print(urllib.request.urlopen(req, timeout=20).status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
")
[[ "$PAT_CODE" == "200" ]] \
  || die "canopy/cloud-runner/canopy-pat does not authenticate against $BASE_URL (HTTP $PAT_CODE) — mint a new PAT and: ./secrets.sh canopy <file>"

[[ -f "$HOME/.claude/canopy/workbench-token" ]] \
  || echo "   WARN: no ~/.claude/canopy/workbench-token — wire.sh and down.sh need it (/canopy:canopy-web-pat-mint)"

# Provenance of the bytes about to be published: the SAME quantity
# deploy-labs.yml computes for the server's `expected_code_sha`, over the same
# paths (tests/test_seed_roundtrip.py pins the lists equal). update_runner.sh was
# missing here until 2026-10-04, so a seed published after an updater-only change
# was stamped with an older sha than the deploy expected.
SHA_PATHS=(cloud_runner.py bootstrap_agents.sh update_runner.sh)
CODE_SHA="$(git log -1 --format=%H -- "${SHA_PATHS[@]}" 2>/dev/null || true)"
CODE_AT="$(git log -1 --format=%ct -- "${SHA_PATHS[@]}" 2>/dev/null || true)"
[[ -n "$CODE_SHA" ]] || die "cannot resolve the runner source sha (shallow clone?). A box seeded without it reports unknown provenance and NEVER auto-updates — fetch full history first."
if [[ -n "$(git status --porcelain -- "${SHA_PATHS[@]}" 2>/dev/null)" && "$ALLOW_DIRTY" != "1" ]]; then
  die "uncommitted changes to ${SHA_PATHS[*]} — the seed would not be the code its stamp names. Commit them, or pass --allow-dirty."
fi

STACK_STATUS=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NONE")
case "$STACK_STATUS" in
  ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
    die "stack $STACK is $STACK_STATUS and cannot be updated — ./down.sh --stack $STACK --keep-runner, then re-run" ;;
  *_IN_PROGRESS)
    die "stack $STACK is $STACK_STATUS — wait for it to settle" ;;
esac
echo ">> stack status: $STACK_STATUS"

MYIP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
echo ">> SSH allowed from ${MYIP}/32"

# ── publish the first-boot seed (manifest + parts) ──────────────────────────────
# Secrets Manager caps a value at 65536 bytes and cloud_runner.py outgrew one
# gzip+base64 secret on 2026-10-04 (91 KB) — every up.sh died on PutSecretValue.
# See seed.py. Parts first, manifest last; the box verifies the sha256 either way.
# The legacy single-value canopy/cloud-runner/runner-code is deliberately NOT
# touched: stacks created before this format still read it.
put_secret() {  # <id> <file> <description>
  if "${AWS[@]}" secretsmanager describe-secret --secret-id "$1" >/dev/null 2>&1; then
    "${AWS[@]}" secretsmanager put-secret-value --secret-id "$1" \
      --secret-string "file://$2" >/dev/null
  else
    "${AWS[@]}" secretsmanager create-secret --name "$1" --description "$3" \
      --secret-string "file://$2" >/dev/null
  fi
}
SEED_DIR="$(mktemp -d)"
trap 'rm -rf "$SEED_DIR"' EXIT
PARTS=$(python3 seed.py build cloud_runner.py "$SEED_DIR" --sha "$CODE_SHA" --committed-at "${CODE_AT:-0}")
echo ">> publishing cloud_runner.py seed ${CODE_SHA:0:12} -> $SEED_SECRET ($PARTS part(s))"
for (( i = 0; i < PARTS; i++ )); do
  put_secret "${SEED_PART_PREFIX}$i" "$SEED_DIR/part-$i" "cloud_runner.py seed part $i — published by up.sh"
done
put_secret "$SEED_SECRET" "$SEED_DIR/manifest.json" "cloud_runner.py seed manifest — published by up.sh"
# Parts past the new count belong to an older, larger publish: remove them so the
# set in Secrets Manager is exactly what the manifest names.
"${AWS[@]}" secretsmanager list-secrets --filters "Key=name,Values=$SEED_PART_PREFIX" \
  --query 'SecretList[].Name' --output text | tr '\t' '\n' | while read -r old; do
  [[ -n "$old" ]] || continue
  idx="${old#"$SEED_PART_PREFIX"}"
  if [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= PARTS )); then
    echo "   removing stale $old"
    "${AWS[@]}" secretsmanager delete-secret --secret-id "$old" --force-delete-without-recovery >/dev/null
  fi
done

# ── deploy ───────────────────────────────────────────────────────────────────────
echo ">> validating template"
"${AWS[@]}" cloudformation validate-template --template-body "file://runner.cfn.yaml" >/dev/null

PARAMS=("SshCidr=${MYIP}/32" "RunnerName=${NAME}")
[[ -n "$VOLUME_SIZE" ]] && PARAMS+=("VolumeSize=${VOLUME_SIZE}")
[[ -n "$INSTANCE_TYPE" ]] && PARAMS+=("InstanceType=${INSTANCE_TYPE}")
if [[ "$STANDBY" == "1" ]]; then
  PARAMS+=("RunnerProjects=" "RunnerSessions=0")
fi
# shellcheck disable=SC2206 # EXTRA_PARAMS is documented as space-separated Key=Val
[[ -n "${EXTRA_PARAMS:-}" ]] && PARAMS+=(${EXTRA_PARAMS})
echo ">> parameters: ${PARAMS[*]}"

if [[ "$STACK_STATUS" == "NONE" ]]; then
  echo ">> creating stack $STACK"
  "${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK" --template-file runner.cfn.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "${PARAMS[@]}"
else
  # An UPDATE goes through a reviewed change set. CloudFormation applies a
  # UserData change as a stop/start and a BlockDeviceMappings change as a
  # REPLACEMENT — either is an outage on the live box, and both happen silently
  # from a plain `deploy` (any template change since the stack was created does
  # it). So look first, and require the operator to say so.
  echo ">> updating stack $STACK (change set first)"
  OUT=$("${AWS[@]}" cloudformation deploy \
    --stack-name "$STACK" --template-file runner.cfn.yaml \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides "${PARAMS[@]}" \
    --no-execute-changeset --no-fail-on-empty-changeset 2>&1) || { echo "$OUT" >&2; exit 1; }
  CS_ARN=$(printf '%s\n' "$OUT" | grep -oE 'arn:aws:cloudformation:[^ ]+:changeSet/[^ ]+' | head -1 || true)
  if [[ -z "$CS_ARN" ]]; then
    echo "   no changes to deploy"
  else
    "${AWS[@]}" cloudformation describe-change-set --change-set-name "$CS_ARN" --output json > "$SEED_DIR/cs.json"
    INSTANCE_CHANGE=$(python3 - "$SEED_DIR/cs.json" <<'PY'
import json, sys
cs = json.load(open(sys.argv[1]))
for c in cs.get("Changes", []):
    rc = c.get("ResourceChange", {})
    print(f"   {rc.get('Action'):8s} {rc.get('LogicalResourceId'):18s} replacement={rc.get('Replacement', '-')}",
          file=sys.stderr)
    if rc.get("LogicalResourceId") == "Instance":
        props = sorted({d.get("Target", {}).get("Name") or d.get("Target", {}).get("Attribute", "?")
                        for d in rc.get("Details", [])})
        print(f"Instance {rc.get('Action')} (replacement={rc.get('Replacement')}; {', '.join(props)})")
PY
)
    if [[ -n "$INSTANCE_CHANGE" && "$ALLOW_INSTANCE_CHANGE" != "1" ]]; then
      "${AWS[@]}" cloudformation delete-change-set --change-set-name "$CS_ARN" >/dev/null || true
      die "this update touches the instance: $INSTANCE_CHANGE.
   A UserData change stop/starts it; a volume/AMI change REPLACES it (a new runner row — run
   ./wire.sh --name $NAME --drill afterwards to swap it in). Re-run with --allow-instance-change
   to proceed. Nothing was changed."
    fi
    "${AWS[@]}" cloudformation execute-change-set --change-set-name "$CS_ARN"
    echo "   waiting for update to complete"
    "${AWS[@]}" cloudformation wait stack-update-complete --stack-name "$STACK"
  fi
fi

echo ">> outputs"
OUT=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json)
IP=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='PublicIp'))")
KID=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='KeyPairId'))")

# Pull the CFN-managed private key out of SSM for SSH access.
KEYFILE="./${STACK}-key.pem"
"${AWS[@]}" ssm get-parameter --name "/ec2/keypair/${KID}" --with-decryption \
  --query 'Parameter.Value' --output text > "$KEYFILE"
chmod 600 "$KEYFILE"

WIRE="./wire.sh --name $NAME --drill"
[[ "$STANDBY" == "1" ]] && WIRE="./wire.sh --name $NAME --standby --agents <a,b> --drill"
echo ""
echo "==> UP. stack=$STACK runner=$NAME ip=$IP"
echo "    next: $WIRE"
echo "    ssh:  ssh -i $KEYFILE ubuntu@$IP"
echo "    logs: ssh -i $KEYFILE ubuntu@$IP 'journalctl -u canopy-runner -f'"
echo "    cloud-init boots the runner automatically (~5 min to pair; wire.sh waits for it)."
