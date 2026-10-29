#!/usr/bin/env bash
# Spin UP a canopy cloud runner via CloudFormation. Idempotent (create or update).
#
#   aws sso login --profile labs
#   ./up.sh                                  # the live box: stack canopy-cloud-runner, runner cloud-ec2-1
#   ./up.sh --name cloud-ec2-test --workspace dimagi --standby   # a NEW box needs --workspace
#   ./up.sh --name cloud-ec2-fork --test --workspace canopy      # a test box: takes only what is put on it
#   ./wire.sh --name <same> [--standby --agents a,b] --drill   # then wire it
#   ./down.sh --name <same>                  # tear it down (stack + canopy-web runner row)
#   ./up.sh --name cloud-ec2-9 --workspace dimagi --runners tree:dimagi,tree:marshellis,agent:eva
#                                            # one machine, several runners, ONE approval
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
RUNNERS=""
IN_PLACE=0
REMOVE_RUNNERS=""
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
                            chats bound to it (its invitation pairs it directed), serves no agent
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
  --runners <spec>          the box's runners: tree:<workspace>,agent:<slug>[@<workspace>],… — each its
                            own user, runner and key on the box (Runners). The runner of the box's
                            own workspace keeps the box's name; the others are <name>-<id>. ONE
                            invitation lists them all, so one passkey touch pairs every runner.
  --remove-runner <id>      let this run drop runner <id>'s KMS key slot (repeatable). Without it a
                            runner that already has a key is never dropped: its key would go, and
                            every vault wrap to it with it. The key itself is RETAINED in KMS.
  --in-place                with --runners on an EXISTING stack: leave the runner stack (and the
                            instance) alone — make the runners' KMS keys and the invitation, and print
                            the one command that moves the running box to several runners (README:
                            "Moving a machine to several runners"). Operator step; needs Jonathan's
                            approval on a live box.
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
    --runners) RUNNERS="$2"; shift 2 ;;
    --in-place) IN_PLACE=1; shift ;;
    --remove-runner) REMOVE_RUNNERS="${REMOVE_RUNNERS:+$REMOVE_RUNNERS,}$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

runner_name_ok "$NAME" || { echo "!! invalid runner name '$NAME' (lowercase letters, digits, '-')" >&2; exit 1; }
# STACK env is honored for backwards compatibility; --stack wins over it.
STACK="${STACK_ARG:-${STACK:-$(stack_for_name "$NAME")}}"
echo ">> runner '$NAME' -> stack '$STACK'"

die() { echo "!! $*" >&2; exit 1; }

# stack_read <stack> <query> [missing-ok] — one describe-stacks value, text. Fails on
# ANY error, except — with missing-ok — that the stack does not exist (then: empty).
# A read that silently came back empty is how a transient AWS error would blank a
# parameter on the next deploy.
stack_read() {
  local out err rc
  err="$(mktemp)"
  out="$("${AWS[@]}" cloudformation describe-stacks --stack-name "$1" --query "$2" --output text 2>"$err")"
  rc=$?
  if (( rc != 0 )); then
    if [[ "${3:-}" == "missing-ok" ]] && grep -q "does not exist" "$err"; then
      rm -f "$err"; return 0
    fi
    sed 's/^/   aws: /' "$err" >&2; rm -f "$err"; return 1
  fi
  rm -f "$err"
  printf '%s' "$out"
}

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

# No Secrets Manager secret to stage: a new box pairs by invitation (no canopy PAT),
# derives each agent's gog keyring password from its runner's own secret (spec
# docs/architecture/runners.md), and keeps its Claude credentials in a vault
# its owner signs in on the runner's page (spec 2026-10-23-runner-claude-credentials-
# in-the-vault-design.md) — there is no bootstrap Claude token any more.

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
  # Its runners too: a parameter not passed falls back to its default (one runner) on
  # every update, which would shrink a box of several runners on its next start.
  if [[ -z "$RUNNERS" ]]; then
    # A failed read must stop the run: an empty answer here would shrink the box.
    RUNNERS=$(stack_read "$STACK" "Stacks[0].Parameters[?ParameterKey=='Runners'].ParameterValue") \
      || die "could not read stack $STACK's Runners — nothing was changed"
    [[ "$RUNNERS" == "None" ]] && RUNNERS=""
    if [[ -z "$RUNNERS" ]]; then
      # A stack made before the rename names them RunnerUnits until it is deployed again.
      RUNNERS=$(stack_read "$STACK" "Stacks[0].Parameters[?ParameterKey=='RunnerUnits'].ParameterValue") \
        || die "could not read stack $STACK's RunnerUnits — nothing was changed"
      [[ "$RUNNERS" == "None" ]] && RUNNERS=""
    fi
    [[ -n "$RUNNERS" ]] && echo ">> runners: the stack's own ($RUNNERS)"
  fi
fi
echo ">> workspace: ${WORKSPACE:-<the stack default>}"
# A NEW box always runs its runners the --install-runners way (cloud_runner.py): with
# Runners empty it is ONE runner, tree:<RunnerWorkspace>, named the box, and it pairs
# with `pair-by-role` listing that runner. The invitation must list it too, or canopy
# refuses every pairing call (401) however the invitation is approved -- found standing
# up the first sensitive-data box, 2026-10-22. So a new stack names its one runner explicitly. An existing
# stack keeps what it has (above).
if [[ -z "$RUNNERS" && "$STACK_STATUS" == "NONE" && -n "$WORKSPACE" ]]; then
  RUNNERS="tree:${WORKSPACE}"
  echo ">> runners: ${RUNNERS} (a new box's one runner)"
fi

# ── several runners on one machine (docs/architecture/runners.md) ──
# Parsed and named by cloud_runner.py itself (one source of truth with the box).
[[ "$IN_PLACE" == "1" && -z "$RUNNERS" ]] && die "--in-place moves a box to --runners; name them"
[[ "$IN_PLACE" == "1" && "$STACK_STATUS" == "NONE" ]] && die "--in-place is for an existing stack; a new box just takes --runners"
RUNNERS_JSON='[]'
if [[ -n "$RUNNERS" ]]; then
  RUNNERS_JSON="$(python3 - "$RUNNERS" "${WORKSPACE:-}" "$NAME" <<'PY'
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("cloud_runner_runners", "cloud_runner.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
try:
    print(json.dumps(m.parse_runners(sys.argv[1], workspace=sys.argv[2], box=sys.argv[3])))
except ValueError as exc:
    print(f"!! --runners: {exc}", file=sys.stderr)
    sys.exit(1)
PY
)" || exit 1
  # Every workspace a runner serves passes the invitation's gates now, before AWS.
  for ws in $(printf '%s' "$RUNNERS_JSON" | python3 -c 'import json,sys; print(" ".join(sorted({u["workspace"] for u in json.load(sys.stdin)})))'); do
    canopy GET "/api/harness/runner-invitations/check?workspace=${ws}" > "$TMPC/check.json"
    [[ "$(cat "$TMPC/code")" == "200" ]] \
      || die "runner workspace '$ws': you may not invite a runner there (HTTP $(cat "$TMPC/code")): $(cat "$TMPC/check.json")"
  done
  echo ">> runners: $(printf '%s' "$RUNNERS_JSON" | python3 -c 'import json,sys; print(", ".join(u["kind"]+":"+u["id"]+" as "+u["name"] for u in json.load(sys.stdin)))')"
fi
# The runners other than the box's own get KMS keys of their own (ecdh-kms.cfn.yaml
# has four slots); the box's own runner keeps the key the box always had.
EXTRA_RUNNER_IDS="$(printf '%s' "$RUNNERS_JSON" | NAME="$NAME" python3 -c 'import json,os,sys; print(" ".join(u["id"] for u in json.load(sys.stdin) if u["name"] != os.environ["NAME"]))')"
# Each runner is PINNED to its key slot: a slot's value names the KMS key a runner's
# wraps are addressed to, so a re-run must never move a runner to another slot (that
# swaps aliases) or blank one (that removes the key). Read the -ecdh stack's
# current slots, keep every runner where it is, put new runners in free slots only, and
# drop a slot only for --remove-runner. Without --runners every slot is kept as it is.
# A failed read must stop the run: an empty answer would blank every slot and remove
# the runners' keys' aliases. Only "the -ecdh stack does not exist yet" means "no slots".
# An -ecdh stack made before the rename still names its slots Unit<i> until its logical
# ids are renamed in place; read both, or a re-run would blank every slot.
EXISTING_SLOTS="$(stack_read "${STACK}-ecdh" \
  "Stacks[0].Parameters[?starts_with(ParameterKey, 'Runner') || starts_with(ParameterKey, 'Unit')].[ParameterKey,ParameterValue]" missing-ok)" \
  || die "could not read ${STACK}-ecdh's runner key slots — nothing was changed (a transient AWS error? re-run)"
EXISTING_SLOTS="$(printf '%s' "$EXISTING_SLOTS" | tr '\t' '=')"
RUNNER_SLOTS="$(EXISTING="$EXISTING_SLOTS" WANT="$EXTRA_RUNNER_IDS" GIVEN="${RUNNERS:+1}" REMOVE="$REMOVE_RUNNERS" python3 - <<'PY'
import os, sys
existing = {}
for line in os.environ["EXISTING"].split():
    key, _, value = line.partition("=")
    key = "Runner" + key[len("Unit"):] if key.startswith("Unit") else key
    if key in {f"Runner{i}" for i in range(1, 5)} and value not in ("", "None"):
        existing[key] = value
slots = {f"Runner{i}": existing.get(f"Runner{i}", "") for i in range(1, 5)}
remove = {r for r in os.environ["REMOVE"].split(",") if r}
want = os.environ["WANT"].split()
if remove & set(want):
    sys.exit(f"!! --remove-runner {sorted(remove & set(want))} is also in --runners")
unknown = remove - set(slots.values())
if unknown:
    sys.exit(f"!! --remove-runner {sorted(unknown)}: no such runner key slot")
if os.environ.get("GIVEN"):
    dropped = [v for v in slots.values() if v and v not in want and v not in remove]
    if dropped:
        sys.exit(f"!! --runners drops {dropped}, whose KMS key slot holds every vault wrap to it. "
                 f"Name it again, or pass --remove-runner {dropped[0]} to drop it on purpose.")
for k, v in slots.items():
    if v in remove:
        slots[k] = ""
for runner in want:
    if runner in slots.values():
        continue                      # pinned where it already is
    free = [k for k, v in slots.items() if not v]
    if not free:
        sys.exit("!! at most four runners besides the box's own (ecdh-kms.cfn.yaml has four key slots)")
    slots[free[0]] = runner
print(" ".join(f"{k}={v}" for k, v in slots.items()))
PY
)" || exit 1
echo ">> runner key slots: ${RUNNER_SLOTS}"

# Provenance of the bytes about to be published: the SAME quantity
# deploy-labs.yml computes for the server's `expected_code_sha`, over the same
# paths (tests/test_seed_roundtrip.py pins the lists equal). update_runner.sh was
# missing here until 2026-10-04, so a seed published after an updater-only change
# was stamped with an older sha than the deploy expected.
SHA_PATHS=(cloud_runner.py bootstrap_agents.sh update_runner.sh kms_broker.py aws_broker.py ../../packages/canopy_transcript ../canopy_acp)
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
  PARAMS+=("RunnerAgents=" "RunnerProjects=${PROJECTS:-canopy-web}" "RunnerSessions=1")
fi
[[ -n "$RUNNERS" ]] && PARAMS+=("Runners=${RUNNERS}")
# shellcheck disable=SC2206 # EXTRA_PARAMS is documented as space-separated Key=Val
[[ -n "${EXTRA_PARAMS:-}" ]] && PARAMS+=(${EXTRA_PARAMS})
echo ">> parameters: ${PARAMS[*]}"

if [[ "$IN_PLACE" == "1" ]]; then
  # The running box moves itself (cloud_runner.py --install-runners); the stack and the
  # instance are not touched, so nothing is stopped, started or replaced.
  echo ">> --in-place: the runner stack $STACK is left as it is"
elif [[ "$STACK_STATUS" == "NONE" ]]; then
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
ECDH_PARAMS=("RunnerRoleArn=$ROLE_ARN")
# shellcheck disable=SC2206 # RUNNER_SLOTS is four Runner<i>=<id> words
ECDH_PARAMS+=($RUNNER_SLOTS)
"${AWS[@]}" cloudformation deploy \
  --stack-name "${STACK}-ecdh" --template-file ecdh-kms.cfn.yaml \
  --parameter-overrides "${ECDH_PARAMS[@]}" --no-fail-on-empty-changeset

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
if [[ -n "$RUNNERS" && "$IN_PLACE" == "1" ]]; then
  # Moving a box to several runners ADOPTS its runner: its instance-role key becomes
  # that runner's own key, which canopy-web does only under an approved invitation
  # listing it.
  echo ">> --in-place: an invitation listing every runner (adopting the box needs your approval)"
elif [[ -n "$RUNNERS" ]]; then
  # A box of several runners: an invitation unless EVERY one is already live (a
  # replaced instance re-attaches each runner by the box's role and the runner's name).
  canopy GET /api/harness/runners/ > "$TMPC/runners.json"
  if RUNNERS_JSON="$RUNNERS_JSON" python3 - "$TMPC/runners.json" <<'PY'
import json, os, sys
try:
    rows = json.load(open(sys.argv[1]))
except Exception:
    rows = []
live = {r.get("name") for r in rows if isinstance(r, dict) and r.get("kind") == "cloud"
        and r.get("status") != "retired"}
sys.exit(0 if all(r["name"] in live for r in json.loads(os.environ["RUNNERS_JSON"])) else 1)
PY
  then
    NEED_INVITE=0
    echo ">> every runner is already paired — no invitation needed (each re-attaches by the box's role)"
  fi
elif [[ "$STACK_STATUS" != "NONE" && -n "${PAT_SECRET:-}" ]]; then
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
  # Each extra runner's KMS public key, by the alias the -ecdh stack gave it, so the one
  # approval covers the runner's ECDH key too.
  : > "$TMPC/runner-ders"
  for rid in $EXTRA_RUNNER_IDS; do
    der=$("${AWS[@]}" kms get-public-key --key-id "alias/canopy-runner-ecdh/${ROLE_NAME}/${rid}" \
      --query PublicKey --output text 2>/dev/null || true)
    printf '%s %s\n' "$rid" "$der" >> "$TMPC/runner-ders"
  done
  BODY=$(NAME="$NAME" WS="$WORKSPACE" ROLE="$ROLE_ARN" DER="$ECDH_DER" RUNNERS_JSON="$RUNNERS_JSON" TEST="$TEST" \
         RUNNER_DERS="$TMPC/runner-ders" python3 - <<'PY'
import base64, json, os
body = {"name": os.environ["NAME"], "workspace": os.environ["WS"], "role_arn": os.environ["ROLE"]}
if os.environ.get("TEST") == "1":
    body["directed"] = True  # a test box is directed from the moment it pairs


def jwk_of(text):
    try:
        der = base64.b64decode((text or "").strip() or "")
    except Exception:
        return None
    # A P-256 SubjectPublicKeyInfo ends in the uncompressed point 04 || X || Y.
    if len(der) >= 65 and der[-65] == 4:
        b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
        return {"kty": "EC", "crv": "P-256", "x": b64u(der[-64:-32]), "y": b64u(der[-32:])}
    return None


box_jwk = jwk_of(os.environ.get("DER", ""))
if box_jwk:
    body["ecdh_public_jwk"] = box_jwk
runners = json.loads(os.environ.get("RUNNERS_JSON") or "[]")
if runners:
    ders = dict(line.split(" ", 1) for line in open(os.environ["RUNNER_DERS"]).read().splitlines() if " " in line)
    body["runners"] = []
    for r in runners:
        entry = {"name": r["name"], "workspace": r["workspace"], "kind": r["kind"], "slug": r["slug"]}
        jwk = box_jwk if r["name"] == os.environ["NAME"] else jwk_of(ders.get(r["id"], ""))
        if jwk:
            entry["ecdh_public_jwk"] = jwk
        body["runners"].append(entry)
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
  if [[ "$IN_PLACE" == "1" ]]; then
    echo "   then move the running box to several runners (Jonathan's approval first — README \"Moving a machine to several runners\"):"
    echo "     aws ssm send-command --instance-ids <instance> --document-name AWS-RunShellScript \\"
    echo "       --parameters 'commands=[\"sudo RUNNERS=${RUNNERS} python3 /opt/canopy-runner/cloud_runner.py --install-runners\"]'"
  fi
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
  if [[ -n "$RUNNERS" ]]; then
    python3 -c 'import json,sys
doc = json.load(open(sys.argv[1]))
for r in doc.get("runners") or []:
    print("    runner %s: %s" % (r.get("name"), r.get("runner_id") or "(not paired)"))' "$TMPC/inv-state.json" || true
  fi
fi

WIRE="./wire.sh --name $NAME --drill"
[[ "$STANDBY" == "1" ]] && WIRE="./wire.sh --name $NAME --standby --agents <a,b> --drill"
[[ "$TEST" == "1" ]] && WIRE="./wire.sh --name $NAME --test"
# A box of several runners: the box's own runner is --name; each other one is --instance <id>.
for rid in $EXTRA_RUNNER_IDS; do WIRE="$WIRE; ./wire.sh --name $NAME --instance $rid --standby --agents <a,b>"; done
echo ""
echo "==> UP. stack=$STACK runner=$NAME${RUNNER_ID:+ ($RUNNER_ID)} ip=$IP"
echo "    next: $WIRE"
echo "    ssh:  ssh -i $KEYFILE ubuntu@$IP"
echo "    logs: ssh -i $KEYFILE ubuntu@$IP 'journalctl -u canopy-runner -f'"
