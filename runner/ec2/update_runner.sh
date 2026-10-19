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
# A box's manually set address (cloud_runner.py BASE_URL_OVERRIDE_FILE).
# runner.env still names the stack parameter's address, which canopy-fetch-env
# re-renders on every start, so this timer must prefer the override exactly as
# the runner does — or it keeps calling the address canopy has left (it did,
# every 30 minutes, after the 2026-10-06 move).
OVERRIDE_FILE="${CANOPY_BASE_URL_OVERRIDE_FILE:-$RUNNER_HOME/base_url.override}"
TARGET="$RUNNER_HOME/cloud_runner.py"
STAMP="$RUNNER_HOME/build-info.json"
BUSY="$RUNNER_HOME/in-flight"
REPO_DIR="${CANOPY_WEB_REPO_DIR:-/opt/canopy-web}"
REPO_URL="${CANOPY_WEB_REPO_URL:-https://github.com/dimagi-internal/canopy-web.git}"
STATE_FILE="${STATE_FILE:-$HOME/.canopy-cloud-runner.json}"
SERVICE="${SERVICE:-canopy-runner.service}"
RESTART_GIVEN="${RESTART_CMD:-}"
RESTART_CMD="${RESTART_CMD:-sudo -n systemctl restart}"
# The first-boot seed (runner/ec2/seed.py): a manifest + "<id>-part-<i>" parts.
SEED_SECRET="${SEED_SECRET:-canopy/cloud-runner/runner-seed}"
# The single-value seed stacks created before 2026-10-04 were built from. up.sh no
# longer writes it; read only when there is no manifest seed to read.
CODE_SECRET="${CODE_SECRET:-canopy/cloud-runner/runner-code}"
RUNNER_SRC="runner/ec2/cloud_runner.py"
# The paths the DEPLOYED sha is computed over (deploy-labs.yml's cloud_sha step).
# Kept identical here so a stamp written by this script names the same commit the
# server expects — bootstrap_agents.sh included, because a restart is how it ships.
# update_runner.sh is in this list ON PURPOSE. The shim at
# /usr/local/bin/canopy-runner-update reads THIS script from the clone's origin/main
# without fetching, and origin/main only moves when the install path runs — which
# only happens when one of these files changes. So an updater-only change never
# reached a box: #742 merged 13:3xZ on 2026-09-10 and the 13:40Z tick still ran the
# old script. Listing it here makes an updater change a deploy, as the shim's own
# comment already claims. Keep in step with deploy-labs.yml's cloud_sha step —
# runner/ec2/tests/test_update_runner_sh.py pins the two lists equal.
SHA_PATHS=("runner/ec2/cloud_runner.py" "runner/ec2/bootstrap_agents.sh" "runner/ec2/update_runner.sh" "runner/ec2/kms_broker.py" "packages/canopy_transcript" "runner/canopy_acp")
# The daemon rewrites the busy marker every heartbeat (20s). Older than this means
# it is not running its loop at all — stopped, wedged, or crash-looping. That is
# NOT busy: it is the case this script exists to rescue, so it must never block.
BUSY_MAX_AGE=120

# --- isolation units (spec 2026-10-19-runner-isolation-units §1) -------------
# A box of units (`cloud_runner.py --setup-units` wrote units.json) runs one
# `canopy-runner@<unit>` per unit, each its own user. This script then runs as ROOT
# (canopy-runner-update.service, installed by `cloud_runner.py --install-units`):
# ONE install for every unit, deferred while ANY unit has a turn in flight, and a
# restart of every `canopy-runner@*`. The runner bytes in /opt/canopy-runner and
# the clone in /opt/canopy-web are root's there, so no unit can rewrite the code
# the others run.
UNITS_FILE="${UNITS_FILE:-/etc/canopy-runner/units.json}"
UNITS_MODE=0
if [ -r "$UNITS_FILE" ]; then
  UNITS_MODE=1
  ENV_FILE="${BOX_ENV_FILE:-/etc/canopy-runner/box.env}"
  SERVICE="${SERVICE_UNITS:-canopy-runner@*.service canopy-kms-broker.service}"
  [ -n "$RESTART_GIVEN" ] || RESTART_CMD="systemctl restart"
fi
# `unit_rows` -> "<id> <user> <home>" per unit, from units.json.
unit_rows() {
  python3 -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for u in doc.get("units") or []:
    if u.get("id") and u.get("user") and u.get("home"):
        print(u["id"], u["user"], u["home"])
' "$UNITS_FILE" 2>/dev/null
}
if [ "$UNITS_MODE" = "1" ]; then
  # The runner id (any unit's: they share an owner, so the runner list it reads
  # names every unit) and a token to read it with.
  read -r _FIRST_ID _FIRST_USER FIRST_HOME <<<"$(unit_rows | head -1)"
  STATE_FILE="${FIRST_HOME:-/nonexistent}/.canopy-cloud-runner.json"
  RUNNER_TOKEN_FILE="${FIRST_HOME:-/nonexistent}/.canopy-runner/runner-token.json"
fi

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
in_flight_of() {  # <marker> -> a count, or "" when the marker cannot be trusted
  python3 - "$1" "$BUSY_MAX_AGE" <<'PY' 2>/dev/null
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
in_flight() {  # echoes a count, or "" when no marker can be trusted
  if [ "$UNITS_MODE" != "1" ]; then
    in_flight_of "$BUSY"
    return
  fi
  # A box of units is busy while ANY unit is: the restart reaches them all.
  local total="" id user home n
  while read -r id user home; do
    [ -n "$home" ] || continue
    n="$(in_flight_of "$home/.canopy-runner/in-flight")"
    [ -n "$n" ] && total=$(( ${total:-0} + n ))
  done <<<"$(unit_rows)"
  printf '%s' "$total"
}

# The moved-to address, or nothing. https only, like the runner's own reader.
moved_base() {
  local m
  m="$(tr -d '[:space:]' < "$OVERRIDE_FILE" 2>/dev/null)"
  m="${m%/}"
  case "$m" in https://*) echo "$m" ;; esac
}

# --- this box's credential ---------------------------------------------------
# The bearer for the one read below — the SAME credential the daemon signs in with.
# Since keyless runners (#1594) that is a runner token bought with this box's device
# key (its EC2 instance role), and runner.env's CANOPY_TOKEN — a person's PAT — is
# only the fallback for a box whose key is not approved yet. This script used to
# read the PAT and nothing else: when it was revoked in the 2026-10-17 token
# cleanup the daemon carried on keyless, but every tick here got a 401, read
# `unknown`, and installed nothing — for hours, on a box the runner itself was
# nudging every ten minutes (cloud-ec2-2, 2026-10-17 23:30Z onward).
#
# Two sources, in order:
#   1. the token the running daemon SHARES (RUNNER_TOKEN_FILE, 0600, #1630) — free;
#   2. one this script BUYS with the INSTALLED runner's own exchange code. Not
#      optional: (1) is written only by runner bytes newer than #1630, and a box
#      whose PAT is dead cannot reach those bytes without this — the updater that
#      would install the token-sharing daemon needs a token to decide to. It also
#      covers the crash-looping daemon that shares nothing. Imported, never run
#      (its `__main__` would start a second daemon), with stdout diverted while it
#      imports so nothing it prints can be mistaken for a token.
RUNNER_TOKEN_FILE="${RUNNER_TOKEN_FILE:-$RUNNER_HOME/runner-token.json}"
shared_runner_token() {
  [ -r "$RUNNER_TOKEN_FILE" ] || return 0
  python3 -c '
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
if (d.get("access_token") or "") and float(d.get("expires_at") or 0) - time.time() > 120:
    print(d["access_token"])
' "$RUNNER_TOKEN_FILE" 2>/dev/null
}

MINT_HELPER='
import contextlib, importlib.util, sys
target, base, rid = sys.argv[1:4]
try:
    with contextlib.redirect_stdout(sys.stderr):
        spec = importlib.util.spec_from_file_location("installed_cloud_runner", target)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        exchange = getattr(m, "_exchange_device_proof", None)
        if exchange is None:
            sys.exit(0)
        m._log = lambda msg: print(msg, file=sys.stderr)
        m.BASE_URL = base.rstrip("/")
        ok = exchange(rid)
    if ok:
        print(m._RUNNER_TOKEN["raw"])
except Exception as exc:
    print(f"device-key exchange failed: {exc}", file=sys.stderr)
'
minted_runner_token() {  # <base> <runner id>
  [ "${CANOPY_DEVICE_KEY:-1}" = "0" ] && return 0
  # A unit's key is in the unit's home, not root's: only its shared token is read.
  [ "$UNITS_MODE" = "1" ] && return 0
  [ -n "$1" ] && [ -n "$2" ] && [ -r "$TARGET" ] || return 0
  python3 -c "$MINT_HELPER" "$TARGET" "$1" "$2" 2>/dev/null | tail -1
}

runner_token() {  # <base> <runner id> -> a runner token, or nothing
  local t
  t="$(shared_runner_token)"
  [ -n "$t" ] || t="$(minted_runner_token "$1" "$2")"
  printf '%s' "$t"
}

# --- what should be installed ----------------------------------------------
fleet_expected() {  # <base> <token> <runner id> -> the sha; non-zero if unreadable
  # `python3 -c`, NOT `python3 - <<HEREDOC`: a heredoc redirects stdin, so it would
  # silently win over the pipe and the parser would read the PROGRAM instead of the
  # response — every box answering `unknown`, forever, with nothing in the log.
  local body
  body="$(curl -fsS --max-time 30 -H "Authorization: Bearer $2" \
      "${1%/}/api/harness/runners/" 2>/dev/null)" || return 1
  printf '%s' "$body" | python3 -c '
import json, sys
rid = sys.argv[1]
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(1)
rows = rows.get("items", rows) if isinstance(rows, dict) else rows
for row in rows or []:
    if str(row.get("id")) == rid:
        print((row.get("expected_code_sha") or "").strip())
        break
' "$3" 2>/dev/null
}

expected_sha() {
  # GET the fleet list and pick our own row. Read-only by construction: there is no
  # POST anywhere in this script, deliberately (see the header).
  #
  # WHY it is empty goes to stderr (the journal): an `unknown` that does not say
  # why is how a revoked token stalled this box for hours with nothing in the log.
  local base pat rid rtok
  [ -r "$ENV_FILE" ] || { log "unknown because: $ENV_FILE is not readable" >&2; return; }
  base="$(moved_base)"
  [ -n "$base" ] || base="$(sed -n 's/^CANOPY_BASE_URL=//p' "$ENV_FILE" | tail -1)"
  pat="$(sed -n 's/^CANOPY_TOKEN=//p' "$ENV_FILE" | tail -1)"
  rid="$(json_field "$STATE_FILE" runner_id "")"
  if [ -z "$base" ] || [ -z "$rid" ]; then
    log "unknown because: no canopy-web address or no runner id ($STATE_FILE) yet" >&2
    return
  fi
  rtok="$(runner_token "$base" "$rid")"
  if [ -n "$rtok" ] && fleet_expected "$base" "$rtok" "$rid"; then return; fi
  if [ -n "$pat" ] && fleet_expected "$base" "$pat" "$rid"; then return; fi
  log "unknown because: canopy-web's runner list was unreadable with $(
    [ -n "$rtok" ] && echo "this box's runner token or " || echo "no runner token (device key not approved?) and ")$(
    [ -n "$pat" ] && echo "the CANOPY_TOKEN fallback" || echo "no CANOPY_TOKEN fallback")" >&2
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
  # any sudo at all — scoped to this exact command by a sudoers drop-in. On a box of
  # units this runs as root and restarts every unit (and the broker) at once.
  # shellcheck disable=SC2086 # SERVICE is one or more unit patterns
  $RESTART_CMD $SERVICE
}

install_broker_from_git() {  # <ref> — the units' KMS broker, beside the runner it serves
  [ "$UNITS_MODE" = "1" ] || return 0
  local tmp="$RUNNER_HOME/kms_broker.py.new"
  if git -C "$REPO_DIR" show "$1:runner/ec2/kms_broker.py" > "$tmp" 2>/dev/null \
     && python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp" 2>/dev/null; then
    chmod 0755 "$tmp" && mv "$tmp" "$RUNNER_HOME/kms_broker.py"
  else
    rm -f "$tmp"
    log "ref $1 has no usable runner/ec2/kms_broker.py; the broker keeps its bytes"
  fi
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
  install_broker_from_git "$ref"
  install_bytes "$tmp" "$sha" "${committed_at:-0}" "$ref"
}

_secret() {  # <id> -> SecretString on stdout
  aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text 2>/dev/null
}

install_from_secret() {
  local tmp="$TARGET.new" dir sha committed_at n i
  dir="$(mktemp -d)"
  # The manifest seed first: it is what up.sh publishes, and it carries its own
  # provenance, so the stamp can name what was installed.
  if _secret "$SEED_SECRET" > "$dir/manifest.json" && [ -s "$dir/manifest.json" ]; then
    n="$(json_field "$dir/manifest.json" parts 0)"
    : > "$dir/code.b64"
    for i in $(seq 0 $((n - 1))); do
      _secret "${SEED_SECRET}-part-$i" >> "$dir/code.b64" || {
        log "could not read ${SEED_SECRET}-part-$i; nothing installed."
        rm -rf "$dir"; return 1
      }
    done
    if ! base64 -d < "$dir/code.b64" 2>/dev/null | gunzip > "$tmp" 2>/dev/null \
       || ! python3 - "$dir/manifest.json" "$tmp" <<'PY'
import hashlib, json, sys
m = json.load(open(sys.argv[1]))
sys.exit(0 if hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest() == m["sha256"] else 1)
PY
    then
      log "the $SEED_SECRET seed did not decode to its own sha256 (a publish in progress?); nothing installed."
      rm -rf "$dir" "$tmp"; return 1
    fi
    sha="$(json_field "$dir/manifest.json" sha "")"
    committed_at="$(json_field "$dir/manifest.json" committed_at 0)"
    rm -rf "$dir"
    install_bytes "$tmp" "$sha" "$committed_at" "secret:$SEED_SECRET"
    return $?
  fi
  rm -rf "$dir"
  if ! _secret "$CODE_SECRET" | base64 -d | gunzip > "$tmp"; then
    log "could not read $SEED_SECRET or $CODE_SECRET; nothing installed."
    rm -f "$tmp"
    return 1
  fi
  # The legacy secret carries no provenance, so the stamp is CLEARED rather than
  # guessed. Unknown is the honest answer: those bytes are whatever an operator
  # published, and a borrowed sha would tell the fleet this box is current when
  # it is not.
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

# Move origin/main: the shim runs THIS script from it, and fetch_commit fetches the
# deployed commit by sha, which never moves it. Without this a box ran a day-old
# updater (cloud-ec2-2, 2026-10-06). Takes effect from the next tick.
[ -d "$REPO_DIR/.git" ] && git -C "$REPO_DIR" fetch --quiet origin main 2>/dev/null || true

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
    # current | busy | unknown all mean "no new CODE", and all exit 0. This runs on
    # a timer: a non-zero exit for the ordinary case fills the journal with false
    # failures and trains everyone to ignore the one that matters.
    log "$V — no code change."
    ;;
esac

# --- credentials, every tick ------------------------------------------------
#
# Code is not the only thing that goes stale. bootstrap_agents.sh runs on service
# START, and the only thing that starts the service is this script installing new
# code — so a CONFIG change (a credential set in canopy-web, a vault rotation, a
# 1Password grant that was missing) had no path to the box at all. It would sit
# unread until someone happened to ship unrelated code, which is not a mechanism.
#
# That is the second half of the 2026-09-07 outage: the fix landed, the operator
# set the credential, and nothing would have picked it up.
#
# `--credentials-only` skips the expensive, disruptive half of a bootstrap (clone,
# op inject, plugin installs) and re-runs the cheap idempotent half — client creds
# and the gmail token — both of which short-circuit when already satisfied. It
# also re-reports readiness, so the control plane's answer refreshes on its own
# rather than dating from the last deploy.
#
# Skipped while a turn is in flight, for the same reason an install is: this
# touches the credential store the running turn is using.
#
# Run from the RUNNER'S own clone (cloud_runner.py's RUNNER_SRC_DIR, reset to
# origin/main on every start), not the working tree of $REPO_DIR: agent turns
# can reach that one, and on 2026-09-22 one had left it on a local branch. The
# shared clone is only the fallback for a box that predates the runner's own.
BOOT_DIR="${RUNNER_SRC_DIR:-$RUNNER_HOME/src}"
[ -x "$BOOT_DIR/runner/ec2/bootstrap_agents.sh" ] || BOOT_DIR="$REPO_DIR"
# A runner that keeps secrets off disk records its tmpfs root here; `canopy agent
# prepare --runner` then writes no env files in this pass either (spec §4).
TURN_ENV_ROOT_MARKER="${TURN_ENV_ROOT_MARKER:-$RUNNER_HOME/turn-env-root}"
AS_UNIT="${AS_UNIT:-runuser -u}"
if [ "$UNITS_MODE" = "1" ]; then
  # Each unit refreshes as ITSELF — its user, its env file, its own runner token —
  # from its own runner clone. Root never runs an agent's prepare.
  if [ "$V" = "busy" ]; then
    log "busy — skipping the credentials refresh too."
  else
    while read -r id user home; do
      [ -n "$home" ] || continue
      script="$home/.canopy-runner/src/runner/ec2/bootstrap_agents.sh"
      [ -x "$script" ] || { log "unit $id: no runner clone yet; skipping its credentials refresh"; continue; }
      tok="$(python3 -c '
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    if float(d.get("expires_at") or 0) - time.time() > 120:
        print(d.get("access_token") or "")
except Exception:
    pass' "$home/.canopy-runner/runner-token.json" 2>/dev/null)"
      root="$(tr -d '[:space:]' < "$home/.canopy-runner/turn-env-root" 2>/dev/null)"
      log "unit $id: refreshing credentials"
      # shellcheck disable=SC2046,SC2086 # the unit's env file, one KEY=value per word
      $AS_UNIT "$user" -- env -i $(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${UNIT_ETC:-/etc/canopy-runner}/$id.env" 2>/dev/null) \
        ${tok:+CANOPY_TOKEN=$tok} ${root:+CANOPY_TURN_ENV_ROOT=$root} \
        bash "$script" --credentials-only \
        || log "unit $id: credentials refresh returned non-zero — the unit is otherwise untouched."
    done <<<"$(unit_rows)"
  fi
  exit 0
fi
if [ "$V" = "busy" ]; then
  log "busy — skipping the credentials refresh too."
elif [ -x "$BOOT_DIR/runner/ec2/bootstrap_agents.sh" ]; then
  log "refreshing credentials (config changes reach the box here, not via a deploy)"
  # Under the RUNNER'S environment, not this timer's. canopy-runner.service loads
  # runner.env; canopy-runner-update.service does not, so this pass used to run
  # with no GOG_KEYRING_PASSWORD (every keyring read fails: "mailbox NOT live" for
  # all five agents) and no CANOPY_TOKEN (no per-agent vault, no canopy-web token:
  # "no gog token anywhere" for ace while the very next service start read both).
  # Measured 2026-09-10 11:59Z on cloud-ec2-1 — and this pass re-reports readiness,
  # so the control plane was told the fleet's mailboxes were dead every 30 minutes.
  ( set +e
    if [ -r "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
    moved="$(moved_base)"
    if [ -n "$moved" ]; then
      case "${CANOPY_WEB_API_URL:-}" in
        ""|"${CANOPY_BASE_URL:-}"|"${CANOPY_BASE_URL:-}/") export CANOPY_WEB_API_URL="$moved" ;;
      esac
      export CANOPY_BASE_URL="$moved"
    fi
    # The roster read, each prepare and its readiness report act AS THE BOX, as on
    # service start — not with runner.env's PAT (revoked 2026-10-17: the roster
    # came back empty, only the AGENT_SLUGS fallback was refreshed, and every
    # readiness report was a 401).
    rt="$(runner_token "${CANOPY_BASE_URL:-}" "$(json_field "$STATE_FILE" runner_id "")")"
    if [ -n "$rt" ]; then export CANOPY_TOKEN="$rt"; fi
    root="$(tr -d '[:space:]' < "$TURN_ENV_ROOT_MARKER" 2>/dev/null)"
    if [ -n "$root" ]; then export CANOPY_TURN_ENV_ROOT="$root"; fi
    "$BOOT_DIR/runner/ec2/bootstrap_agents.sh" --credentials-only ) \
    || log "credentials refresh returned non-zero — the box is otherwise untouched."
fi
exit 0
