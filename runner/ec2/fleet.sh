#!/usr/bin/env bash
# A runner FLEET: an Auto Scaling group of interchangeable cloud runners that
# canopy-web grows and shrinks with the work waiting for it (spec
# docs/superpowers/specs/2026-10-24-runner-fleets-design.md; README "Runner fleets").
#
#   aws sso login --profile labs
#   ./fleet.sh up   --name dimagi --workspace dimagi [--instance-type m7i.xlarge]
#                   [--min 1] [--max 4] [--warm 1] [--agents ace,eva] [--projects canopy-web]
#                   [--credential-set "jj credentials"]
#   ./fleet.sh down --name dimagi
#
# `up` builds (or updates) the stack canopy-fleet-<name> and its -ecdh key stack,
# then asks canopy-web for the fleet with YOUR workbench token and prints the approve
# link: one passkey touch, once, ever. From then on every instance the group starts
# joins by itself — AWS vouches for it — and canopy-web sizes the group. Re-running
# `up` on an approved fleet updates the stack and the bounds; it asks nothing.
#
# `--credential-set`: the members read one of your existing credential sets (by name or
# id) — a list other runners of yours already read, in this workspace tree or another;
# without it the fleet gets a new set of its own. Only asked when the fleet is created.
#
# `down` retires the fleet in canopy-web (nothing joins, the scaler lets go), empties
# the group (each member's chats move as it goes), then deletes both stacks. The KMS
# key is retained by its stack's policy.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source ./_lib.sh

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")
BASE_URL="${CANOPY_BASE_URL:-https://canopy.dimagi.com}"
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"
CMD="${1:-}"; shift || true
NAME="" WORKSPACE="" INSTANCE_TYPE="" MIN=1 MAX=4 WARM=1 AGENTS="" PROJECTS="" APPROVE_MINUTES=30 CREDENTIAL_SET=""

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}
die() { echo "!! $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
    --min) MIN="$2"; shift 2 ;;
    --max) MAX="$2"; shift 2 ;;
    --warm) WARM="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --projects) PROJECTS="$2"; shift 2 ;;
    --approve-minutes) APPROVE_MINUTES="$2"; shift 2 ;;
    --credential-set) CREDENTIAL_SET="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[[ "$CMD" == "up" || "$CMD" == "down" ]] || usage
[[ "$NAME" =~ ^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || die "--name: a lowercase slug of at most 40 characters"
(( MIN >= 1 && MIN <= MAX )) || die "--min must be at least 1 and at most --max"
STACK="canopy-fleet-${NAME}"

for tool in aws python3 git curl; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is not on PATH"
done
CANOPY_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE" 2>/dev/null || true)"
TMPC="$(mktemp -d)"
trap 'rm -rf "$TMPC"' EXIT
canopy() {  # canopy METHOD PATH [json-body] — body to stdout, HTTP code to $TMPC/code
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -o "$TMPC/body" -w '%{http_code}' -X "$method" "${BASE_URL}${path}"
              -H "Authorization: Bearer ${CANOPY_TOKEN}")
  [[ -n "$body" ]] && args+=(-H 'Content-Type: application/json' --data "$body")
  curl "${args[@]}" > "$TMPC/code" 2>/dev/null || echo 000 > "$TMPC/code"
  cat "$TMPC/body" 2>/dev/null || true
}
json() {  # json <file> <python expression over d>
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}
canopy GET /api/harness/runner-fleets/ > "$TMPC/fleets.json"
[[ "$(cat "$TMPC/code")" == "200" ]] \
  || die "your workbench token at $TOKEN_FILE is dead or missing (HTTP $(cat "$TMPC/code")) — run /canopy:canopy-web-pat-mint"
"${AWS[@]}" sts get-caller-identity --query Account --output text >/dev/null \
  || die "AWS credentials for profile '$PROFILE' are not valid — run: aws sso login --profile $PROFILE"
FLEET_ID=$(NAME="$NAME" python3 -c 'import json,os,sys
print(next((f["id"] for f in json.load(open(sys.argv[1])) if f["name"] == os.environ["NAME"]), ""))' "$TMPC/fleets.json")

# ── down ─────────────────────────────────────────────────────────────────────────
if [[ "$CMD" == "down" ]]; then
  if [[ -n "$FLEET_ID" ]]; then
    canopy POST "/api/harness/runner-fleets/${FLEET_ID}/retire" > /dev/null
    [[ "$(cat "$TMPC/code")" == "200" ]] || die "canopy-web could not retire fleet $NAME (HTTP $(cat "$TMPC/code"))"
    echo ">> fleet $NAME retired in canopy-web: nothing joins, the scaler has let go"
  fi
  if "${AWS[@]}" autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$STACK" \
       --query 'AutoScalingGroups[0].AutoScalingGroupName' --output text 2>/dev/null | grep -q "$STACK"; then
    echo ">> emptying group $STACK (each member's chats move as it goes)"
    # canopy-web protects every member from scale-in and lifts it only while the
    # fleet is live; once retired nothing lifts it, and a protected member never leaves.
    ids=$("${AWS[@]}" autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$STACK" \
            --query 'AutoScalingGroups[0].Instances[?ProtectedFromScaleIn].InstanceId' --output text 2>/dev/null || true)
    if [[ -n "$ids" && "$ids" != "None" ]]; then
      # shellcheck disable=SC2086
      "${AWS[@]}" autoscaling set-instance-protection --auto-scaling-group-name "$STACK" \
        --instance-ids $ids --no-protected-from-scale-in
    fi
    # One still waiting on the launch hook (it never joined: a fleet that was never
    # approved) is held there for an hour, and the stack delete waits on it.
    waiting=$( { "${AWS[@]}" autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$STACK" \
                   --query 'AutoScalingGroups[0].Instances[?ends_with(LifecycleState, `Pending:Wait`)].InstanceId' --output text
                 "${AWS[@]}" autoscaling describe-warm-pool --auto-scaling-group-name "$STACK" \
                   --query 'Instances[?ends_with(LifecycleState, `Pending:Wait`)].InstanceId' --output text; } 2>/dev/null || true)
    for i in $waiting; do
      [[ "$i" == "None" ]] && continue
      "${AWS[@]}" autoscaling complete-lifecycle-action --auto-scaling-group-name "$STACK" \
        --lifecycle-hook-name canopy-ready --instance-id "$i" --lifecycle-action-result ABANDON >/dev/null 2>&1 || true
    done
    "${AWS[@]}" autoscaling update-auto-scaling-group --auto-scaling-group-name "$STACK" \
      --min-size 0 --max-size 0 --desired-capacity 0
    "${AWS[@]}" autoscaling delete-warm-pool --auto-scaling-group-name "$STACK" --force-delete >/dev/null 2>&1 || true
    for _ in $(seq 1 60); do
      n=$("${AWS[@]}" autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$STACK" \
            --query 'length(AutoScalingGroups[0].Instances)' --output text 2>/dev/null || echo 0)
      [[ "$n" == "0" || "$n" == "None" ]] && break
      sleep 10
    done
  fi
  for s in "$STACK" "${STACK}-ecdh"; do
    if "${AWS[@]}" cloudformation describe-stacks --stack-name "$s" >/dev/null 2>&1; then
      echo ">> deleting stack $s"
      "${AWS[@]}" cloudformation delete-stack --stack-name "$s"
      "${AWS[@]}" cloudformation wait stack-delete-complete --stack-name "$s"
    fi
  done
  echo "==> DOWN. fleet $NAME"
  exit 0
fi

# ── up ───────────────────────────────────────────────────────────────────────────
if [[ -z "$FLEET_ID" ]]; then
  [[ -n "$WORKSPACE" ]] || die "a new fleet needs --workspace <slug>: the workspace its members serve"
  canopy GET "/api/harness/runner-invitations/check?workspace=${WORKSPACE}" > "$TMPC/check.json"
  [[ "$(cat "$TMPC/code")" == "200" ]] \
    || die "you may not run work in workspace '$WORKSPACE' (HTTP $(cat "$TMPC/code")): $(cat "$TMPC/check.json")"
fi
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i.xlarge}"
# Turns one member runs at once — what cloud_runner.py itself works out from RAM
# (one per 1.5 GiB) — so canopy-web knows what one more member absorbs.
MEM=$("${AWS[@]}" ec2 describe-instance-types --instance-types "$INSTANCE_TYPE" \
        --query 'InstanceTypes[0].MemoryInfo.SizeInMiB' --output text) || die "no instance type $INSTANCE_TYPE"
MEMBER_TURNS=$(( MEM / 1536 )); (( MEMBER_TURNS >= 1 )) || MEMBER_TURNS=1

# The first-boot seed every member installs (as up.sh publishes it): a fleet must
# not start members on whatever seed a box last left in Secrets Manager.
CODE_SHA=$(git log -1 --format=%H -- cloud_runner.py 2>/dev/null || true)
CODE_AT=$(git log -1 --format=%ct -- cloud_runner.py 2>/dev/null || true)
[[ -n "$CODE_SHA" ]] || die "cannot resolve the runner source sha (shallow clone?)"
[[ -z "$(git status --porcelain -- cloud_runner.py)" ]] || die "uncommitted changes to cloud_runner.py — commit them first"
put_secret() {  # <id> <file> <description>
  if "${AWS[@]}" secretsmanager describe-secret --secret-id "$1" >/dev/null 2>&1; then
    "${AWS[@]}" secretsmanager put-secret-value --secret-id "$1" --secret-string "file://$2" >/dev/null
  else
    "${AWS[@]}" secretsmanager create-secret --name "$1" --description "$3" --secret-string "file://$2" >/dev/null
  fi
}
PARTS=$(python3 seed.py build cloud_runner.py "$TMPC/seed" --sha "$CODE_SHA" --committed-at "${CODE_AT:-0}")
echo ">> publishing cloud_runner.py seed ${CODE_SHA:0:12} ($PARTS part(s))"
for (( i = 0; i < PARTS; i++ )); do
  put_secret "${SEED_PART_PREFIX}$i" "$TMPC/seed/part-$i" "cloud_runner.py seed part $i — published by up.sh/fleet.sh"
done
put_secret "$SEED_SECRET" "$TMPC/seed/manifest.json" "cloud_runner.py seed manifest — published by up.sh/fleet.sh"

echo ">> rendering the fleet template (runner.cfn.yaml's own cloud-init)"
python3 fleet_template.py fleet.cfn.yaml runner.cfn.yaml > fleet.rendered.cfn.yaml
"${AWS[@]}" cloudformation validate-template --template-body file://fleet.rendered.cfn.yaml >/dev/null
PARAMS=("FleetName=$NAME" "InstanceType=$INSTANCE_TYPE" "MaxMembers=$MAX")
# Only the zones that offer the type: AWS otherwise tries one that does not (us-east-1e
# has no t3) and launches late, after a failed attempt.
ZONES=$("${AWS[@]}" ec2 describe-instance-type-offerings --location-type availability-zone \
          --filters "Name=instance-type,Values=$INSTANCE_TYPE" --query 'InstanceTypeOfferings[].Location' \
          --output text | tr -s '\t ' '\n' | sort | paste -sd, -)
[[ -n "$ZONES" ]] || die "no zone in $REGION offers $INSTANCE_TYPE"
PARAMS+=("Zones=$ZONES")
[[ -n "$WORKSPACE" ]] && PARAMS+=("RunnerWorkspace=$WORKSPACE")
[[ -n "$AGENTS" ]] && PARAMS+=("RunnerAgents=$AGENTS" "AgentSlugs=$AGENTS")
[[ -n "$PROJECTS" ]] && PARAMS+=("RunnerProjects=$PROJECTS")
deploy_group() {  # <min members> <warm pool size>
  "${AWS[@]}" cloudformation deploy --stack-name "$STACK" --template-file fleet.rendered.cfn.yaml \
    --capabilities CAPABILITY_NAMED_IAM --parameter-overrides "${PARAMS[@]}" \
    "MinMembers=$1" "WarmPoolSize=$2" --no-fail-on-empty-changeset
}
if [[ -z "$FLEET_ID" ]]; then
  # A new fleet's group starts EMPTY. An instance reports ready (the group's launch
  # hook, which the stack waits on) only after it has joined, and it can join only
  # once canopy-web knows the fleet and you have approved it — so the group is
  # sized after that, below.
  echo ">> deploying $STACK (an empty group, until the fleet is approved)"
  deploy_group 0 0
else
  echo ">> deploying $STACK"
  deploy_group "$MIN" "$WARM"
fi
ROLE_ARN=$("${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text)

echo ">> ECDH key stack ${STACK}-ecdh (only $ROLE_ARN may derive)"
"${AWS[@]}" cloudformation deploy --stack-name "${STACK}-ecdh" --template-file ecdh-kms.cfn.yaml \
  --parameter-overrides "RunnerRoleArn=$ROLE_ARN" --no-fail-on-empty-changeset
ECDH_ARN=$("${AWS[@]}" cloudformation describe-stacks --stack-name "${STACK}-ecdh" \
  --query "Stacks[0].Outputs[?OutputKey=='EcdhKeyArn'].OutputValue" --output text)
ECDH_DER=$("${AWS[@]}" kms get-public-key --key-id "$ECDH_ARN" --query PublicKey --output text)

if [[ -n "$FLEET_ID" ]]; then
  canopy PUT "/api/harness/runner-fleets/${FLEET_ID}/size" "{\"min_members\": $MIN, \"max_members\": $MAX}" > /dev/null
  [[ "$(cat "$TMPC/code")" == "200" ]] || die "canopy-web refused the fleet's bounds (HTTP $(cat "$TMPC/code"))"
  echo "==> UP. fleet $NAME ($FLEET_ID), $MIN–$MAX members of $INSTANCE_TYPE, $MEMBER_TURNS turns each"
  exit 0
fi

SET_ID=""
if [[ -n "$CREDENTIAL_SET" ]]; then
  canopy GET /api/harness/runner-credential-sets/ > "$TMPC/sets.json"
  [[ "$(cat "$TMPC/code")" == "200" ]] || die "could not list your credential sets (HTTP $(cat "$TMPC/code"))"
  SET_ID=$(WANT="$CREDENTIAL_SET" python3 -c 'import json,os,sys
want = os.environ["WANT"]
print(next((s["id"] for s in json.load(open(sys.argv[1])) if want in (s["id"], s["name"])), ""))' "$TMPC/sets.json")
  [[ -n "$SET_ID" ]] || die "no credential set of yours is called '$CREDENTIAL_SET'"
fi

BODY=$(NAME="$NAME" WS="$WORKSPACE" ROLE="$ROLE_ARN" DER="$ECDH_DER" REGION="$REGION" GROUP="$STACK" \
       MIN="$MIN" MAX="$MAX" TURNS="$MEMBER_TURNS" SET_ID="$SET_ID" python3 - <<'PY'
import base64, json, os
der = base64.b64decode(os.environ["DER"].strip())
# A P-256 SubjectPublicKeyInfo ends in the uncompressed point 04 || X || Y (as up.sh).
assert len(der) >= 65 and der[-65] == 4, "the fleet's KMS key is not P-256"
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
print(json.dumps({
    "name": os.environ["NAME"], "workspace": os.environ["WS"], "role_arn": os.environ["ROLE"],
    "ecdh_public_jwk": {"kty": "EC", "crv": "P-256", "x": b64u(der[-64:-32]), "y": b64u(der[-32:])},
    "region": os.environ["REGION"], "group_name": os.environ["GROUP"],
    "min_members": int(os.environ["MIN"]), "max_members": int(os.environ["MAX"]),
    "member_turns": int(os.environ["TURNS"]),
    "credential_set": os.environ["SET_ID"] or None,
}))
PY
)
canopy POST /api/harness/runner-fleets/ "$BODY" > "$TMPC/invitation.json"
[[ "$(cat "$TMPC/code")" == "201" ]] \
  || die "canopy-web refused the fleet (HTTP $(cat "$TMPC/code")): $(cat "$TMPC/invitation.json")"
APPROVE_URL=$(json "$TMPC/invitation.json" 'd["approve_url"]')
FLEET_ID=$(json "$TMPC/invitation.json" 'd["fleet"]["id"]')
echo ""
echo ">> fleet $NAME in $WORKSPACE (role $ROLE_ARN, group $STACK)"
echo "   approve: $APPROVE_URL"
echo "   (one passkey touch, once for the whole fleet; also emailed to you. Waiting up to ${APPROVE_MINUTES} min…)"
for _ in $(seq 1 $(( APPROVE_MINUTES * 6 ))); do
  canopy GET "/api/harness/runner-fleets/${FLEET_ID}" > "$TMPC/fleet.json"
  [[ "$(json "$TMPC/fleet.json" 'd.get("status")' 2>/dev/null)" == "active" ]] && break
  sleep 10
done
[[ "$(json "$TMPC/fleet.json" 'd.get("status")' 2>/dev/null)" == "active" ]] \
  || die "the fleet was not approved within ${APPROVE_MINUTES} min — approve it at $APPROVE_URL, then run this same ./fleet.sh up again: its group stays empty until then"
echo ">> approved: sizing $STACK to $MIN member(s) and a warm pool of $WARM"
deploy_group "$MIN" "$WARM"
echo "==> UP. fleet $NAME ($FLEET_ID), $MIN–$MAX members of $INSTANCE_TYPE, $MEMBER_TURNS turns each"
echo "    next, once its first member has joined (a few minutes):"
echo "      its Credentials tab: Make its key (one passkey touch), then + Token for each Claude account"
echo "      (on a shared set: Get a token on each entry with no value in this tree yet) —"
echo "      every member reads that one list, and every later member is given the key by canopy-web."
echo "      route agents to it: add its member to the workspace runner order or an agent's runners —"
echo "      every later member is routed the same."
