#!/usr/bin/env bash
# runner/ec2/wire.sh — operator side of standing up a cloud runner: script
# what was done by hand on 2026-07-25 (design spec §3) so `./up.sh && ./wire.sh
# [--drill]` is the whole lifecycle, no runbook required.
#
#   1. discover the fresh cloud runner `up.sh` just paired (or take --runner-id)
#   2. stage its credential bundle: the claude token (Secrets Manager, same
#      secret up.sh already required). Nothing else: a box holds no GitHub
#      credential — each agent's turns get its owner's, one turn at a time
#      (AgentDelegation) — and no 1Password key (each agent's comes with it).
#   3. retire every OTHER non-retired cloud runner with the same name (the
#      predecessor this box replaces)
#   4. for every agent that already has an assignment list: replace the
#      predecessor's row with the new runner id (preserving rank + enabled),
#      or append the new runner at the tail if the predecessor wasn't in it
#   5. --drill: wait for the box's first bootstrap, fire a readiness drill on
#      the new runner and poll to completion, printing the pass/fail grid
#      (exit 0 only when every drill PASSed)
#
# --standby replaces steps 3-4 with ONE additive change: the runner is appended
# as a DISABLED row to the --agents given, and nothing else is touched — no
# predecessor retired, no rule or workspace order edited. A disabled row never
# claims routed work, but it IS drillable (drill-before-enable), which is the
# point: prove a second box end to end without moving any traffic onto it.
# `down.sh --name <same>` retires the runner, which deletes those rows again.
#
# Pure curl + python3 — no deps, matching up.sh/down.sh/secrets.sh's house style.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source ./_lib.sh

BASE_URL="https://labs.connect.dimagi.com/canopy"
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"
RUNNER_ID=""
RUNNER_NAME="$DEFAULT_RUNNER_NAME"
AGENTS=""     # comma-separated slug allowlist; empty = every agent with assignments
DRILL=0
STANDBY=0
DISCOVER_MINUTES=15   # cloud-init (node, claude, aws, gh, op) takes 4-6 min before the first pair
BOOTSTRAP_MINUTES=40  # first bootstrap clones + provisions every registered agent
AWS_PROFILE_="${AWS_PROFILE:-labs}"
AWS_REGION_="${AWS_REGION:-us-east-1}"

usage() {
  cat <<'USAGE'
usage: ./wire.sh [options]

  --runner-id <uuid>     skip discovery; wire this specific runner id
  --base-url <url>       canopy-web base URL (default https://labs.connect.dimagi.com/canopy)
  --name <name>          Runner.name to match when discovering / retiring (default cloud-ec2-1;
                         --runner-name is the same flag)
  --agents <a,b,c>       only touch these agents' assignment lists (default: every
                         agent that currently has ANY runner assignment)
  --standby              add the runner as a DISABLED row to --agents (required) and touch
                         nothing else: no retire, no swap, no rule/order edit. For a second
                         box you want to prove (with --drill) without routing work to it.
  --drill                wait for the box's first bootstrap, fire a readiness drill, poll to
                         completion, print the grid; exits non-zero unless every drill passes
  --discover-minutes <n> how long to wait for the fresh box to pair (default 15)
  -h, --help             this
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner-id) RUNNER_ID="$2"; shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --runner-name|--name) RUNNER_NAME="$2"; shift 2 ;;
    --standby) STANDBY=1; shift ;;
    --discover-minutes) DISCOVER_MINUTES="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --shared-vault) echo "   (--shared-vault is ignored: wire.sh no longer reads a shared GitHub token)"; shift 2 ;;
    --drill) DRILL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

runner_name_ok "$RUNNER_NAME" || { echo "!! invalid runner name '$RUNNER_NAME'" >&2; exit 1; }
if [[ "$STANDBY" == "1" && -z "$AGENTS" ]]; then
  echo "!! --standby needs --agents <a,b>: it only ever touches the agents you name" >&2
  exit 1
fi

[[ -f "$TOKEN_FILE" ]] || {
  echo "!! no bearer token at $TOKEN_FILE — mint one first: /canopy:canopy-web-pat-mint" >&2
  exit 1
}
CANOPY_TOKEN="$(cat "$TOKEN_FILE")"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# api METHOD PATH [body-file] — response body written to stdout.
api() {
  local method="$1" path="$2" bodyfile="${3:-}"
  if [[ -n "$bodyfile" ]]; then
    curl -sS -X "$method" "${BASE_URL}${path}" \
      -H "Authorization: Bearer ${CANOPY_TOKEN}" -H 'Content-Type: application/json' \
      --data @"$bodyfile"
  else
    curl -sS -X "$method" "${BASE_URL}${path}" -H "Authorization: Bearer ${CANOPY_TOKEN}"
  fi
}

echo ">> $BASE_URL"

# ── Preflight: what each named agent needs before a box can run it ──────────────
# All of these are ONE-TIME human steps, so they are checked here, before the box
# is touched, with the fix named — not discovered as a failed drill ten minutes in.
#   - the agent's own 1Password vault + service-account key (Settings → Credentials)
#   - its GitHub delegation (the owner's token lent to it; Settings → Credentials → GitHub)
#   - its workspace's shared vault (/w/<ws>/settings/secrets)
# In --standby mode an agent that FOLLOWS its workspace's runner order is refused:
# giving it a row of its own — even a disabled one — makes it stop following,
# leaving it with no enabled runner at all. Drill it by adding the box to the
# workspace order instead, or give the agent an explicit order first.
preflight_agent() {  # <slug> -> 0 ok, 1 refused (message printed)
  local slug="$1"
  api GET "/api/agents/${slug}/" > "$TMP/agent-${slug}.json"
  api GET "/api/agents/${slug}/vault" > "$TMP/vault-${slug}.json"
  api GET "/api/agents/${slug}/github" > "$TMP/github-${slug}.json"
  api GET "/api/agents/${slug}/default-order" > "$TMP/order-of-${slug}.json"
  local ws
  ws=$(python3 -c "import json;print((json.load(open('$TMP/agent-${slug}.json')) or {}).get('workspace') or '')" 2>/dev/null || true)
  [[ -n "$ws" ]] && api GET "/api/workspaces/${ws}/shared-vault" > "$TMP/shared-${slug}.json"
  SLUG="$slug" WS="$ws" STANDBY="$STANDBY" BASE="$BASE_URL" TMPD="$TMP" python3 - <<'PY'
import json, os, sys
slug, ws, tmp, base = os.environ["SLUG"], os.environ["WS"], os.environ["TMPD"], os.environ["BASE"]
def load(name):
    try:
        return json.load(open(f"{tmp}/{name}-{slug}.json"))
    except Exception:
        return {}
problems = []
agent = load("agent")
if not isinstance(agent, dict) or not agent.get("slug"):
    print(f"   {slug}: !! no such agent visible to this token")
    sys.exit(1)
vault = load("vault")
if not vault.get("vault") or not vault.get("key_set"):
    problems.append(f"no agent vault/key registered — {base}/w/{ws}/agents/{slug}/settings (Credentials), "
                    f"or Ada's bin/ada-vault-provision --slug {slug}")
gh = load("github")
if not gh.get("set"):
    problems.append(f"no GitHub delegation — the owner sets it at {base}/w/{ws}/agents/{slug}/settings (Credentials → GitHub)")
shared = load("shared")
if ws and not shared.get("key_set"):
    problems.append(f"workspace {ws} has no shared vault key — {base}/w/{ws}/settings/secrets")
order = load("order-of")
if os.environ["STANDBY"] == "1" and order.get("own") is False:
    problems.append(f"follows workspace {order.get('workspace') or ws}'s runner order — a standby row would "
                    "replace that order with ONE disabled runner and strand it. Not touching it.")
for p in problems:
    print(f"   {slug}: !! {p}")
if not problems:
    print(f"   {slug}: ok (vault {vault.get('vault')}, GitHub as {gh.get('login')}, "
          f"{'own runner order' if order.get('own') else 'follows ' + str(order.get('workspace'))})")
sys.exit(1 if problems else 0)
PY
}

if [[ -n "$AGENTS" ]]; then
  echo ">> preflight: $AGENTS"
  PRE_FAIL=0
  IFS=',' read -ra _PRE <<<"$AGENTS"
  for slug in "${_PRE[@]}"; do
    [[ -n "$slug" ]] || continue
    preflight_agent "$slug" || PRE_FAIL=1
  done
  if [[ "$PRE_FAIL" == "1" ]]; then
    if [[ "$STANDBY" == "1" ]]; then
      echo "!! preflight failed — fix the above (each is a one-time step) and re-run. Nothing was changed." >&2
      exit 1
    fi
    echo "   WARN: preflight problems above; wiring anyway (those agents' drills will fail)"
  fi
fi

# ── Step 1: discover (or accept) the fresh runner ───────────────────────────────
if [[ -z "$RUNNER_ID" ]]; then
  echo ">> waiting up to ${DISCOVER_MINUTES} min for a fresh '$RUNNER_NAME' cloud runner (paired, not yet online)…"
  for _ in $(seq 1 $(( DISCOVER_MINUTES * 12 ))); do
    api GET /api/harness/runners/ > "$TMP/runners.json"
    RUNNER_ID=$(python3 -c "
import json
rows = json.load(open('$TMP/runners.json'))
# The genuinely-fresh box is the one that has NEVER heartbeated: on boot the
# runner pairs (creating this row) and then BLOCKS in fetch_and_stage_credential
# BEFORE it starts heartbeating — so a NULL last_heartbeat_at means 'paired,
# waiting for the credential bundle we are about to POST'. A dead predecessor
# left by down.sh carries a real (stale) timestamp, and since GET orders by
# last_heartbeat_at desc nulls_last it would otherwise sort AHEAD of the new
# box and get picked by mistake. Filter on the never-heartbeated signal, not
# status, to survive the standard down.sh --keep-runner && up.sh && wire.sh recycle.
for r in rows:
    if r['kind'] == 'cloud' and r['name'] == '$RUNNER_NAME' and not r.get('last_heartbeat_at'):
        print(r['id']); break
")
    [[ -n "$RUNNER_ID" ]] && break
    sleep 5
  done
  [[ -n "$RUNNER_ID" ]] || {
    echo "!! timed out (${DISCOVER_MINUTES} min) waiting for a fresh cloud runner named '$RUNNER_NAME' — is up.sh done? cloud-init log: ssh ... 'sudo tail -50 /var/log/cloud-init-output.log'" >&2
    exit 1
  }
fi
echo "==> runner: $RUNNER_ID"

# ── Step 2: credential bundle ────────────────────────────────────────────────────
echo ">> staging credential bundle"
CLAUDE_TOKEN=$(aws --profile "$AWS_PROFILE_" --region "$AWS_REGION_" \
  secretsmanager get-secret-value --secret-id canopy/cloud-runner/claude-oauth-token \
  --query SecretString --output text)
CLAUDE_TOKEN="$CLAUDE_TOKEN" python3 -c "
import json, os
json.dump({'claude_token': os.environ['CLAUDE_TOKEN']}, open('$TMP/cred.json', 'w'))
"
api POST "/api/harness/runners/${RUNNER_ID}/credential" "$TMP/cred.json" | python3 -m json.tool
echo "==> credential staged"

# ── the drill (step 5), defined here so --standby can reach it too ──────────────
wait_for_bootstrap() {
  # A drill fired before the box's first bootstrap sits queued behind it, and the
  # poll budget below (per drill) then times out on a box that is merely busy
  # cloning. Wait for the box to REPORT a finished bootstrap first.
  echo ">> waiting up to ${BOOTSTRAP_MINUTES} min for the runner's first bootstrap to finish…"
  for _ in $(seq 1 $(( BOOTSTRAP_MINUTES * 6 ))); do
    # There is no single-runner GET; the list is the read.
    api GET "/api/harness/runners/" > "$TMP/runner.json" 2>/dev/null || true
    STATE=$(python3 -c "
import json
try:
    rows = json.load(open('$TMP/runner.json'))
    r = next((x for x in rows if x.get('id') == '$RUNNER_ID'), {})
except Exception:
    r = {}
b = r.get('health_bootstrapped_at') or 0
print('done' if b else (r.get('status') or 'unknown'))
")
    if [[ "$STATE" == "done" ]]; then
      echo "   bootstrapped"
      return 0
    fi
    sleep 10
  done
  echo "   WARN: no finished bootstrap reported after ${BOOTSTRAP_MINUTES} min (status: $STATE) — drilling anyway"
}

run_drill() {  # <body-json-file>
  wait_for_bootstrap
  echo ">> firing readiness drill on $RUNNER_ID"
  api POST "/api/harness/runners/${RUNNER_ID}/drill" "$1" > "$TMP/drill-result.json"
  python3 -c "
import json
rows = json.load(open('$TMP/drill-result.json'))
if isinstance(rows, dict) and rows.get('detail'):
    raise SystemExit('drill start failed: ' + rows['detail'])
print(f'   started {len(rows)} drill(s)')
"
  # Drills run SERIALLY on the box (one claude -p at a time, ~75-90s each), so a
  # full 5-agent wave needs ~7-8 min. The old 5-min budget timed the LAST agent out
  # every time — reporting a PENDING that looked like a hang but was just the poll
  # giving up early. Budget per drill started, with a floor, rather than a flat cap.
  DRILL_COUNT=$(python3 -c "import json;print(len(json.load(open('$TMP/drill-result.json'))))")
  POLL_TICKS=$(( DRILL_COUNT * 36 ))   # 36 ticks x 5s = 3 min per drill
  [[ "$POLL_TICKS" -lt 60 ]] && POLL_TICKS=60
  echo ">> polling for completion (up to $((POLL_TICKS * 5 / 60)) min for $DRILL_COUNT drill(s))"
  for _ in $(seq 1 "$POLL_TICKS"); do
    api GET "/api/harness/runners/${RUNNER_ID}/drills" > "$TMP/drills.json"
    PENDING=$(python3 -c "
import json
rows = json.load(open('$TMP/drills.json'))
print(sum(1 for r in rows if r['outcome'] == 'pending'))
")
    [[ "$PENDING" == "0" ]] && break
    sleep 5
  done
  echo ">> drill grid:"
  python3 -c "
import json, sys
rows = json.load(open('$TMP/drills.json'))
for r in sorted(rows, key=lambda r: r['agent_slug']):
    mark = {'pass': 'PASS', 'fail': 'FAIL', 'pending': 'PENDING (timed out waiting)'}[r['outcome']]
    print(f\"   {r['agent_slug']:10s} {mark:28s} {r['summary'][:80]}\")
sys.exit(0 if rows and all(r['outcome'] == 'pass' for r in rows) else 2)
"
}

# ── --standby: one additive change, then (optionally) the drill ─────────────────
if [[ "$STANDBY" == "1" ]]; then
  echo ">> standby: adding $RUNNER_ID as a DISABLED row for: $AGENTS (nothing else is touched)"
  IFS=',' read -ra SB_SLUGS <<<"$AGENTS"
  for slug in "${SB_SLUGS[@]}"; do
    [[ -n "$slug" ]] || continue
    api GET "/api/agents/${slug}/runners" > "$TMP/rows-${slug}.json"
    SB_ACTION=$(python3 -c "
import json
rows = json.load(open('$TMP/rows-${slug}.json'))
if not isinstance(rows, list):
    raise SystemExit('cannot read ${slug} runners: ' + json.dumps(rows)[:200])
new_id = '$RUNNER_ID'
out = [{'runner_id': r['runner_id'], 'enabled': r['enabled']} for r in rows]
if any(r['runner_id'] == new_id for r in rows):
    print('present')
else:
    out.append({'runner_id': new_id, 'enabled': False})
    json.dump({'runners': out}, open('$TMP/put-${slug}.json', 'w'))
    print('appended')
")
    if [[ "$SB_ACTION" == "appended" ]]; then
      api PUT "/api/agents/${slug}/runners" "$TMP/put-${slug}.json" > "$TMP/put-out-${slug}.json"
      python3 -c "
import json, sys
rows = json.load(open('$TMP/put-out-${slug}.json'))
if not isinstance(rows, list):
    sys.exit('${slug}: PUT refused: ' + json.dumps(rows)[:300])
"
    fi
    echo "   $slug: $SB_ACTION (disabled)"
  done
  if [[ "$DRILL" == "1" ]]; then
    python3 -c "
import json
json.dump({'agents': [s for s in '$AGENTS'.split(',') if s]}, open('$TMP/drill.json', 'w'))
"
    run_drill "$TMP/drill.json" || {
      echo "==> standby-wired: $RUNNER_ID (drill did not fully pass) — tear down with ./down.sh --name $RUNNER_NAME"
      exit 2
    }
  fi
  echo "==> standby-wired: $RUNNER_ID ($RUNNER_NAME) — tear down with ./down.sh --name $RUNNER_NAME"
  exit 0
fi

# ── Step 3: retire predecessors ──────────────────────────────────────────────────
#
# Resolve the agent list and SNAPSHOT every agent's assignment rows FIRST, before
# anything is retired. Retiring a runner drops its assignment rows, so a snapshot
# taken afterwards no longer contains the predecessor — step 4 then finds nothing
# to swap, takes its append path, and appends the new runner with a hardcoded
# `enabled: True`. On 2026-08-12 that silently re-enabled the cloud runner for all
# five agents, which had been deliberately disabled. A rebuild must return the box
# to the fleet on exactly the terms it left, so the state step 4 reasons about has
# to be read while it still exists.
if [[ -n "$AGENTS" ]]; then
  IFS=',' read -ra AGENT_SLUGS <<<"$AGENTS"
else
  api GET "/api/agents/?limit=200" > "$TMP/agents.json"
  # `mapfile`/`readarray` is bash4+ only — macOS ships bash 3.2 as /bin/bash and
  # this script runs on the OPERATOR's machine, not the (bash5) EC2 box — so
  # build the array the bash-3.2-compatible way via word-splitting (safe here:
  # slugs are simple identifiers, never containing spaces or glob chars).
  AGENT_SLUGS=($(python3 -c "
import json
print(' '.join(a['slug'] for a in json.load(open('$TMP/agents.json'))['items']))
"))
fi
for slug in "${AGENT_SLUGS[@]}"; do
  [[ -n "$slug" ]] || continue
  api GET "/api/agents/${slug}/runners" > "$TMP/rows-${slug}.json" 2>/dev/null || true
  # Source/actor ROUTING RULES are assignment rows too (same table), so retiring
  # the predecessor cascades them exactly as it cascades the default rows — and
  # they must be snapshotted here, pre-retire, for the same reason. Losing them
  # silently reverts the fleet to "everything runs on the operator's laptop",
  # which is the state actor rules exist to end (spec 2026-09-05).
  api GET "/api/agents/${slug}/runner-rules" > "$TMP/rules-${slug}.json" 2>/dev/null || true
done

# Workspace default orders too: agents with no order of their own FOLLOW their
# workspace's, so this is where most of the fleet's cloud routing lives now
# (2026-10-03). Retiring drops the predecessor from every order, so snapshot first.
api GET "/api/workspaces/" > "$TMP/workspaces.json"
WS_SLUGS=($(python3 -c "
import json
rows = json.load(open('$TMP/workspaces.json'))
rows = rows.get('items', rows) if isinstance(rows, dict) else rows
print(' '.join(w['slug'] for w in rows))
"))
for ws in ${WS_SLUGS[@]+"${WS_SLUGS[@]}"}; do
  [[ -n "$ws" ]] || continue
  api GET "/api/workspaces/${ws}/runner-order" > "$TMP/order-${ws}.json" 2>/dev/null || true
done

echo ">> retiring other non-retired '$RUNNER_NAME' cloud runners"
api GET /api/harness/runners/ > "$TMP/runners.json"
python3 -c "
import json
rows = json.load(open('$TMP/runners.json'))
for r in rows:
    if r['kind'] == 'cloud' and r['name'] == '$RUNNER_NAME' and r['id'] != '$RUNNER_ID':
        print(r['id'])
" > "$TMP/predecessors.txt"

if [[ -s "$TMP/predecessors.txt" ]]; then
  while IFS= read -r pid; do
    echo "   retiring $pid"
    api POST "/api/harness/runners/${pid}/retire" >/dev/null
  done < "$TMP/predecessors.txt"
else
  echo "   none found"
fi

# ── Step 4: swap assignments ─────────────────────────────────────────────────────
# Works from the PRE-RETIRE snapshot taken in step 3 — see the note there. Reading
# the list again here would see the predecessor already gone and silently reset
# `enabled`.
echo ">> swapping agent assignments -> $RUNNER_ID"

for slug in "${AGENT_SLUGS[@]}"; do
  [[ -n "$slug" ]] || continue
  rm -f "$TMP/put-${slug}.json"  # no stale file from a previous slug can leak into this PUT
  if [[ ! -s "$TMP/rows-${slug}.json" ]]; then
    echo "   $slug: not found — skipping"
    continue
  fi
  ACTION=$(python3 -c "
import json
rows = json.load(open('$TMP/rows-${slug}.json'))
if not isinstance(rows, list):
    # An RFC7807 problem+json body (e.g. a mistyped --agents slug -> 404, or a
    # deleted agent) — curl -sS returns 0 with an error body, so guard here
    # rather than let the swap logic throw and abort the whole run mid-loop.
    print('skip')
    raise SystemExit(0)
if not rows:
    print('skip')  # no assignments at all — default scope leaves this agent untouched
    raise SystemExit(0)
predecessors = set(l.strip() for l in open('$TMP/predecessors.txt')) if __import__('os').path.exists('$TMP/predecessors.txt') else set()
new_id = '$RUNNER_ID'
out, swapped = [], False
for r in rows:
    rid = r['runner_id']
    if rid in predecessors:
        if not swapped:
            out.append({'runner_id': new_id, 'enabled': r['enabled']})
            swapped = True
        # a second predecessor row (shouldn't happen — one_assignment_per_agent_runner) is dropped
    else:
        out.append({'runner_id': rid, 'enabled': r['enabled']})
if not swapped:
    out.append({'runner_id': new_id, 'enabled': True})  # append at the tail
json.dump({'runners': out}, open('$TMP/put-${slug}.json', 'w'))
print('replaced' if swapped else 'appended')
")
  if [[ "$ACTION" == "skip" ]]; then
    echo "   $slug: no assignments — skipping"
    continue
  fi
  api PUT "/api/agents/${slug}/runners" "$TMP/put-${slug}.json" >/dev/null
  echo "   $slug: $ACTION"

  # …and the same swap over this agent's routing rules, from the pre-retire
  # snapshot. A rule naming any OTHER runner is carried across byte-for-byte:
  # rewriting one would be the same silent config edit the `enabled` regression
  # was, in reverse.
  rm -f "$TMP/put-rules-${slug}.json"
  [[ -s "$TMP/rules-${slug}.json" ]] || continue
  RULE_ACTION=$(python3 -c "
import json, os
rows = json.load(open('$TMP/rules-${slug}.json'))
if not isinstance(rows, list) or not rows:
    print('none'); raise SystemExit(0)
predecessors = set(l.strip() for l in open('$TMP/predecessors.txt')) if os.path.exists('$TMP/predecessors.txt') else set()
new_id = '$RUNNER_ID'
# The API returns rule rows FLAT, one per runner; a RULE is the rows sharing
# (source, actor), ordered by rank. Regroup, swap, and re-emit in that shape.
rules, order = {}, []
for r in sorted(rows, key=lambda r: r.get('rank', 0)):
    key = (r['source'], r.get('actor', ''))
    if key not in rules:
        rules[key] = {'source': r['source'], 'actor': r.get('actor', ''),
                      'strict': r['strict'], 'runners': []}
        order.append(key)
    rid = new_id if r['runner_id'] in predecessors else r['runner_id']
    # A rule naming BOTH the predecessor and the new box would otherwise emit the
    # same runner twice, which the API rejects (one row per runner per rule).
    if rid not in [x['runner_id'] for x in rules[key]['runners']]:
        rules[key]['runners'].append({'runner_id': rid, 'enabled': r['enabled']})
out = [rules[k] for k in order]
swapped = sum(1 for r in rows if r['runner_id'] in predecessors)
json.dump({'rules': out}, open('$TMP/put-rules-${slug}.json', 'w'))
print(f'{len(out)} rule(s), {swapped} row(s) moved')
")
  if [[ "$RULE_ACTION" != "none" ]]; then
    api PUT "/api/agents/${slug}/runner-rules" "$TMP/put-rules-${slug}.json" >/dev/null
    echo "   $slug rules: $RULE_ACTION"
  fi
done

# …and in each workspace default order that named a predecessor, from the
# pre-retire snapshot. An order that did not name one is left alone: adding the
# new box to a workspace's order is a routing decision, not part of a rebuild.
echo ">> swapping workspace default orders -> $RUNNER_ID"
for ws in ${WS_SLUGS[@]+"${WS_SLUGS[@]}"}; do
  [[ -s "$TMP/order-${ws}.json" ]] || continue
  rm -f "$TMP/put-order-${ws}.json"
  ORDER_ACTION=$(python3 -c "
import json, os
rows = json.load(open('$TMP/order-${ws}.json'))
if not isinstance(rows, list) or not rows:
    print('none'); raise SystemExit(0)
predecessors = set(l.strip() for l in open('$TMP/predecessors.txt')) if os.path.exists('$TMP/predecessors.txt') else set()
if not any(r['runner_id'] in predecessors for r in rows):
    print('none'); raise SystemExit(0)
new_id = '$RUNNER_ID'
out = []
for r in sorted(rows, key=lambda r: r['rank']):
    rid = new_id if r['runner_id'] in predecessors else r['runner_id']
    if rid not in [x['runner_id'] for x in out]:
        out.append({'runner_id': rid, 'enabled': r['enabled']})
json.dump({'runners': out}, open('$TMP/put-order-${ws}.json', 'w'))
print('replaced')
")
  if [[ "$ORDER_ACTION" == "replaced" ]]; then
    api PUT "/api/workspaces/${ws}/runner-order" "$TMP/put-order-${ws}.json" >/dev/null
    echo "   $ws: replaced"
  fi
done

# ── Step 5: optional drill wave ──────────────────────────────────────────────────
if [[ "$DRILL" == "1" ]]; then
  echo '{}' > "$TMP/drill.json"
  run_drill "$TMP/drill.json" || { echo "==> wired: $RUNNER_ID (drill did not fully pass)"; exit 2; }
fi

echo "==> wired: $RUNNER_ID"
