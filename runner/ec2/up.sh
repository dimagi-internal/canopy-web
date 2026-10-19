#!/usr/bin/env bash
# Spin UP a canopy cloud runner via CloudFormation. Idempotent (create or update).
#
#   aws sso login --profile labs
#   ./up.sh                                  # the live box: stack canopy-cloud-runner, runner cloud-ec2-1
#   ./up.sh --name cloud-ec2-test --workspace dimagi --standby   # a NEW box needs --workspace
#   ./up.sh --name cloud-ec2-fork --test --workspace canopy      # a test box: takes only what is put on it
#   ./wire.sh --name <same> [--standby --agents a,b] --drill   # then wire it
#   ./down.sh --name <same>                  # tear it down (stack + canopy-web runner row)
#
# A new box holds no PAT. up.sh invites it (POST /api/harness/runner-invitations/,
# with YOUR workbench token) into the workspace you name, bound to the stack's
# instance role, prints the approve link and waits: you approve it in the browser
# with one passkey touch, and the box pairs by proving its role.
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
BASE_URL="https://canopy.dimagi.com"
NAME="$DEFAULT_RUNNER_NAME"
STACK_ARG=""
STANDBY=0
TEST=0
PROJECTS=""
WORKSPACE=""
VOLUME_SIZE=""
INSTANCE_TYPE=""
ALLOW_INSTANCE_CHANGE=0
ALLOW_DIRTY=0
APPROVE_MINUTES=30
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"

usage() {
  cat <<'USAGE'
usage: ./up.sh [options]

  --name <runner-name>      Runner.name the box pairs as (default cloud-ec2-1, the live box)
  --workspace <slug>        the workspace a NEW box pairs into (required to create a stack;
                            an update keeps the stack's own RunnerWorkspace)
  --approve-minutes <n>     how long to wait for you to approve the pairing invitation (default 30;
                            the invitation itself expires after an hour)
  --stack <stack>           CloudFormation stack (default: derived from --name —
                            cloud-ec2-1 -> canopy-cloud-runner, cloud-ec2-X -> canopy-cloud-runner-X)
  --standby                 a box that claims NOTHING it is not explicitly given: no repo
                            (RunnerProjects='') and no chat-session turns (RunnerSessions=0).
                            Agent work reaches it only through assignment rows / drills.
  --test                    a directed-only TEST box: it claims only turns pinned to it and
                            chats bound to it (RunnerDirected=1), serves no agent
                            (RunnerAgents=''), takes chats (RunnerSessions=1) for --projects.
                            Not for the live name; needs --workspace. Then: ./wire.sh --test
  --projects <a,b>          the repos a --test box holds (default canopy-web)
  --volume-size <GiB>       root volume (template default 40)
  --instance-type <type>    EC2 type (template default t3.medium)
  --base-url <url>          canopy-web (default https://canopy.dimagi.com)
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
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --approve-minutes) APPROVE_MINUTES="$2"; shift 2 ;;
    --stack) STACK_ARG="$2"; shift 2 ;;
    --standby) STANDBY=1; shift ;;
    --test) TEST=1; shift ;;
    --projects) PROJECTS="$2"; shift 2 ;;
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

# A --test box takes nothing it is not given, so it must never be the live box,
# and it must say where it pairs (the template default is not a decision).
if [[ "$TEST" == "1" ]]; then
  [[ "$STANDBY" == "1" ]] && die "--test and --standby are different boxes: pick one"
  [[ "$NAME" == "$DEFAULT_RUNNER_NAME" ]] \
    && die "--test is never the live box ($DEFAULT_RUNNER_NAME): pass --name <test-name>"
  [[ -n "$WORKSPACE" ]] || die "--test needs --workspace <slug>: the workspace the test box pairs into"
fi
[[ -z "$PROJECTS" || "$TEST" == "1" ]] || die "--projects is for a --test box"

# ── preflight: every one-time human step, checked before anything is written ────
for tool in aws python3 git curl; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not on PATH"
done

# YOUR canopy-web token: up.sh invites the new box with it, and wire.sh / down.sh
# use it too. Checked first, so a dead one stops everything before AWS is touched.
CANOPY_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE" 2>/dev/null || true)"
canopy() {  # canopy METHOD PATH [json-body] — body to stdout, HTTP code to $TMPC/code
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -o "$TMPC/body" -w '%{http_code}' -X "$method" "${BASE_URL}${path}"
              -H "Authorization: Bearer ${CANOPY_TOKEN}")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data "$body")
  curl "${args[@]}" > "$TMPC/code" 2>/dev/null || echo 000 > "$TMPC/code"
  cat "$TMPC/body" 2>/dev/null || true
}
TMPC="$(mktemp -d)"
trap 'rm -rf "$TMPC"' EXIT
TOKEN_CODE=000
if [[ -n "$CANOPY_TOKEN" ]]; then
  canopy GET /api/harness/runners/ > /dev/null
  TOKEN_CODE="$(cat "$TMPC/code")"
fi
[[ "$TOKEN_CODE" == "200" ]] \
  || die "your workbench token at ~/.claude/canopy/workbench-token is dead or missing (HTTP $TOKEN_CODE from $BASE_URL) — run /canopy:canopy-web-pat-mint"
echo ">> canopy-web token ok"

echo ">> account"
"${AWS[@]}" sts get-caller-identity --query Account --output text \
  || die "AWS credentials for profile '$PROFILE' are not valid — run: aws sso login --profile $PROFILE"

# The secrets every box reads at boot. They are SHARED by every runner stack —
# a second box reuses the live box's — so a missing one is a one-time human step.
# No canopy PAT among them: a new box pairs by invitation and holds none.
for pair in "canopy/cloud-runner/claude-oauth-token|./secrets.sh claude <file>   (the build FALLBACK — only read when no live runner has logins for wire.sh to copy; must exist, may be old)" \
            "canopy/cloud-runner/gog-keyring-password|./secrets.sh gog <file>   (any strong password; without it no agent has Gmail)"; do
  s="${pair%%|*}"; fix="${pair#*|}"
  "${AWS[@]}" secretsmanager describe-secret --secret-id "$s" >/dev/null 2>&1 \
    || die "missing secret '$s' — stage it: $fix"
done

STACK_STATUS=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "NONE")
case "$STACK_STATUS" in
  ROLLBACK_COMPLETE|ROLLBACK_FAILED|DELETE_FAILED)
    die "stack $STACK is $STACK_STATUS and cannot be updated — ./down.sh --stack $STACK --keep-runner, then re-run" ;;
  *_IN_PROGRESS)
    die "stack $STACK is $STACK_STATUS — wait for it to settle" ;;
esac
echo ">> stack status: $STACK_STATUS"

# The workspace the box pairs into. A NEW stack must be told (a box that lands in
# the CloudFormation default serves the wrong tenant); an update keeps the
# stack's own value, so rebuilding the live box needs no new flag.
my_workspaces() {
  canopy GET /api/workspaces/ | python3 -c 'import json,sys
try:
    print(" ".join(w["slug"] for w in json.load(sys.stdin)))
except Exception:
    pass'
}
if [[ "$STACK_STATUS" == "NONE" ]]; then
  if [[ -z "$WORKSPACE" ]]; then
    die "a new box needs --workspace <slug>: the workspace it pairs into. Yours: $(my_workspaces)"
  fi
  # The invitation's own gates (member, and may run work there), asked now —
  # before anything is written to AWS — since the invitation itself can only be
  # made once the stack's role exists.
  canopy GET "/api/harness/runner-invitations/check?workspace=${WORKSPACE}" > "$TMPC/check.json"
  case "$(cat "$TMPC/code")" in
    200) ;;
    403) die "you may not run work in workspace '$WORKSPACE' (an invitation needs it): $(cat "$TMPC/check.json")" ;;
    404) die "you are not a member of workspace '$WORKSPACE'. Yours: $(my_workspaces)" ;;
    *) die "canopy-web could not check workspace '$WORKSPACE' (HTTP $(cat "$TMPC/code")): $(cat "$TMPC/check.json")" ;;
  esac
else
  CURRENT_WS=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='RunnerWorkspace'].ParameterValue" --output text 2>/dev/null || true)
  [[ "$CURRENT_WS" == "None" ]] && CURRENT_WS=""
  if [[ -n "$WORKSPACE" && -n "$CURRENT_WS" && "$WORKSPACE" != "$CURRENT_WS" ]]; then
    die "stack $STACK pairs into '$CURRENT_WS', not '$WORKSPACE'. An existing box keeps its workspace (enroll it in another from the runner page). Nothing was changed."
  fi
  WORKSPACE="${CURRENT_WS:-$WORKSPACE}"
fi
echo ">> workspace: ${WORKSPACE:-<the stack default>}"

# Provenance of the bytes about to be published: the SAME quantity
# deploy-labs.yml computes for the server's `expected_code_sha`, over the same
# paths (tests/test_seed_roundtrip.py pins the lists equal). update_runner.sh was
# missing here until 2026-10-04, so a seed published after an updater-only change
# was stamped with an older sha than the deploy expected.
SHA_PATHS=(cloud_runner.py bootstrap_agents.sh update_runner.sh ../../packages/canopy_transcript ../canopy_acp)
CODE_SHA="$(git log -1 --format=%H -- "${SHA_PATHS[@]}" 2>/dev/null || true)"
CODE_AT="$(git log -1 --format=%ct -- "${SHA_PATHS[@]}" 2>/dev/null || true)"
[[ -n "$CODE_SHA" ]] || die "cannot resolve the runner source sha (shallow clone?). A box seeded without it reports unknown provenance and NEVER auto-updates — fetch full history first."
if [[ -n "$(git status --porcelain -- "${SHA_PATHS[@]}" 2>/dev/null)" && "$ALLOW_DIRTY" != "1" ]]; then
  die "uncommitted changes to ${SHA_PATHS[*]} — the seed would not be the code its stamp names. Commit them, or pass --allow-dirty."
fi


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
trap 'rm -rf "$SEED_DIR" "$TMPC"' EXIT
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
[[ -n "$WORKSPACE" ]] && PARAMS+=("RunnerWorkspace=${WORKSPACE}")
[[ -n "$VOLUME_SIZE" ]] && PARAMS+=("VolumeSize=${VOLUME_SIZE}")
[[ -n "$INSTANCE_TYPE" ]] && PARAMS+=("InstanceType=${INSTANCE_TYPE}")
if [[ "$STANDBY" == "1" ]]; then
  PARAMS+=("RunnerProjects=" "RunnerSessions=0")
fi
if [[ "$TEST" == "1" ]]; then
  PARAMS+=("RunnerAgents=" "RunnerProjects=${PROJECTS:-canopy-web}" "RunnerSessions=1" "RunnerDirected=1")
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

# The box's zero-knowledge ECDH key lives in KMS, in its own stack (ecdh-kms.cfn.yaml):
# only this runner's instance role may derive with it. A separate stack because an
# update to the runner stack re-resolves the AMI and REPLACES the instance — adding a
# key must never do that. Idempotent; the box adopts the key on its next start.
ROLE_NAME=$("${AWS[@]}" cloudformation describe-stack-resource --stack-name "$STACK" \
  --logical-resource-id RunnerRole --query 'StackResourceDetail.PhysicalResourceId' --output text)
ROLE_ARN=$("${AWS[@]}" iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)
echo ">> ECDH key stack ${STACK}-ecdh (role $ROLE_NAME)"
"${AWS[@]}" cloudformation deploy \
  --stack-name "${STACK}-ecdh" --template-file ecdh-kms.cfn.yaml \
  --parameter-overrides "RunnerRoleArn=$ROLE_ARN" --no-fail-on-empty-changeset

echo ">> outputs"
OUT=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query 'Stacks[0].Outputs' --output json)
IP=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='PublicIp'))")
KID=$(echo "$OUT" | python3 -c "import sys,json;print(next(o['OutputValue'] for o in json.load(sys.stdin) if o['OutputKey']=='KeyPairId'))")

# Pull the CFN-managed private key out of SSM for SSH access. KEY_DIR (default:
# here) exists for the tests, which run up.sh in parallel and must not share — or
# delete — one another's key file in this directory.
KEYFILE="${KEY_DIR:-.}/${STACK}-key.pem"
"${AWS[@]}" ssm get-parameter --name "/ec2/keypair/${KID}" --with-decryption \
  --query 'Parameter.Value' --output text > "$KEYFILE"
chmod 600 "$KEYFILE"

# ── the pairing invitation (a new box holds no PAT) ──────────────────────────────
# Needed when no live runner of this name exists yet: a new stack, or one whose box
# never paired (or was retired). A replaced instance of an existing runner needs
# none — its role is already that runner's approved key, so it re-attaches.
INVITE_FILE="${KEY_DIR:-.}/${STACK}-invitation.json"
RUNNER_ID=""
NEED_INVITE=1
if [[ "$STACK_STATUS" != "NONE" ]]; then
  PAT_SECRET=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Parameters[?ParameterKey=='CanopyTokenSecret'].ParameterValue" --output text 2>/dev/null || true)
  [[ "$PAT_SECRET" == "None" ]] && PAT_SECRET=""
fi
if [[ "$STACK_STATUS" != "NONE" && -n "${PAT_SECRET:-}" ]]; then
  # An older stack hands its box a PAT (CanopyTokenSecret), and a box with a
  # token pairs the old way: it would never use an invitation.
  NEED_INVITE=0
  echo ">> no pairing invitation: stack $STACK still names a PAT secret ($PAT_SECRET), so its box pairs with that"
elif [[ "$STACK_STATUS" != "NONE" ]]; then
  canopy GET /api/harness/runners/ > "$TMPC/runners.json"
  if NAME="$NAME" python3 - "$TMPC/runners.json" <<'PY'
import json, os, sys
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    rows = []
sys.exit(0 if any(r.get("kind") == "cloud" and r.get("name") == os.environ["NAME"]
                  and r.get("status") != "retired" for r in rows if isinstance(r, dict)) else 1)
PY
  then
    NEED_INVITE=0
    echo ">> runner '$NAME' is already paired — no invitation needed (a replaced instance re-attaches by its role)"
  fi
fi

if [[ "$NEED_INVITE" == "1" ]]; then
  [[ -n "$WORKSPACE" ]] || die "no workspace to invite '$NAME' into — pass --workspace <slug>. Yours: $(my_workspaces)"
  # The box's zero-knowledge ECDH public key (KMS GetPublicKey on the -ecdh stack's
  # key), so the one approval covers it too. Omitted when it cannot be read.
  ECDH_ARN=$("${AWS[@]}" cloudformation describe-stacks --stack-name "${STACK}-ecdh" \
    --query "Stacks[0].Outputs[?OutputKey=='EcdhKeyArn'].OutputValue" --output text 2>/dev/null || true)
  ECDH_DER=""
  if [[ -n "$ECDH_ARN" && "$ECDH_ARN" != "None" ]]; then
    ECDH_DER=$("${AWS[@]}" kms get-public-key --key-id "$ECDH_ARN" --query PublicKey --output text 2>/dev/null || true)
  fi
  BODY=$(NAME="$NAME" WS="$WORKSPACE" ROLE="$ROLE_ARN" DER="$ECDH_DER" python3 - <<'PY'
import base64, json, os
body = {"name": os.environ["NAME"], "workspace": os.environ["WS"], "role_arn": os.environ["ROLE"]}
try:
    der = base64.b64decode(os.environ.get("DER", "").strip() or "")
except Exception:
    der = b""
# A P-256 SubjectPublicKeyInfo ends in the uncompressed point 04 || X || Y.
if len(der) >= 65 and der[-65] == 4:
    b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    body["ecdh_public_jwk"] = {"kty": "EC", "crv": "P-256", "x": b64u(der[-64:-32]), "y": b64u(der[-32:])}
print(json.dumps(body))
PY
)
  canopy POST /api/harness/runner-invitations/ "$BODY" > "$TMPC/invitation.json"
  [[ "$(cat "$TMPC/code")" == "201" ]] \
    || die "canopy-web refused the pairing invitation (HTTP $(cat "$TMPC/code")): $(cat "$TMPC/invitation.json")"
  INV_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$TMPC/invitation.json")
  APPROVE_URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["approve_url"])' "$TMPC/invitation.json")
  printf '{"id": "%s", "runner_id": null}\n' "$INV_ID" > "$INVITE_FILE"
  echo ""
  echo ">> pairing invitation for '$NAME' in '$WORKSPACE' (role $ROLE_ARN)"
  echo "   approve: $APPROVE_URL"
  echo "   (one passkey touch; also emailed to you. Waiting up to ${APPROVE_MINUTES} min for the box to pair…)"
  STATUS=""
  for _ in $(seq 1 $(( APPROVE_MINUTES * 6 ))); do
    canopy GET "/api/harness/runner-invitations/${INV_ID}" > "$TMPC/inv-state.json"
    STATUS=$(python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1])); print(d.get("status", ""), d.get("runner_id") or "")
except Exception:
    print("")' "$TMPC/inv-state.json")
    case "$STATUS" in
      consumed*) RUNNER_ID="${STATUS#consumed }"; break ;;
      expired*) die "the invitation expired before the box paired — re-run ./up.sh for a new one" ;;
    esac
    sleep 10
  done
  if [[ -z "$RUNNER_ID" ]]; then
    die "the box did not pair within ${APPROVE_MINUTES} min (invitation: ${STATUS%% *}). Approve it at $APPROVE_URL, then run ./wire.sh --name $NAME; or re-run ./up.sh for a new invitation."
  fi
  printf '{"id": "%s", "runner_id": "%s"}\n' "$INV_ID" "$RUNNER_ID" > "$INVITE_FILE"
  echo "==> paired: runner $RUNNER_ID ($INVITE_FILE)"
fi

WIRE="./wire.sh --name $NAME --drill"
[[ "$STANDBY" == "1" ]] && WIRE="./wire.sh --name $NAME --standby --agents <a,b> --drill"
[[ "$TEST" == "1" ]] && WIRE="./wire.sh --name $NAME --test"
echo ""
echo "==> UP. stack=$STACK runner=$NAME${RUNNER_ID:+ ($RUNNER_ID)} ip=$IP"
echo "    next: $WIRE"
echo "    ssh:  ssh -i $KEYFILE ubuntu@$IP"
echo "    logs: ssh -i $KEYFILE ubuntu@$IP 'journalctl -u canopy-runner -f'"
