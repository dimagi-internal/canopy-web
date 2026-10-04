#!/usr/bin/env bash
# Spin DOWN a canopy cloud runner: delete its CloudFormation stack (instance, SG,
# role, key pair), remove the local SSH key, and RETIRE its runner row in
# canopy-web — which deletes every assignment row, source rule and workspace-order
# entry naming it, so nothing is left routing work at a box that no longer exists.
#
#   ./down.sh                          # the live box: stack canopy-cloud-runner, runner cloud-ec2-1
#   ./down.sh --name cloud-ec2-test    # a second box: stack canopy-cloud-runner-test
#   ./down.sh --keep-runner            # RECYCLE: leave the row for wire.sh to swap onto the new box
#   ./down.sh --purge-secrets          # also delete the shared secrets (refused while another
#                                      # runner stack still reads them)
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source ./_lib.sh

PROFILE="${AWS_PROFILE:-labs}"
REGION="${AWS_REGION:-us-east-1}"
AWS=(aws --profile "$PROFILE" --region "$REGION")
BASE_URL="https://labs.connect.dimagi.com/canopy"
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"
NAME="$DEFAULT_RUNNER_NAME"
STACK_ARG=""
RUNNER_ID=""
PURGE=0
KEEP_RUNNER=0
STALE_SECONDS=90   # a box heartbeats every 20s; silent this long = gone

usage() {
  cat <<'USAGE'
usage: ./down.sh [options]

  --name <runner-name>   the runner to tear down (default cloud-ec2-1, the live box)
  --stack <stack>        its stack (default derived from --name, as up.sh does)
  --runner-id <uuid>     retire exactly this runner row instead of finding it by name
  --keep-runner          delete the stack but leave the runner row and its routing — for a
                         recycle, where ./wire.sh swaps the NEW box into the old one's slots.
                         (Retiring first would delete the rows wire.sh has to copy.)
  --purge-secrets        also delete the canopy/cloud-runner/* secrets. They are SHARED by
                         every runner stack, so this is refused while any other runner stack exists.
  --base-url <url>       canopy-web (default https://labs.connect.dimagi.com/canopy)
  -h, --help             this
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --stack) STACK_ARG="$2"; shift 2 ;;
    --runner-id) RUNNER_ID="$2"; shift 2 ;;
    --keep-runner) KEEP_RUNNER=1; shift ;;
    --purge-secrets) PURGE=1; shift ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

runner_name_ok "$NAME" || { echo "!! invalid runner name '$NAME'" >&2; exit 1; }
STACK="${STACK_ARG:-${STACK:-$(stack_for_name "$NAME")}}"
echo ">> runner '$NAME' / stack '$STACK'"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ "$KEEP_RUNNER" != "1" ]]; then
  [[ -f "$TOKEN_FILE" ]] || {
    echo "!! no canopy-web token at $TOKEN_FILE — needed to retire the runner row." >&2
    echo "   Mint one (/canopy:canopy-web-pat-mint), or pass --keep-runner to leave the row." >&2
    exit 1
  }
  CANOPY_TOKEN="$(cat "$TOKEN_FILE")"
fi

api() {  # METHOD PATH -> body on stdout
  curl -sS -X "$1" "${BASE_URL}$2" -H "Authorization: Bearer ${CANOPY_TOKEN}"
}

# Purge guard FIRST, before anything is deleted: the secrets are shared, and the
# live box re-reads canopy-pat / claude-oauth-token on every service start.
if [[ "$PURGE" == "1" ]]; then
  OTHERS=$("${AWS[@]}" cloudformation list-stacks \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE ROLLBACK_COMPLETE \
      CREATE_IN_PROGRESS UPDATE_IN_PROGRESS \
    --query "StackSummaries[?starts_with(StackName, '${DEFAULT_STACK}')].StackName" --output text \
    | tr '\t' '\n' | grep -vx "$STACK" | grep -v '^$' || true)
  if [[ -n "$OTHERS" ]]; then
    echo "!! --purge-secrets refused: these runner stacks still read canopy/cloud-runner/*:" >&2
    echo "$OTHERS" | sed 's/^/     /' >&2
    echo "   Tear them down first, or drop --purge-secrets. Nothing was changed." >&2
    exit 1
  fi
fi

# ── 1. the stack ─────────────────────────────────────────────────────────────────
if "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then
  echo ">> deleting stack $STACK"
  "${AWS[@]}" cloudformation delete-stack --stack-name "$STACK"
  "${AWS[@]}" cloudformation wait stack-delete-complete --stack-name "$STACK"
  echo "==> stack deleted."
else
  echo "   stack $STACK does not exist — nothing to delete"
fi
rm -f "./${STACK}-key.pem"

# ── 2. the runner row (and with it every route to it) ────────────────────────────
# After the stack, never before: a retired runner 404s its own box, and a box
# that is still running would re-pair as a fresh row.
#
# Found by NAME, but only rows that have gone SILENT: the stack's box is gone, so
# its row stops heartbeating. A row with this name that is still heartbeating
# belongs to some other box (a replacement wire.sh already swapped in) and is
# left alone — retiring it would take a live runner out of the fleet.
if [[ "$KEEP_RUNNER" == "1" ]]; then
  echo "   --keep-runner: leaving the '$NAME' runner row and its routing for wire.sh"
else
  if [[ -n "$RUNNER_ID" ]]; then
    printf '%s\n' "$RUNNER_ID" > "$TMP/retire.txt"
  else
    echo ">> finding '$NAME' runner rows that stopped heartbeating"
    for _ in $(seq 1 36); do
      api GET /api/harness/runners/ > "$TMP/runners.json"
      WAITING=$(NAME="$NAME" STALE="$STALE_SECONDS" python3 - "$TMP/runners.json" "$TMP/retire.txt" <<'PY'
import datetime as dt, json, os, sys
rows = json.load(open(sys.argv[1]))
now = dt.datetime.now(dt.timezone.utc)
stale, fresh = [], []
for r in rows if isinstance(rows, list) else []:
    if r.get("kind") != "cloud" or r.get("name") != os.environ["NAME"] or r.get("status") == "retired":
        continue
    hb = r.get("last_heartbeat_at")
    age = (now - dt.datetime.fromisoformat(hb.replace("Z", "+00:00"))).total_seconds() if hb else 1e9
    (stale if age >= float(os.environ["STALE"]) else fresh).append((r["id"], int(age)))
open(sys.argv[2], "w").write("".join(f"{i}\n" for i, _ in stale))
for i, age in fresh:
    print(f"{i} (last heartbeat {age}s ago)")
PY
)
      [[ -z "$WAITING" ]] && break
      sleep 5
    done
    if [[ -n "${WAITING:-}" ]]; then
      echo "   still heartbeating, so NOT this stack's box — left alone:"
      echo "$WAITING" | sed 's/^/     /'
    fi
  fi
  if [[ -s "$TMP/retire.txt" ]]; then
    while IFS= read -r rid; do
      [[ -n "$rid" ]] || continue
      api POST "/api/harness/runners/${rid}/retire" > "$TMP/retire-out.json"
      python3 -c "
import json, sys
r = json.load(open('$TMP/retire-out.json'))
if not isinstance(r, dict) or 'dropped_routes' not in r:
    sys.exit('   !! retire $rid failed: ' + json.dumps(r)[:300])
d = r['dropped_routes']
print(f\"   retired $rid ({r.get('runner')}): {len(d)} route(s) dropped\")
for x in d:
    print(f\"     - {x['agent']} {x['source'] or 'default order'}{' for ' + x['actor'] if x['actor'] else ''}\")
"
    done < "$TMP/retire.txt"

    # Verify rather than trust: no agent's list or rules may still name it.
    api GET "/api/agents/?limit=200" > "$TMP/agents.json"
    LEFT=0
    for slug in $(python3 -c "import json;print(' '.join(a['slug'] for a in json.load(open('$TMP/agents.json')).get('items', [])))"); do
      api GET "/api/agents/${slug}/runners" > "$TMP/v-rows.json"
      api GET "/api/agents/${slug}/runner-rules" > "$TMP/v-rules.json"
      if python3 - "$TMP/retire.txt" "$TMP/v-rows.json" "$TMP/v-rules.json" <<'PY'
import json, sys
ids = set(filter(None, open(sys.argv[1]).read().split()))
hit = 0
for f in sys.argv[2:]:
    rows = json.load(open(f))
    hit += sum(1 for r in rows if isinstance(rows, list) and r.get("runner_id") in ids)
sys.exit(1 if hit else 0)
PY
      then :; else echo "   !! $slug still routes to a retired runner"; LEFT=1; fi
    done
    [[ "$LEFT" == "0" ]] && echo "==> runner retired; no assignment rows or rules name it."
  else
    echo "   no silent '$NAME' runner row to retire"
  fi
fi

# ── 3. secrets (optional) ────────────────────────────────────────────────────────
if [[ "$PURGE" == "1" ]]; then
  # Every secret a runner stack reads. The four a human stages via secrets.sh, the
  # first-boot seed up.sh publishes (manifest + parts), and the legacy single-value
  # seed (runner-code + runner-code-sha) that stacks created before 2026-10-04 read.
  # gog-keyring-password was missing here until 2026-09-05: a "full" purge left it
  # behind, so a rebuild could pass on inherited state — the exact false green a
  # purge-then-rebuild exists to rule out. Kept in step with
  # secrets.sh/up.sh/_lib.sh/runner.cfn.yaml by tests/test_purge_secrets_is_complete.py.
  for s in canopy/cloud-runner/canopy-pat \
           canopy/cloud-runner/claude-oauth-token \
           canopy/cloud-runner/op-service-account-token \
           canopy/cloud-runner/gog-keyring-password \
           canopy/cloud-runner/runner-seed \
           canopy/cloud-runner/runner-code \
           canopy/cloud-runner/runner-code-sha; do
    echo ">> deleting secret $s"
    "${AWS[@]}" secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null || true
  done
  # The seed's parts are a family (one per 60 KB), so they go by prefix.
  # shellcheck disable=SC2043  # a list of one today; a new family is one more word
  for prefix in canopy/cloud-runner/runner-seed-part-; do
    "${AWS[@]}" secretsmanager list-secrets --filters "Key=name,Values=$prefix" \
      --query 'SecretList[].Name' --output text | tr '\t' '\n' | while read -r s; do
      [[ -n "$s" && "$s" != "None" ]] || continue
      echo ">> deleting secret $s"
      "${AWS[@]}" secretsmanager delete-secret --secret-id "$s" --force-delete-without-recovery >/dev/null || true
    done
  done
fi
