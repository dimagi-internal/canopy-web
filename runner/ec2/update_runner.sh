#!/usr/bin/env bash
#
# Should this cloud box install newer runner bytes right now?
#
#   canopy-runner-update               # the timer's mode: install iff stale (and, for a
#                                      # daemon that cannot hand its turns off, idle)
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
#     daemon on every heartbeat. A daemon run by the runner's SUPERVISOR (cloud_runner.py
#     "turns that outlive the daemon") HANDS OFF: the updater signals the supervisor
#     (SIGHUP), which swaps the daemon for the new bytes while every turn keeps
#     running, and the new daemon adopts them. So a busy box installs at once; only a
#     turn the marker counts as `pinned` (one a handoff cannot carry) still waits, as
#     does every turn of a daemon that predates the supervisor. A restart of the UNIT
#     (an idle box, an old daemon) still stops everything, as it always has.
#     UPDATE_IDLE_ONLY=1, or a file $RUNNER_HOME/update-idle-only, opts back into
#     installing only on an idle box.
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
# `systemctl restart canopy-runner.service`. (The exception is one config file that
# keeps needrestart away from the runner: see ensure_needrestart_exclusion.)
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
SHA_PATHS=("runner/ec2/cloud_runner.py" "runner/ec2/bootstrap_agents.sh" "runner/ec2/update_runner.sh" "runner/ec2/kms_broker.py" "runner/ec2/aws_broker.py" "packages/canopy_transcript" "runner/canopy_acp")
# The daemon rewrites the busy marker every heartbeat (20s). Older than this means
# it is not running its loop at all — stopped, wedged, or crash-looping. That is
# NOT busy: it is the case this script exists to rescue, so it must never block.
BUSY_MAX_AGE=120
IDLE_ONLY_FILE="${IDLE_ONLY_FILE:-$RUNNER_HOME/update-idle-only}"
idle_only() { [ "${UPDATE_IDLE_ONLY:-0}" = "1" ] || [ -e "$IDLE_ONLY_FILE" ]; }
# The decision of every tick, for the runner's `update` check (this script never
# calls canopy-web: the daemon reads this file and reports it on its heartbeat).
DECISION_FILE="${DECISION_FILE:-$RUNNER_HOME/update-last.json}"
# How long a handoff gets to show a new daemon before it is called unconfirmed.
HANDOFF_WAIT="${HANDOFF_WAIT:-90}"

# --- several runners on one machine (docs/architecture/runners.md) ---------------
# A machine of several runners (`cloud_runner.py --setup-runners` wrote runners.json)
# runs one `canopy-runner@<id>` per runner, each its own user. This script then runs
# as ROOT (canopy-runner-update.service, installed by `cloud_runner.py --install-runners`):
# ONE install for every runner, deferred while ANY runner has a turn in flight, and a
# restart of every `canopy-runner@*`. The runner bytes in /opt/canopy-runner and
# the clone in /opt/canopy-web are root's there, so no runner can rewrite the code
# the others run.
RUNNERS_FILE="${RUNNERS_FILE:-/etc/canopy-runner/runners.json}"
RUNNERS_MODE=0
if [ -r "$RUNNERS_FILE" ]; then
  RUNNERS_MODE=1
  ENV_FILE="${BOX_ENV_FILE:-/etc/canopy-runner/box.env}"
  SERVICE="${SERVICE_UNITS:-canopy-runner@*.service canopy-kms-broker.service}"
  [ -n "$RESTART_GIVEN" ] || RESTART_CMD="systemctl restart"
fi
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
# `runner_rows` -> "<id> <user> <home>" per runner, from runners.json.
runner_rows() {
  python3 -c '
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
for r in doc.get("runners") or []:
    if r.get("id") and r.get("user") and r.get("home"):
        print(r["id"], r["user"], r["home"])
' "$RUNNERS_FILE" 2>/dev/null
}
if [ "$RUNNERS_MODE" = "1" ]; then
  # The runner id (any runner's: they share an owner, so the runner list it reads
  # names every runner on the box) and a token to read it with.
  read -r _FIRST_ID _FIRST_USER FIRST_HOME <<<"$(runner_rows | head -1)"
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
# What a runner's state dir says about handing its turns off: "<count> <blocking> <pid>"
# — `blocking` is how many of its turns an install must wait for (all of them, unless
# its daemon hands off: then only the `pinned` ones); <pid> is its supervisor's, or 0
# when none that hands off is running. Nothing at all when the marker is not trusted.
marker_info() {  # <dir holding in-flight and supervisor.json>
  local idle=0
  idle_only && idle=1
  python3 - "$1/in-flight" "$1/supervisor.json" "$BUSY_MAX_AGE" "$idle" <<'PY' 2>/dev/null
import json, os, sys, time
marker, sup, max_age, idle_only = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1"
try:
    raw = json.load(open(marker))
    if time.time() - float(raw["at"]) > max_age:
        sys.exit(0)
    count = int(raw["count"])
except Exception:
    sys.exit(0)
pid = 0
try:
    s = json.load(open(sup))
    cand = int(s.get("pid") or 0)
    if s.get("handoff") and cand > 0:
        os.kill(cand, 0)
        cmd = os.path.join("/proc", str(cand), "cmdline")
        if not os.path.isdir("/proc/self") or b"cloud_runner.py" in open(cmd, "rb").read():
            pid = cand
except Exception:
    pid = 0
handoff = bool(pid and raw.get("handoff") and not idle_only)
blocking = int(raw.get("pinned") or 0) if handoff else count
print(count, blocking, pid if handoff else 0)
PY
}
# The single runner's state dir (in-flight and supervisor.json sit side by side).
BUSY_DIR="$(dirname "$BUSY")"
home_of_runner() { runner_rows | awk -v i="$1" '$1 == i {print $3}'; }
blocking() {  # how many turns hold an install back ("" = no trustworthy marker)
  local c b p
  if [ "$RUNNERS_MODE" != "1" ]; then
    read -r c b p <<<"$(marker_info "$BUSY_DIR")"
    printf '%s' "$b"
    return
  fi
  # A box of several runners waits only while EVERY runner is held: one that is idle
  # or hands off takes the new code at once (`restart_runners`).
  local total="" id user home
  while read -r id user home; do
    [ -n "$home" ] || continue
    read -r c b p <<<"$(marker_info "$home/.canopy-runner")"
    [ -n "$b" ] || { printf '0'; return; }
    [ "$b" -eq 0 ] 2>/dev/null && { printf '0'; return; }
    total=$(( ${total:-0} + b ))
  done <<<"$(runner_rows)"
  printf '%s' "$total"
}
# A runner held back only by turns it can hand off: in flight, but none pinned.
hands_off() {  # <runner id> -> its supervisor pid, or nothing
  local c b p
  read -r c b p <<<"$(marker_info "$(home_of_runner "$1")/.canopy-runner")"
  [ "${p:-0}" != "0" ] && [ "${b:-1}" = "0" ] && echo "$p"
}
in_flight() {  # echoes a count, or "" when no marker can be trusted
  if [ "$RUNNERS_MODE" != "1" ]; then
    in_flight_of "$BUSY"
    return
  fi
  # A box of several runners is busy only while EVERY runner is: an idle runner
  # takes the new code at once (`restart_runners`), and a busy one when it is idle.
  [ -n "$(idle_runners)" ] && { printf '0'; return; }
  local total="" id user home n
  while read -r id user home; do
    [ -n "$home" ] || continue
    n="$(in_flight_of "$home/.canopy-runner/in-flight")"
    [ -n "$n" ] && total=$(( ${total:-0} + n ))
  done <<<"$(runner_rows)"
  printf '%s' "$total"
}
# The ids of a box's runners with / without a turn in flight, one per line. A marker
# that cannot be trusted is idle, as above: unknown must not block.
busy_runners() {
  local id user home n
  while read -r id user home; do
    [ -n "$home" ] || continue
    n="$(in_flight_of "$home/.canopy-runner/in-flight")"
    [ -n "$n" ] && [ "$n" -gt 0 ] 2>/dev/null && echo "$id"
  done <<<"$(runner_rows)"
}
idle_runners() {
  local id user home busy
  busy="$(busy_runners)"
  while read -r id user home; do
    [ -n "$id" ] || continue
    grep -qxF "$id" <<<"$busy" || echo "$id"
  done <<<"$(runner_rows)"
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
  # A runner's key is in the runner's home, not root's: only its shared token is read.
  [ "$RUNNERS_MODE" = "1" ] && return 0
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
  carrying="$(blocking)"
  if [ -n "$carrying" ] && [ "$carrying" -gt 0 ] 2>/dev/null; then echo "busy"; return; fi
  echo "stale"
}

# --- what this tick decided -------------------------------------------------
# One line in the journal AND the decision file, every tick: current, installed (from
# -> to, and how each runner took it), deferred (and why), unknown, failed.
decide() {  # <verdict> <detail>
  log "decision: $1 — $2"
  python3 - "$DECISION_FILE" "$1" "$2" "${INSTALLED_SHA:-}" "${EXPECTED:-}" <<'PY' 2>/dev/null || true
import json, os, sys, time
path, verdict, detail, frm, to = sys.argv[1:6]
tmp = path + ".new"
with open(tmp, "w") as fh:
    json.dump({"verdict": verdict, "detail": detail, "from": frm, "to": to,
               "at": int(time.time())}, fh)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

# Why an install waits, in words (`busy`).
deferral_reason() {
  if idle_only; then
    echo "idle-only is set ($IDLE_ONLY_FILE or UPDATE_IDLE_ONLY=1): waiting for no turn in flight"
    return
  fi
  local c b p
  if [ "$RUNNERS_MODE" != "1" ]; then
    read -r c b p <<<"$(marker_info "$BUSY_DIR")"
    if [ "${p:-0}" = "0" ]; then
      echo "$c turn(s) in flight and the running daemon cannot hand them off (it predates the supervisor)"
    else
      echo "$b of $c turn(s) in flight cannot be handed off (pinned)"
    fi
    return
  fi
  local out="" id user home
  while read -r id user home; do
    [ -n "$home" ] || continue
    read -r c b p <<<"$(marker_info "$home/.canopy-runner")"
    out="$out$id: $b of $c turn(s) held$( [ "${p:-0}" = "0" ] && echo ' (no handoff)'); "
  done <<<"$(runner_rows)"
  echo "every runner is held — $out"
}

# Hand a running daemon off to the installed bytes: SIGHUP to its supervisor, which
# swaps the daemon while every turn keeps running. Waits for the new daemon to show.
handoff_to_new_bytes() {  # <label> <state dir> <supervisor pid>
  local label="$1" dir="$2" pid="$3" before after waited=0
  before="$(json_field "$dir/supervisor.json" daemon_started_at 0)"
  if ! ${KILL_CMD:-kill} -HUP "$pid" 2>/dev/null; then
    log "$label: could not signal its supervisor (pid $pid) to hand off"
    return 1
  fi
  log "$label: handing off to the new code (supervisor pid $pid; its turns keep running)"
  while [ "$waited" -lt "$HANDOFF_WAIT" ]; do
    after="$(json_field "$dir/supervisor.json" daemon_started_at 0)"
    if [ "$after" != "$before" ]; then
      log "$label: handoff done — new daemon pid $(json_field "$dir/supervisor.json" daemon_pid "?")"
      return 0
    fi
    sleep "${HANDOFF_POLL:-2}"
    waited=$(( waited + ${HANDOFF_POLL:-2} ))
  done
  log "$label: handoff NOT confirmed within ${HANDOFF_WAIT}s (no new daemon in $dir/supervisor.json)"
  return 1
}
RESTART_OUTCOME=""

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
  if [ "$RUNNERS_MODE" = "1" ]; then
    log "installed ${sha:-<unknown>} ($ref) -> $TARGET"
    restart_runners "$(idle_runners)" "$(runner_rows | cut -d' ' -f1)"
    return
  fi
  local c b p
  read -r c b p <<<"$(marker_info "$BUSY_DIR")"
  if [ -n "$c" ] && [ "$c" -gt 0 ] 2>/dev/null && [ "${p:-0}" != "0" ]; then
    # Busy, and the daemon hands off: the turns keep running, the new daemon adopts them.
    log "installed ${sha:-<unknown>} ($ref) -> $TARGET; $c turn(s) in flight — handing off"
    if handoff_to_new_bytes "runner" "$BUSY_DIR" "$p"; then
      RESTART_OUTCOME="handed off ($c turn(s) kept running)"
    else
      RESTART_OUTCOME="handoff not confirmed"
    fi
    return 0
  fi
  log "installed ${sha:-<unknown>} ($ref) -> $TARGET; restarting $SERVICE"
  # The only privileged step, and the only reason this box grants the service user
  # any sudo at all — scoped to this exact command by a sudoers drop-in.
  # shellcheck disable=SC2086 # SERVICE is one or more unit patterns
  if $RESTART_CMD $SERVICE; then
    RESTART_OUTCOME="restarted $SERVICE"
  else
    RESTART_OUTCOME="restart of $SERVICE FAILED"
    log "$RESTART_OUTCOME"
  fi
}

# --- restarting a box of several runners ------------------------------------
#
# A runner's bytes in $TARGET are read once, at its start, so they can be replaced
# while every runner keeps running. What cannot happen under a running turn is
# root's setup: canopy-runners.service is Required by every runner, so restarting
# it restarts them all, and the egress lock it re-applies can cut a turn off. So:
#
# * the whole box idle: one restart of everything — root's setup first (users, env
#   files, the IMDS rule, the egress lock and its sync timer), its runners with it,
#   and the broker — exactly as a box of one runner restarts.
# * some runner busy: only the IDLE runners restart, each onto the new code (and
#   through its own start, a fresh bootstrap and canopy CLI). The busy ones are
#   written to $OWED and restarted, one by one, on the first tick that finds each
#   idle; root's setup and the broker wait for a tick that finds the whole box idle.
#   One busy runner used to hold every runner on the machine back
#   (cloud-ec2-2-marshellis idle on canopy 0.2.813 behind cloud-ec2-live's turns).
#
# $OWED existing means root's setup is owed; its lines are the runners still
# running the old bytes.
OWED="${OWED_FILE:-$RUNNER_HOME/restart-owed}"
restart_runners() {  # <idle ids to restart> <ids still on the old bytes>, one per line each
  local idle="$1" old="$2" id busy
  busy="$(busy_runners)"
  if [ -z "$busy" ]; then
    log "restarting every runner, root's setup and the broker"
    $SYSTEMCTL restart canopy-runners.service || log "canopy-runners.service did not restart cleanly"
    # shellcheck disable=SC2086 # SERVICE is one or more unit patterns
    $RESTART_CMD $SERVICE
    rm -f "$OWED"
    return
  fi
  local outcome="" pid
  while read -r id; do
    [ -n "$id" ] || continue
    log "runner $id: idle — restarting it onto the new code"
    if $RESTART_CMD "canopy-runner@$id.service"; then outcome="$outcome$id restarted; "
    else log "runner $id: did not restart cleanly"; outcome="$outcome$id restart FAILED; "; fi
  done <<<"$idle"
  : > "$OWED"
  while read -r id; do
    [ -n "$id" ] && grep -qxF "$id" <<<"$busy" || continue
    pid="$(hands_off "$id")"
    if [ -n "$pid" ]; then
      # Busy, every turn adoptable: the daemon is swapped, its turns keep running.
      if handoff_to_new_bytes "runner $id" "$(home_of_runner "$id")/.canopy-runner" "$pid"; then
        outcome="$outcome$id handed off; "
      else
        outcome="$outcome$id handoff not confirmed; "
      fi
      continue
    fi
    echo "$id" >> "$OWED"
    outcome="$outcome$id owed (a turn that cannot be handed off); "
  done <<<"$old"
  RESTART_OUTCOME="$outcome"
  log "still owed a restart (a turn that cannot be handed off): $(tr '\n' ' ' < "$OWED")— root's setup waits for the whole box to be idle"
}
# A tick with no new code: finish what an earlier install could not.
settle_owed() {
  [ "$RUNNERS_MODE" = "1" ] && [ -f "$OWED" ] || return 0
  local now_idle owed_idle id
  now_idle="$(idle_runners)"
  owed_idle=""
  while read -r id; do
    [ -n "$id" ] || continue
    grep -qxF "$id" <<<"$now_idle" && owed_idle="$owed_idle$id"$'\n'
  done < "$OWED"
  # An owed runner whose daemon can now hand off (its pinned turn ended) takes it too.
  local ready=""
  while read -r id; do
    [ -n "$id" ] && [ -n "$(hands_off "$id")" ] && ready=1
  done < "$OWED"
  if [ -z "$(busy_runners)" ] || [ -n "$owed_idle" ] || [ -n "$ready" ]; then
    restart_runners "$owed_idle" "$(cat "$OWED")"
  fi
}

install_broker_from_git() {  # <ref> — the runners' KMS broker, beside the runner it serves
  [ "$RUNNERS_MODE" = "1" ] || return 0
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

# --- keep needrestart off the runner ----------------------------------------
#
# Ubuntu's unattended-upgrades runs needrestart, which restarts every service
# using an upgraded library. On cloud-ec2-2 it restarted canopy-runner.service
# 25 minutes into an ACE turn (failure investigation 7, a libsystemd0 update) and
# the turn died with the unit's cgroup. This updater refuses to restart a busy
# runner; needrestart does not check, so it is told to skip every canopy unit —
# the runner (`canopy-runner`, `canopy-runner@<id>`) and what a box of several
# runners runs beside it (`canopy-runners`, `canopy-kms-broker`, `canopy-pair`), any of
# which takes the turns down with it. A new library is picked up at the next
# deliberate restart (an install above, or a reboot).
#
# The template bakes this file into new boxes; cloud-init never re-runs on boxes
# already up, so every tick lays it down too, replacing a rule that differs (the
# narrower `^canopy-runner` one written by hand on 2026-10-16). As the service
# user it goes through `sudo -n`; on a box of several runners this already runs as root. It
# is skipped when the box has no needrestart and never fails the tick.
NEEDRESTART_CONF="${NEEDRESTART_CONF:-/etc/needrestart/conf.d/50-canopy-runner.conf}"
if [ "$(id -u)" = "0" ]; then PRIV_CMD="${PRIV_CMD-}"; else PRIV_CMD="${PRIV_CMD-sudo -n}"; fi
NEEDRESTART_RULE='$nrconf{override_rc}->{qr(^canopy-)} = 0;'
ensure_needrestart_exclusion() {
  [ -d "$(dirname "$NEEDRESTART_CONF")" ] || return 0
  grep -qsF "$NEEDRESTART_RULE" "$NEEDRESTART_CONF" && return 0
  local tmp; tmp="$(mktemp)"
  printf '# canopy: never let needrestart restart a canopy unit mid-turn (canopy-web runner/ec2/update_runner.sh)\n%s\n' \
    "$NEEDRESTART_RULE" > "$tmp"
  # shellcheck disable=SC2086 # PRIV_CMD is empty or `sudo -n`
  if $PRIV_CMD install -m 0644 "$tmp" "$NEEDRESTART_CONF" 2>/dev/null; then
    log "needrestart told to skip every canopy unit ($NEEDRESTART_CONF)"
  else
    log "could not write $NEEDRESTART_CONF; an unattended upgrade can still restart the runner mid-turn"
  fi
  rm -f "$tmp"
}

# --- what no runner cleans --------------------------------------------------
#
# Each runner sweeps only its own files (cloud_runner.py maybe_sweep_disk),
# while its `disk` check measures the whole volume. On cloud-ec2-2 (2026-10-26)
# that check warned at 89% while every sweep freed 0 MB: 35 GB sat in the home of
# the user the box ran as before it had several runners, and 12 GB of /tmp belonged
# to users no runner may delete. Root's tick is the one place that can see all of it.

# /tmp: Ubuntu's own rule (/usr/lib/tmpfiles.d/tmp.conf, `D /tmp ... -`) empties
# /tmp only at boot, and a runner box is not rebooted. A file of the same name in
# /etc replaces the vendor one, adding an age: systemd-tmpfiles-clean.timer then
# removes what has not been touched in 3 days, whoever owns it, every day.
# systemd's own private dirs keep their `x` rules (systemd-tmp.conf).
TMPFILES_CONF="${TMPFILES_CONF:-/etc/tmpfiles.d/tmp.conf}"
TMPFILES_RULE='D /tmp 1777 root root 3d'
ensure_tmp_age_rule() {
  [ -d "$(dirname "$TMPFILES_CONF")" ] || return 0
  grep -qsxF "$TMPFILES_RULE" "$TMPFILES_CONF" && return 0
  local tmp; tmp="$(mktemp)"
  printf '# canopy: /tmp loses what nobody touched in 3 days, not only at boot (canopy-web runner/ec2/update_runner.sh)\n%s\n' \
    "$TMPFILES_RULE" > "$tmp"
  # shellcheck disable=SC2086 # PRIV_CMD is empty or `sudo -n`
  if $PRIV_CMD install -m 0644 "$tmp" "$TMPFILES_CONF" 2>/dev/null; then
    log "/tmp now ages out after 3 days ($TMPFILES_CONF)"
  else
    log "could not write $TMPFILES_CONF; /tmp is still emptied only at boot"
  fi
  rm -f "$tmp"
}

# The user a box ran as before it had several runners keeps a home full of caches
# nothing reads any more (on cloud-ec2-2: plugin versions at 1.8 GB each, uv's cache).
# Only regenerable caches go, never work, keys or config, and only on a box of
# several runners, when that user is not one of theirs and runs nothing.
LEGACY_USER="${LEGACY_USER:-ubuntu}"
LEGACY_CACHES=(".claude/plugins/cache" ".cache/uv" ".cache/ms-playwright" ".npm/_cacache")
retire_legacy_caches() {
  [ "$RUNNERS_MODE" = "1" ] || return 0
  local home="${LEGACY_HOME:-}" rel size
  [ -n "$home" ] || home="$(getent passwd "$LEGACY_USER" 2>/dev/null | cut -d: -f6)"
  [ -n "$home" ] && [ -d "$home" ] || return 0
  # A runner's own user is never legacy.
  runner_rows | awk '{print $2}' | grep -qxF "$LEGACY_USER" && return 0
  # Someone (or something) is working as it: leave its caches alone.
  pgrep -u "$LEGACY_USER" >/dev/null 2>&1 && return 0
  for rel in "${LEGACY_CACHES[@]}"; do
    [ -d "$home/$rel" ] && [ ! -L "$home/$rel" ] || continue
    size="$(du -sh "$home/$rel" 2>/dev/null | cut -f1)"
    rm -rf "${home:?}/$rel" \
      && log "removed $home/$rel (${size:-?}): $LEGACY_USER runs nothing on this box of runners"
  done
}

# Where the space is, for the runners' `disk` check: they run as their own users and
# cannot read the other homes, so root measures them and leaves the answer here.
# Only when the volume is at warn — du over every home is not free.
DISK_USAGE_FILE="${DISK_USAGE_FILE:-$RUNNER_HOME/disk-usage.json}"
DISK_USAGE_ROOT="${DISK_USAGE_ROOT:-/}"
DISK_WARN_PCT="${DISK_WARN_PCT:-80}"
record_disk_usage() {
  local pct p paths=()
  pct="$(df -P "$DISK_USAGE_ROOT" 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}')"
  if [ -z "$pct" ] || [ "$pct" -lt "${DISK_WARN_PCT%.*}" ]; then
    rm -f "$DISK_USAGE_FILE"
    return 0
  fi
  for p in "${DISK_USAGE_ROOT%/}"/home/* "${DISK_USAGE_ROOT%/}"/tmp; do
    [ -d "$p" ] && paths+=("$p")
  done
  [ ${#paths[@]} -gt 0 ] || return 0
  local limit=""; command -v timeout >/dev/null && limit="timeout 300"
  # shellcheck disable=SC2086 # limit is empty or `timeout 300`
  $limit du -xs --block-size=1 "${paths[@]}" 2>/dev/null | python3 -c '
import json, os, sys, time
rows = []
for line in sys.stdin:
    size, _, path = line.rstrip("\n").partition("\t")
    if size.isdigit():
        rows.append([path, int(size)])
rows.sort(key=lambda r: -r[1])
tmp = sys.argv[1] + ".new"
with open(tmp, "w") as fh:
    json.dump({"at": int(time.time()), "pct": int(sys.argv[2]), "largest": rows[:6]}, fh)
os.chmod(tmp, 0o644)
os.replace(tmp, sys.argv[1])
' "$DISK_USAGE_FILE" "$pct" || log "could not record where the disk space is"
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

if [ "$MODE" != "check" ]; then
  ensure_needrestart_exclusion
  ensure_tmp_age_rule
  retire_legacy_caches
  record_disk_usage
fi

if [ "$MODE" = "check" ]; then
  echo "$V expected=${EXPECTED:-<unknown>} installed=${INSTALLED_SHA:-<unknown>}"
  exit 0
fi

case "$V" in
  stale)
    log "behind — installing ${EXPECTED:0:12} (running ${INSTALLED_SHA:0:12})"
    if install_from_git "$EXPECTED"; then
      decide installed "${INSTALLED_SHA:0:12} -> ${EXPECTED:0:12}: ${RESTART_OUTCOME:-done}"
    else
      decide failed "could not install ${EXPECTED:0:12} (running ${INSTALLED_SHA:0:12}); see the journal"
    fi
    ;;
  current)
    log "current — no code change."
    settle_owed
    decide current "${INSTALLED_SHA:0:12}${RESTART_OUTCOME:+; owed restarts: $RESTART_OUTCOME}"
    ;;
  busy)
    log "busy — no code change."
    decide deferred "${INSTALLED_SHA:0:12} -> ${EXPECTED:0:12} waits: $(deferral_reason)"
    ;;
  *)
    # current | busy | unknown all mean "no new CODE", and all exit 0. This runs on
    # a timer: a non-zero exit for the ordinary case fills the journal with false
    # failures and trains everyone to ignore the one that matters.
    log "$V — no code change."
    decide unknown "expected=${EXPECTED:-<unknown>} installed=${INSTALLED_SHA:-<unknown>} (why: above)"
    ;;
esac

# --- credentials, every tick ------------------------------------------------
#
# Code is not the only thing that goes stale. bootstrap_agents.sh runs on service
# START, and the only thing that starts the service is this script installing new
# code — so a CONFIG change (a credential set in canopy-web, a vault rotation, a
# vault grant that was missing) had no path to the box at all. It would sit
# unread until someone happened to ship unrelated code, which is not a mechanism.
#
# That is the second half of the 2026-09-07 outage: the fix landed, the operator
# set the credential, and nothing would have picked it up.
#
# `--credentials-only` skips the expensive, disruptive half of a bootstrap (clone,
# env fill, plugin installs) and re-runs the cheap idempotent half — client creds
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
AS_RUNNER="${AS_RUNNER:-runuser -u}"
# Run AS a runner's user: its own token (shared by its daemon, 0600 in its home) and tmpfs
# root, read in its own process; then its bootstrap's credentials pass.
# shellcheck disable=SC2016 # expanded by the runner user's shell, not this one
RUNNER_REFRESH='
t="$(python3 -c "
import json, sys, time
try:
    d = json.load(open(sys.argv[1]))
    if float(d.get(\"expires_at\") or 0) - time.time() > 120:
        print(d.get(\"access_token\") or \"\")
except Exception:
    pass" "$HOME/.canopy-runner/runner-token.json" 2>/dev/null)"
[ -n "$t" ] && export CANOPY_TOKEN="$t"
r="$(tr -d "[:space:]" < "$HOME/.canopy-runner/turn-env-root" 2>/dev/null)"
[ -n "$r" ] && export CANOPY_TURN_ENV_ROOT="$r"
exec bash "$0" --credentials-only'
if [ "$RUNNERS_MODE" = "1" ]; then
  # Each runner refreshes as ITSELF — its user, its env file, its own runner token —
  # from its own runner clone. Root never runs an agent's prepare.
  if [ "$V" = "busy" ]; then
    log "busy — skipping the credentials refresh too."
  else
    busy="$(busy_runners)"
    while read -r id user home; do
      [ -n "$home" ] || continue
      grep -qxF "$id" <<<"$busy" && { log "runner $id: busy — skipping its credentials refresh"; continue; }
      script="$home/.canopy-runner/src/runner/ec2/bootstrap_agents.sh"
      [ -x "$script" ] || { log "runner $id: no runner clone yet; skipping its credentials refresh"; continue; }
      log "runner $id: refreshing credentials"
      # The runner reads its OWN runner token and tmpfs root, inside its own process:
      # a token passed here would sit in /proc/<pid>/cmdline for any local process
      # to read. The env file holds no secret, so its words on the command line are safe.
      # shellcheck disable=SC2046,SC2016 # the runner's env file, one KEY=value per word; $HOME is the runner user's
      $AS_RUNNER "$user" -- env -i $(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "${RUNNER_ETC:-/etc/canopy-runner}/$id.env" 2>/dev/null) \
        bash -c "$RUNNER_REFRESH" "$script" \
        || log "runner $id: credentials refresh returned non-zero — the runner is otherwise untouched."
    done <<<"$(runner_rows)"
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
