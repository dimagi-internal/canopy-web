#!/usr/bin/env bash
#
# Should this cloud box install newer runner bytes right now?
#
#   canopy-runner-update               # the timer's mode: install iff stale + idle
#   canopy-runner-update --check       # print the verdict, change nothing
#   canopy-runner-update --ref BRANCH  # install something else, deliberately
#   canopy-runner-update --from-secret # restore the Secrets Manager bytes
#
# The cloud half of the auto-update story the laptop runner already has
# (runner/canopy_runner/canopy_runner/update.py + install-runner.sh --if-stale).
# The question has the same three parts, each answered by whoever actually knows:
#
#   * WHAT IS INSTALLED — /opt/canopy-runner/build-info.json, locally. Deliberately
#     NOT the server's record of code_sha: that is only as fresh as the last
#     heartbeat, and a runner crash-looping (the case where auto-update matters
#     MOST) has not sent one.
#   * WHAT SHOULD BE INSTALLED — `expected_code_sha` off this runner's own row.
#     That is the cloud runner source in the DEPLOYED image, so it has already been
#     through CI, the merge queue and a deploy. Tracking origin/main instead would
#     install code nothing has deployed AND leave the box permanently mismatched
#     against the server — the staleness banner would then fire forever on exactly
#     the boxes auto-updating correctly.
#   * WHETHER NOW IS SAFE — /opt/canopy-runner/in-flight, rewritten by the running
#     daemon on every heartbeat. An update restarts the service; restarting mid-turn
#     strands the work.
#
# READ-ONLY against the control plane, and that is load-bearing: this must never
# heartbeat. A heartbeat from this second process would stamp the runner ONLINE and
# overwrite the provenance the real daemon reports — forging liveness for a daemon
# that may be dead.
#
# Runs as the SERVICE USER (ubuntu), not root. /opt/canopy-runner and
# /opt/canopy-web are both ubuntu-owned, so nothing here needs privilege except the
# restart itself — and root git-ing around in a directory agent turns can write
# would both trip git's dubious-ownership guard and hand root an attack surface for
# no benefit. The one privileged step goes through a sudoers rule scoped to exactly
# `systemctl restart canopy-runner.service`.
#
# A SEPARATE unit from the runner, not a thread inside it: a runner that
# crash-loops can never update itself — it never heartbeats, never learns it is
# behind, and stays bricked until a human notices. An independent timer means
# shipping a fix is enough to rescue the box.
#
# See docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md.
set -uo pipefail

RUNNER_HOME="${RUNNER_HOME:-/opt/canopy-runner}"
ENV_FILE="${ENV_FILE:-$RUNNER_HOME/runner.env}"
TARGET="$RUNNER_HOME/cloud_runner.py"
STAMP="$RUNNER_HOME/build-info.json"
BUSY="$RUNNER_HOME/in-flight"
REPO_DIR="${CANOPY_WEB_REPO_DIR:-/opt/canopy-web}"
REPO_URL="${CANOPY_WEB_REPO_URL:-https://github.com/dimagi-internal/canopy-web.git}"
STATE_FILE="${STATE_FILE:-$HOME/.canopy-cloud-runner.json}"
SERVICE="${SERVICE:-canopy-runner.service}"
RESTART_CMD="${RESTART_CMD:-sudo -n systemctl restart}"
CODE_SECRET="${CODE_SECRET:-canopy/cloud-runner/runner-code}"
RUNNER_SRC="runner/ec2/cloud_runner.py"
# The paths the DEPLOYED sha is computed over (deploy-labs.yml's cloud_sha step).
# Kept identical here so a stamp written by this script names the same commit the
# server expects — bootstrap_agents.sh included, because a restart is how it ships.
SHA_PATHS=("runner/ec2/cloud_runner.py" "runner/ec2/bootstrap_agents.sh")
# The daemon rewrites the busy marker every heartbeat (20s). Older than this means
# it is not running its loop at all — stopped, wedged, or crash-looping. That is
# NOT busy: it is the case this script exists to rescue, so it must never block.
BUSY_MAX_AGE=120

MODE="update"
REF=""

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    --ref) REF="${2:?--ref needs a git ref}"; MODE="pinned"; shift 2 ;;
    --from-secret) MODE="secret"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { echo "$(date -u +%FT%TZ) canopy-runner-update: $*"; }

# --- what is installed ------------------------------------------------------
json_field() {  # <file> <key> <default>
  python3 - "$1" "$2" "$3" <<'PY' 2>/dev/null || echo "$3"
import json, sys
path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as fh:
        print(json.load(fh).get(key) or default)
except Exception:
    print(default)
PY
}

INSTALLED_SHA="$(json_field "$STAMP" sha "")"

# --- whether now is safe ----------------------------------------------------
in_flight() {  # echoes a count, or "" when the marker cannot be trusted
  python3 - "$BUSY" "$BUSY_MAX_AGE" <<'PY' 2>/dev/null
import json, sys, time
path, max_age = sys.argv[1], float(sys.argv[2])
try:
    with open(path) as fh:
        raw = json.load(fh)
    if time.time() - float(raw["at"]) > max_age:
        sys.exit(0)          # stale marker: unknown, and unknown must not block
    print(int(raw["count"]))
except Exception:
    pass
PY
}

# --- what should be installed ----------------------------------------------
expected_sha() {
  # GET the fleet list and pick our own row. Read-only by construction: there is no
  # POST anywhere in this script, deliberately (see the header).
  local base token rid
  [ -r "$ENV_FILE" ] || { echo ""; return; }
  base="$(sed -n 's/^CANOPY_BASE_URL=//p' "$ENV_FILE" | tail -1)"
  token="$(sed -n 's/^CANOPY_TOKEN=//p' "$ENV_FILE" | tail -1)"
  rid="$(json_field "$STATE_FILE" runner_id "")"
  [ -n "$base" ] && [ -n "$token" ] && [ -n "$rid" ] || { echo ""; return; }
  # `python3 -c`, NOT `python3 - <<HEREDOC`: a heredoc redirects stdin, so it would
  # silently win over the pipe and the parser would read the PROGRAM instead of the
  # response — every box answering `unknown`, forever, with nothing in the log.
  curl -fsS --max-time 30 -H "Authorization: Bearer $token" \
      "${base%/}/api/harness/runners/" 2>/dev/null \
    | python3 -c '
import json, sys
rid = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = rows.get("items", rows) if isinstance(rows, dict) else rows
for row in rows or []:
    if str(row.get("id")) == rid:
        print((row.get("expected_code_sha") or "").strip())
        break
' "$rid" 2>/dev/null
}

# --- verdict ----------------------------------------------------------------
# current | stale | busy | unknown — the same four the laptop's update.py returns,
# and unknown covers EITHER side being empty for the same reason it does there: a
# dev server bakes in no expectation, and installing an empty sha would be a
# reinstall loop against a target that does not exist.
verdict() {
  local expected="$1" carrying
  if [ -z "$expected" ] || [ -z "$INSTALLED_SHA" ]; then echo "unknown"; return; fi
  if [ "$expected" = "$INSTALLED_SHA" ]; then echo "current"; return; fi
  carrying="$(in_flight)"
  if [ -n "$carrying" ] && [ "$carrying" -gt 0 ] 2>/dev/null; then echo "busy"; return; fi
  echo "stale"
}

# --- install ----------------------------------------------------------------
fetch_commit() {  # make <ref> resolvable in the clone
  local ref="$1"
  if [ ! -d "$REPO_DIR/.git" ]; then
    git clone --quiet "$REPO_URL" "$REPO_DIR" || return 1
  fi
  git -C "$REPO_DIR" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null && return 0
  # The bootstrap clone is --depth 1, so an older commit simply is not here yet.
  # Ask for that object by name first (GitHub serves reachable shas); deepen only
  # if it refuses, because unshallowing this repo is not free.
  git -C "$REPO_DIR" fetch --quiet origin "$ref" 2>/dev/null \
    || git -C "$REPO_DIR" fetch --quiet --unshallow origin 2>/dev/null \
    || git -C "$REPO_DIR" fetch --quiet origin main 2>/dev/null
  git -C "$REPO_DIR" rev-parse --verify --quiet "${ref}^{commit}" >/dev/null
}

install_bytes() {  # <candidate> <sha> <committed_at> <ref-label>
  local candidate="$1" sha="$2" committed_at="$3" ref="$4"
  # systemd's ExecStart points straight at this file, so a truncated or malformed
  # copy is a box that will not boot. Parse it before it can ever be started — the
  # counterpart of `plutil -lint`ing a plist before going near the running job.
  # `ast.parse` rather than py_compile so this leaves no __pycache__ behind.
  if ! python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$candidate" 2>/dev/null; then
    log "REFUSING to install: $candidate does not parse. The running runner is untouched."
    rm -f "$candidate"
    return 1
  fi
  chmod 0755 "$candidate" || return 1
  mv "$candidate" "$TARGET" || return 1
  python3 - "$STAMP" "$sha" "$committed_at" "$ref" <<'PY'
import json, sys, time
path, sha, committed_at, ref = sys.argv[1:5]
with open(path, "w") as fh:
    json.dump({"sha": sha, "committed_at": int(committed_at or 0),
               "installed_at": int(time.time()), "ref": ref}, fh)
PY
  log "installed ${sha:-<unknown>} ($ref) -> $TARGET; restarting $SERVICE"
  # The only privileged step, and the only reason this box grants the service user
  # any sudo at all — scoped to this exact command by a sudoers drop-in.
  $RESTART_CMD "$SERVICE"
}

install_from_git() {  # <git ref or sha>
  local ref="$1" sha committed_at tmp
  fetch_commit "$ref" || {
    # In timer mode this is survivable and self-correcting: the sha may simply not
    # have reached this clone yet. Wait for the next cycle rather than shouting
    # every 30 minutes.
    log "ref $ref is not in $REPO_DIR yet; will retry."
    return 1
  }
  sha="$(git -C "$REPO_DIR" log -1 --format=%H "$ref" -- "${SHA_PATHS[@]}")"
  committed_at="$(git -C "$REPO_DIR" log -1 --format=%ct "$ref" -- "${SHA_PATHS[@]}")"
  tmp="$TARGET.new"
  # `git show`, never a checkout: agent turns run in this clone and one may have
  # left it on a branch. Reading the blob out of the object store cannot be
  # affected by that — it is the working-tree coupling that bit the laptop three
  # times before the runner became an installed package.
  if ! git -C "$REPO_DIR" show "$ref:$RUNNER_SRC" > "$tmp" 2>/dev/null; then
    log "ref $ref has no $RUNNER_SRC; nothing installed."
    rm -f "$tmp"
    return 1
  fi
  install_bytes "$tmp" "$sha" "${committed_at:-0}" "$ref"
}

install_from_secret() {
  local tmp="$TARGET.new"
  if ! aws secretsmanager get-secret-value --secret-id "$CODE_SECRET" \
        --query SecretString --output text 2>/dev/null | base64 -d | gunzip > "$tmp"; then
    log "could not read $CODE_SECRET; nothing installed."
    rm -f "$tmp"
    return 1
  fi
  # The secret carries no provenance, so the stamp is CLEARED rather than guessed.
  # Unknown is the honest answer: those bytes are whatever an operator published,
  # and a borrowed sha would tell the fleet this box is current when it is not.
  install_bytes "$tmp" "" 0 "secret:$CODE_SECRET"
}

# --- main -------------------------------------------------------------------
case "$MODE" in
  secret)
    install_from_secret
    exit $?
    ;;
  pinned)
    log "installing $REF by hand — this box reads as stale until a deploy ships it"
    install_from_git "$REF"
    exit $?
    ;;
esac

EXPECTED="$(expected_sha)"
V="$(verdict "$EXPECTED")"

if [ "$MODE" = "check" ]; then
  echo "$V expected=${EXPECTED:-<unknown>} installed=${INSTALLED_SHA:-<unknown>}"
  exit 0
fi

case "$V" in
  stale)
    log "behind — installing ${EXPECTED:0:12} (running ${INSTALLED_SHA:0:12})"
    install_from_git "$EXPECTED"
    ;;
  *)
    # current | busy | unknown all mean "do nothing", and all exit 0. This runs on
    # a timer: a non-zero exit for the ordinary case fills the journal with false
    # failures and trains everyone to ignore the one that matters.
    log "$V — nothing to do."
    ;;
esac
exit 0
