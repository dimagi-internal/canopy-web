#!/usr/bin/env bash
# deploy/ec2-runner/wire.sh — operator side of standing up a cloud runner: script
# what was done by hand on 2026-07-25 (design spec §3) so `./up.sh && ./wire.sh
# [--drill]` is the whole lifecycle, no runbook required.
#
#   1. discover the fresh cloud runner `up.sh` just paired (or take --runner-id)
#   2. stage its credential bundle: claude token (Secrets Manager, same secret
#      up.sh already required), op service-account token (Secrets Manager,
#      `./secrets.sh op <file>` — optional), github token (1Password
#      Canopy-Shared/github-token — optional)
#   3. retire every OTHER non-retired cloud runner with the same name (the
#      predecessor this box replaces)
#   4. for every agent that already has an assignment list: replace the
#      predecessor's row with the new runner id (preserving rank + enabled),
#      or append the new runner at the tail if the predecessor wasn't in it
#   5. --drill: fire a readiness drill on the new runner and poll to
#      completion, printing the pass/fail grid
#
# Pure curl + python3 — no deps, matching up.sh/down.sh/secrets.sh's house style.
set -euo pipefail
cd "$(dirname "$0")"

BASE_URL="https://labs.connect.dimagi.com/canopy"
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"
RUNNER_ID=""
RUNNER_NAME="cloud-ec2-1"
AGENTS=""     # comma-separated slug allowlist; empty = every agent with assignments
DRILL=0
AWS_PROFILE_="${AWS_PROFILE:-labs}"
AWS_REGION_="${AWS_REGION:-us-east-1}"

usage() {
  cat <<'USAGE'
usage: ./wire.sh [options]

  --runner-id <uuid>     skip discovery; wire this specific runner id
  --base-url <url>       canopy-web base URL (default https://labs.connect.dimagi.com/canopy)
  --runner-name <name>   Runner.name to match when discovering / retiring (default cloud-ec2-1)
  --agents <a,b,c>       only touch these agents' assignment lists (default: every
                         agent that currently has ANY runner assignment)
  --drill                fire a readiness drill on the new runner, poll to completion, print the grid
  -h, --help             this
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner-id) RUNNER_ID="$2"; shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --runner-name) RUNNER_NAME="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --drill) DRILL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

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

# ── Step 1: discover (or accept) the fresh runner ───────────────────────────────
if [[ -z "$RUNNER_ID" ]]; then
  echo ">> waiting for a fresh '$RUNNER_NAME' cloud runner (paired, not yet online)…"
  for _ in $(seq 1 60); do
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
# status, to survive the standard down.sh && up.sh && wire.sh recycle.
for r in rows:
    if r['kind'] == 'cloud' and r['name'] == '$RUNNER_NAME' and not r.get('last_heartbeat_at'):
        print(r['id']); break
")
    [[ -n "$RUNNER_ID" ]] && break
    sleep 5
  done
  [[ -n "$RUNNER_ID" ]] || {
    echo "!! timed out (5 min) waiting for a fresh cloud runner named '$RUNNER_NAME' — is up.sh running / cloud-init done?" >&2
    exit 1
  }
fi
echo "==> runner: $RUNNER_ID"

# ── Step 2: credential bundle ────────────────────────────────────────────────────
echo ">> staging credential bundle"
CLAUDE_TOKEN=$(aws --profile "$AWS_PROFILE_" --region "$AWS_REGION_" \
  secretsmanager get-secret-value --secret-id canopy/cloud-runner/claude-oauth-token \
  --query SecretString --output text)
OP_SA_TOKEN=$(aws --profile "$AWS_PROFILE_" --region "$AWS_REGION_" \
  secretsmanager get-secret-value --secret-id canopy/cloud-runner/op-service-account-token \
  --query SecretString --output text 2>/dev/null) || OP_SA_TOKEN=""
GITHUB_TOKEN=$(op read "op://Canopy-Shared/github-token/credential" 2>/dev/null) || GITHUB_TOKEN=""

[[ -n "$OP_SA_TOKEN" ]] || echo "   (no op-service-account-token secret — bootstrap_agents.sh's \`canopy provision\` / gmail-token steps will skip)"
[[ -n "$GITHUB_TOKEN" ]] || echo "   (no Canopy-Shared/github-token in 1Password — private per-agent clones will fail)"

CLAUDE_TOKEN="$CLAUDE_TOKEN" OP_SA_TOKEN="$OP_SA_TOKEN" GITHUB_TOKEN="$GITHUB_TOKEN" python3 -c "
import json, os
body = {'claude_token': os.environ['CLAUDE_TOKEN']}
if os.environ.get('OP_SA_TOKEN'):
    body['op_sa_token'] = os.environ['OP_SA_TOKEN']
if os.environ.get('GITHUB_TOKEN'):
    body['github_token'] = os.environ['GITHUB_TOKEN']
json.dump(body, open('$TMP/cred.json', 'w'))
"
api POST "/api/harness/runners/${RUNNER_ID}/credential" "$TMP/cred.json" | python3 -m json.tool
echo "==> credential staged"

# ── Step 3: retire predecessors ──────────────────────────────────────────────────
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
echo ">> swapping agent assignments -> $RUNNER_ID"
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
  rm -f "$TMP/put-${slug}.json"  # no stale file from a previous slug can leak into this PUT
  if ! api GET "/api/agents/${slug}/runners" > "$TMP/rows-${slug}.json" 2>/dev/null; then
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
done

# ── Step 5: optional drill wave ──────────────────────────────────────────────────
if [[ "$DRILL" == "1" ]]; then
  echo ">> firing readiness drill on $RUNNER_ID"
  echo '{}' > "$TMP/drill.json"
  api POST "/api/harness/runners/${RUNNER_ID}/drill" "$TMP/drill.json" > "$TMP/drill-result.json"
  python3 -c "
import json
rows = json.load(open('$TMP/drill-result.json'))
if isinstance(rows, dict) and rows.get('detail'):
    raise SystemExit('drill start failed: ' + rows['detail'])
print(f'   started {len(rows)} drill(s)')
"
  echo ">> polling for completion (up to 5 min)"
  for _ in $(seq 1 60); do
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
import json
rows = json.load(open('$TMP/drills.json'))
for r in sorted(rows, key=lambda r: r['agent_slug']):
    mark = {'pass': 'PASS', 'fail': 'FAIL', 'pending': 'PENDING (timed out waiting)'}[r['outcome']]
    print(f\"   {r['agent_slug']:10s} {mark:28s} {r['summary'][:80]}\")
"
fi

echo "==> wired: $RUNNER_ID"
