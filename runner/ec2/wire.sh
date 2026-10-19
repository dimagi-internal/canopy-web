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
# --test (for a box `up.sh --test` made: directed-only) stops after step 2 and the
# probe wait: a test box takes only turns pinned to it and chats bound to it, so
# there is no routing to change — no predecessor retired, no assignment row, rule
# or workspace order touched, and no --agents. It still waits for a staged
# credential to answer (a test box with a dead token tests nothing), then prints
# how to point a chat at the box.
#
# --unit <id> wires ONE isolation unit of a box of units (up.sh --units): its runner
# is `<name>-<id>` (the box's own tree unit is just `--name <box>`), found through the
# box's one invitation. Its Claude credentials default to a copy of the box's own
# unit's logins — long-lived tokens copy across units; a full login does not (its
# refresh token rotates), so a unit that wants one signs in on its own runner page.
#
# Pure curl + python3 — no deps, matching up.sh/down.sh/secrets.sh's house style.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source ./_lib.sh

BASE_URL="https://canopy.dimagi.com"
TOKEN_FILE="$HOME/.claude/canopy/workbench-token"
RUNNER_ID=""
RUNNER_NAME="$DEFAULT_RUNNER_NAME"
AGENTS=""     # comma-separated slug allowlist; empty = every agent with assignments
DRILL=0
STANDBY=0
TEST=0
CRED_FROM=""  # runner name|id whose Claude logins to copy (see step 2)
UNIT=""       # an isolation unit of the box --name names (up.sh --units)
DISCOVER_MINUTES=15   # cloud-init (node, claude, aws, gh, op) takes 4-6 min before the first pair
BOOTSTRAP_MINUTES=40  # first bootstrap clones + provisions every registered agent
PROBE_MINUTES=5       # how long to wait for the box to prove one staged credential answers
POLL_SECONDS="${WIRE_POLL_SECONDS:-10}"
AWS_PROFILE_="${AWS_PROFILE:-labs}"
AWS_REGION_="${AWS_REGION:-us-east-1}"

usage() {
  cat <<'USAGE'
usage: ./wire.sh [options]

  --runner-id <uuid>     skip discovery; wire this specific runner id
  --base-url <url>       canopy-web base URL (default https://canopy.dimagi.com)
  --name <name>          Runner.name to match when discovering / retiring (default cloud-ec2-1;
                         --runner-name is the same flag)
  --agents <a,b,c>       only touch these agents' assignment lists (default: every
                         agent that currently has ANY runner assignment)
  --standby              add the runner as a DISABLED row to --agents (required) and touch
                         nothing else: no retire, no swap, no rule/order edit. For a second
                         box you want to prove (with --drill) without routing work to it.
  --test                 a directed-only test box (up.sh --test): discover it, stage its
                         credentials, wait for one to answer (--probe-minutes) and stop — no
                         routing change of any kind, no --agents. Prints how to point a chat
                         at it.
  --drill                wait for the box's first bootstrap, fire a readiness drill, poll to
                         completion, print the grid; exits non-zero unless every drill passes
  --unit <id>            one isolation unit of the box --name names (up.sh --units): wires the
                         runner <name>-<id>, found through the box's invitation, and copies the
                         box's own unit's Claude logins to it unless --credential-from says else
  --credential-from <runner>
                         copy the Claude logins (primary, fallback, API key) from this
                         runner (name or id). Default: the same-name predecessor, else the
                         live cloud box (online, ready, not paused, latest heartbeat), else
                         Secrets Manager's bootstrap token
  --discover-minutes <n> how long to wait for the fresh box to pair (default 15)
  --probe-minutes <n>    after staging, how long to wait for the box to prove a credential
                         answers a prompt (default 5); exits non-zero, before any routing
                         change, when none does. 0 skips the wait
  -h, --help             this
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --runner-id) RUNNER_ID="$2"; shift 2 ;;
    --base-url) BASE_URL="${2%/}"; shift 2 ;;
    --runner-name|--name) RUNNER_NAME="$2"; shift 2 ;;
    --standby) STANDBY=1; shift ;;
    --test) TEST=1; shift ;;
    --credential-from) CRED_FROM="$2"; shift 2 ;;
    --unit) UNIT="$2"; shift 2 ;;
    --discover-minutes) DISCOVER_MINUTES="$2"; shift 2 ;;
    --probe-minutes) PROBE_MINUTES="$2"; shift 2 ;;
    --agents) AGENTS="$2"; shift 2 ;;
    --shared-vault) echo "   (--shared-vault is ignored: wire.sh no longer reads a shared GitHub token)"; shift 2 ;;
    --drill) DRILL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

runner_name_ok "$RUNNER_NAME" || { echo "!! invalid runner name '$RUNNER_NAME'" >&2; exit 1; }
# The box (its stack and its one invitation) and, with --unit, the unit's own runner.
BOX_NAME="$RUNNER_NAME"
if [[ -n "$UNIT" ]]; then
  [[ "$UNIT" =~ ^[a-z0-9][a-z0-9-]{0,27}$ ]] || { echo "!! invalid unit id '$UNIT'" >&2; exit 1; }
  RUNNER_NAME="${BOX_NAME}-${UNIT}"
  runner_name_ok "$RUNNER_NAME" || { echo "!! '$RUNNER_NAME' is not a valid runner name" >&2; exit 1; }
  # Tokens copy across units: the box's own unit is the natural source.
  [[ -n "$CRED_FROM" ]] || CRED_FROM="$BOX_NAME"
fi
if [[ "$TEST" == "1" && ( "$STANDBY" == "1" || -n "$AGENTS" || "$DRILL" == "1" ) ]]; then
  echo "!! --test changes no routing: it takes no --standby, --agents or --drill" >&2
  exit 1
fi
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

# Every agent the token can see, as {"items": [...]}. The flat list first; a
# token whose workspaces span more than one tenant tree gets a 422 there
# (canopy-web's read rule, 2026-10-11), so then ask each workspace by name.
agents_json() {
  local flat
  flat="$(api GET "/api/agents/?limit=200")"
  if printf '%s' "$flat" | python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(sys.stdin).get("items"), list) else 1)' 2>/dev/null; then
    printf '%s\n' "$flat"
    return 0
  fi
  local ws
  for ws in $(api GET "/api/workspaces/" | python3 -c 'import json,sys; print(" ".join(w["slug"] for w in json.load(sys.stdin)))'); do
    api GET "/api/w/${ws}/agents/?limit=200"
    echo
  done | python3 -c '
import json, sys
seen = {}
for line in sys.stdin:
    try:
        d = json.loads(line)
    except Exception:
        continue
    for a in d.get("items") or []:
        seen.setdefault(a["slug"], a)
print(json.dumps({"items": list(seen.values())}))
'
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
                    f"or run `canopy agent vault --slug {slug} --workspace {ws}` (needs 1Password rights)")
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
# A box paired by invitation names itself: up.sh records the invitation in
# <stack>-invitation.json, and canopy-web says which runner consumed it.
INVITE_FILE="${KEY_DIR:-.}/$(stack_for_name "$BOX_NAME")-invitation.json"
if [[ -z "$RUNNER_ID" && -f "$INVITE_FILE" ]]; then
  INV_ID=$(python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get("id") or "")
except Exception:
    print("")' "$INVITE_FILE")
  if [[ -n "$INV_ID" ]]; then
    api GET "/api/harness/runner-invitations/${INV_ID}" > "$TMP/invitation.json" || true
    INV=$(BOX="$BOX_NAME" NAME="$RUNNER_NAME" python3 - "$TMP/invitation.json" <<'PY'
import json, os, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
if not isinstance(d, dict) or d.get("name") != os.environ["BOX"]:
    print("unknown -")
else:
    # A box of units: this unit's runner, by its name, from the one invitation.
    units = {u.get("name"): u.get("runner_id") for u in d.get("units") or []}
    rid = units.get(os.environ["NAME"]) if units else d.get("runner_id")
    print(d.get("status") or "unknown", rid or "-")
PY
)
    if [[ "${INV%% *}" == "consumed" && "${INV#* }" != "-" ]]; then
      RUNNER_ID="${INV#* }"
      echo ">> runner from invitation $INV_ID ($INVITE_FILE)"
    else
      echo "   invitation $INV_ID is ${INV%% *}, not consumed — discovering instead"
    fi
  fi
fi
if [[ -z "$RUNNER_ID" ]]; then
  echo ">> waiting up to ${DISCOVER_MINUTES} min for a fresh '$RUNNER_NAME' cloud runner (paired, no credential staged)…"
  for _ in $(seq 1 $(( DISCOVER_MINUTES * 12 ))); do
    api GET /api/harness/runners/ > "$TMP/runners.json"
    # The fresh box is the one with NO CREDENTIAL STAGED: on boot it pairs
    # (creating this row) and waits in fetch_and_stage_credential for the bundle
    # we are about to POST. It used to be found as "never heartbeated", which
    # stopped being true when that wait started heartbeating (online, not ready),
    # and a paired-but-unstaged cloud-ec2-fork was never discovered (2026-10-18).
    # A dead predecessor left by down.sh --keep-runner has its bundle staged, so
    # it is never mistaken for the new box. Most recent heartbeat first.
    # Live only: online, and heartbeating within the last 2 minutes (the wait
    # beats every 15 s). A dead unstaged row — a box that paired and died — must
    # not be taken for the one that is waiting now.
    CANDIDATES=$(python3 -c "
import datetime as dt, json
rows = json.load(open('$TMP/runners.json'))
now = dt.datetime.now(dt.timezone.utc)
def fresh(r):
    raw = r.get('last_heartbeat_at') or ''
    try:
        at = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return False
    return (now - at).total_seconds() <= 120
hits = [r for r in rows if r.get('kind') == 'cloud' and r.get('name') == '$RUNNER_NAME'
        and r.get('status') == 'online' and fresh(r)]
hits.sort(key=lambda r: r.get('last_heartbeat_at') or '', reverse=True)
print(' '.join(r['id'] for r in hits))
" 2>/dev/null || true)
    for cid in $CANDIDATES; do
      api GET "/api/harness/runners/${cid}/credential/status" > "$TMP/cand-status.json" 2>/dev/null || true
      if python3 - "$TMP/cand-status.json" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if not isinstance(s, dict) or "has_claude_token" not in s:
    sys.exit(1)   # unreadable: not evidence the box is unstaged
staged = (s.get("has_claude_token") or s.get("has_claude_token_secondary") or s.get("has_claude_api_key")
          or any(lg.get("signed_in") for lg in s.get("logins") or []))
sys.exit(1 if staged else 0)
PY
      then
        RUNNER_ID="$cid"
        break
      fi
    done
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
# WHERE the Claude logins come from, in order:
#   1. --credential-from <runner name|id>
#   2. the predecessor this box replaces (a same-name runner)
#   3. the live cloud box: not retired, online, ready, not paused, latest heartbeat.
#      Not a box by name: defaulting to cloud-ec2-1 copied a retired box's tokens
#      onto cloud-ec2-fork (2026-10-18).
#   4. Secrets Manager canopy/cloud-runner/claude-oauth-token — the BUILD FALLBACK
# Full logins never copy: their refresh token rotates, so a copy breaks both. What
# was copied and what stayed behind is printed, with the link to add a login.
# By design the fleet's logins live on the runner page (/supervisor → Runners →
# Claude login, primary + fallback), and a new box inherits them from a live one:
# copying the whole bundle (both logins, the API key, their labels) server-to-server
# keeps it on what the fleet actually runs on. Step 4 exists only for a fleet with
# no live cloud runner to copy from; that secret is rarely touched, so it being old
# or at its usage cap is expected — the box runs on whatever the runner page sets.
# The values pass through this process only, never a file outside $TMP or the screen.
echo ">> staging credential bundle"
api GET /api/harness/runners/ > "$TMP/runners-cred.json"
CRED_PICK=$(FROM="$CRED_FROM" NAME="$RUNNER_NAME" NEW="$RUNNER_ID" \
  python3 - "$TMP/runners-cred.json" <<'PY'
import json, os, sys
rows = [r for r in json.load(open(sys.argv[1])) if r.get("kind") == "cloud"
        and r.get("status") != "retired" and r["id"] != os.environ["NEW"]]
want = os.environ["FROM"]
def pick(match):
    # Most recently heartbeating first: a live box's logins over a dead one's.
    hits = sorted((r for r in rows if match(r)), key=lambda r: r.get("last_heartbeat_at") or "", reverse=True)
    return hits[0]["id"] + " " + hits[0]["name"] if hits else ""
def live(r):
    return (r.get("status") == "online" and r.get("ready", True) is not False
            and not r.get("paused"))
if want:
    print((pick(lambda r: r["id"] == want or r["name"] == want) or "MISSING") + "|--credential-from")
else:
    same = pick(lambda r: r["name"] == os.environ["NAME"])
    if same:
        print(same + "|the predecessor this box replaces")
    else:
        box = pick(live)
        print(box + "|the live cloud box (online, ready, not paused, latest heartbeat)" if box else "|")
PY
)
CRED_SRC="${CRED_PICK%%|*}"
CRED_WHY="${CRED_PICK#*|}"
if [[ "$CRED_SRC" == "MISSING" ]]; then
  echo "!! --credential-from '$CRED_FROM': no such (non-retired, cloud) runner" >&2
  exit 1
fi
STAGED_FROM=""
if [[ -n "$CRED_SRC" ]]; then
  SRC_ID="${CRED_SRC%% *}"
  api GET "/api/harness/runners/${SRC_ID}/credential" > "$TMP/src-cred.json"
  api GET "/api/harness/runners/${SRC_ID}/credential/status" > "$TMP/src-status.json"
  if python3 - "$TMP/src-cred.json" "$TMP/src-status.json" "$TMP/cred.json" <<'PY'
import json, os, sys
os.umask(0o077)
cred, status = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
if not isinstance(cred, dict) or not cred.get("claude_token"):
    sys.exit(1)
out = {k: cred[k] for k in ("claude_token", "claude_token_secondary", "claude_api_key") if cred.get(k)}
for k in ("claude_token_label", "claude_token_secondary_label"):
    if status.get(k):
        out[k] = status[k]
json.dump(out, open(sys.argv[3], "w"))
PY
  then
    STAGED_FROM="runner ${CRED_SRC#* } ($SRC_ID) — ${CRED_WHY}"
  else
    echo "   WARN: could not read a Claude login from ${CRED_SRC#* } — falling back to Secrets Manager"
  fi
fi
if [[ -z "$STAGED_FROM" ]]; then
  # Nothing to copy from: no point listing a source's logins below.
  rm -f "$TMP/src-status.json"
  CLAUDE_TOKEN=$(aws --profile "$AWS_PROFILE_" --region "$AWS_REGION_" \
    secretsmanager get-secret-value --secret-id canopy/cloud-runner/claude-oauth-token \
    --query SecretString --output text)
  CLAUDE_TOKEN="$CLAUDE_TOKEN" python3 -c "
import json, os
os.umask(0o077)
json.dump({'claude_token': os.environ['CLAUDE_TOKEN']}, open('$TMP/cred.json', 'w'))
"
  STAGED_FROM="Secrets Manager canopy/cloud-runner/claude-oauth-token"
fi
echo "   from: $STAGED_FROM"
# What was copied, by name — never a value.
python3 - "$TMP/cred.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
got = []
for key, label, name in (("claude_token", "Token 1", "claude_token_label"),
                         ("claude_token_secondary", "Token 2", "claude_token_secondary_label"),
                         ("claude_api_key", "the API key", "")):
    if c.get(key):
        got.append(label + (f" ({c[name]})" if name and c.get(name) else ""))
print("   copied: " + (", ".join(got) if got else "nothing"))
PY
api POST "/api/harness/runners/${RUNNER_ID}/credential" "$TMP/cred.json" > "$TMP/staged.json"
python3 -m json.tool < "$TMP/staged.json" || cat "$TMP/staged.json"
# The staging moment, on the SERVER's clock (the bundle's updated_at): the probe
# wait below counts only probes taken after it, so a result left from an earlier
# staging (--runner-id on a running box) cannot pass.
STAGED_AT=$(python3 - "$TMP/staged.json" <<'PY'
import datetime as dt, json, sys
try:
    raw = (json.load(open(sys.argv[1])) or {}).get("updated_at") or ""
    at = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
except Exception:
    at = dt.datetime.now(dt.timezone.utc)
print(at.astimezone(dt.timezone.utc).isoformat())
PY
)
rm -f "$TMP/cred.json" "$TMP/src-cred.json"
CREDENTIALS_URL="${BASE_URL}/supervisor?tab=runners&runner=${RUNNER_ID}"
# …and what could not be: the source's full logins stay where they were made.
if [[ -s "$TMP/src-status.json" ]]; then
  python3 - "$TMP/src-status.json" <<'PY'
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    s = {}
for lg in (s.get("logins") or []) if isinstance(s, dict) else []:
    if lg.get("signed_in"):
        print(f"   not copied: {lg.get('account') or 'a full login'} — full logins stay on the box they were made on")
PY
fi
echo "==> credential staged — to use claude.ai connectors, + Add a login on its credentials: $CREDENTIALS_URL"

# ── Prove a credential works, before anything routes to it ──────────────────────
# A stored value is not a working one: cloud-ec2-fork was wired onto a token at
# its weekly cap and failed every turn as "not signed in" (2026-10-18). The box
# asks each credential one short question after staging (its claude.probe.<label>
# health checks); nothing below runs until one has answered.
if [[ "$PROBE_MINUTES" != "0" ]]; then
  echo ">> waiting up to ${PROBE_MINUTES} min for the box to prove a Claude credential works…"
  # Reads the runner row: `state` prints none|ok|bad over the probes taken AFTER
  # the staging moment (each check's detail ends "· probed <ISO>", on the box's
  # clock; the row's health_received_at is the server's), `show` prints them.
  cat > "$TMP/probes.py" <<'PY'
import datetime as dt, json, re, sys
mode, path, rid, staged = sys.argv[1:5]
SLACK = dt.timedelta(seconds=5)
def when(raw):
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
staged_at = when(staged)
try:
    rows = json.load(open(path))
    r = next((x for x in rows if x.get("id") == rid), {})
except Exception:
    r = {}
received = when(r.get("health_received_at") or "")
fresh = {}
if not (received and staged_at and received < staged_at - SLACK):
    for name, v in sorted((r.get("health_checks") or {}).items()):
        if not name.startswith("claude.probe."):
            continue
        m = re.search(r"probed (\S+)\s*$", v.get("detail") or "")
        taken = when(m.group(1)) if m else None
        if taken and staged_at and taken >= staged_at - SLACK:
            fresh[name[len("claude.probe."):]] = v
if mode == "show":
    for label, v in fresh.items():
        print(f"   {label:22s} {v.get('status', '?'):5s} {v.get('detail', '')}")
elif not fresh:
    print("none")
else:
    print("ok" if any(v.get("status") == "ok" for v in fresh.values()) else "bad")
PY
  PROBE_STATE="none"
  for _ in $(seq 1 $(( PROBE_MINUTES * 6 ))); do
    api GET /api/harness/runners/ > "$TMP/runner-probe.json" 2>/dev/null || true
    PROBE_STATE=$(python3 "$TMP/probes.py" state "$TMP/runner-probe.json" "$RUNNER_ID" "$STAGED_AT")
    [[ "$PROBE_STATE" == "ok" ]] && break
    sleep "$POLL_SECONDS"
  done
  python3 "$TMP/probes.py" show "$TMP/runner-probe.json" "$RUNNER_ID" "$STAGED_AT"
  if [[ "$PROBE_STATE" != "ok" ]]; then
    if [[ "$PROBE_STATE" == "none" ]]; then
      echo "!! the box reported no credential probe taken since staging (${STAGED_AT}) within ${PROBE_MINUTES} min." >&2
    else
      echo "!! no staged Claude credential answered a prompt." >&2
    fi
    echo "!! Nothing was routed to it. + Add a login (or set a token) on its credentials, then re-run:" >&2
    echo "!!   $CREDENTIALS_URL" >&2
    exit 3
  fi
  echo "==> a Claude credential answered"
fi

# ── --test: a directed-only box has no routing to change — stop here ───────────
if [[ "$TEST" == "1" ]]; then
  api GET /api/harness/runners/ > "$TMP/runners-test.json"
  TEST_WS=$(python3 -c "
import json
rows = json.load(open('$TMP/runners-test.json'))
r = next((x for x in rows if isinstance(x, dict) and x.get('id') == '$RUNNER_ID'), {})
print(r.get('workspace') or '')
")
  CHAT_URL="${BASE_URL}/w/${TEST_WS:-<workspace>}/chat"
  echo "==> test box ready: $RUNNER_ID ($RUNNER_NAME) — directed only, nothing routed to it"
  echo "    point a chat at it: $CHAT_URL → New chat → pick a repo → Run on $RUNNER_NAME"
  echo "    tear down: ./down.sh --name $RUNNER_NAME"
  exit 0
fi

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
    # Only the drills THIS run started: the list also holds every earlier drill
    # of this runner, and an old PASS must not stand in for one still running.
    PENDING=$(python3 -c "
import json
started = {r['agent_slug'] for r in json.load(open('$TMP/drill-result.json'))}
rows = [r for r in json.load(open('$TMP/drills.json')) if r['agent_slug'] in started]
print(sum(1 for r in rows if r['outcome'] == 'pending'))
")
    [[ "$PENDING" == "0" ]] && break
    sleep 5
  done
  echo ">> drill grid:"
  python3 -c "
import json, sys
started = {r['agent_slug'] for r in json.load(open('$TMP/drill-result.json'))}
rows = [r for r in json.load(open('$TMP/drills.json')) if r['agent_slug'] in started]
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
  agents_json > "$TMP/agents.json"
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
