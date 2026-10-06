#!/usr/bin/env bash
#
# Install (or update) the canopy laptop runner as a SNAPSHOT of a git ref.
#
#   runner/canopy_runner/scripts/install-runner.sh                 # origin/main
#   runner/canopy_runner/scripts/install-runner.sh --ref my-branch # deliberately, a branch
#   runner/canopy_runner/scripts/install-runner.sh --no-launchd    # install only, don't touch the daemon
#   runner/canopy_runner/scripts/install-runner.sh --if-stale      # auto-update mode (the timer job)
#   runner/canopy_runner/scripts/install-runner.sh --no-auto-update # skip installing the timer job
#   CANOPY_MENUBAR=0 runner/canopy_runner/scripts/install-runner.sh  # skip the macOS menu-bar app
#
# A NEW macOS account is the same one command — with no ~/.canopy/runner.json it
# pairs first (`canopy-runner pair`: register with canopy-web, pick free CDP/hook
# ports, write runner.json, build ~/Applications/Emdash CDP.app):
#   runner/canopy_runner/scripts/install-runner.sh --workspace dimagi
#   ... --pair         # run the pairing check even though runner.json exists (no-op if paired)
#   ... --no-pair      # never pair; install only
#   ... --name N --agents a,b --base-url URL   # pairing overrides (see `canopy-runner pair -h`)
#
# Why a snapshot and not the working tree: the daemon used to execute from
# ~/emdash-projects/canopy-web via PYTHONPATH, so any `git checkout` in that
# checkout silently changed the code it ran. Building from `git archive <ref>`
# into a temp dir means a dirty or branch-switched working tree cannot be
# installed by accident — installing a branch stays possible, but it is an act
# rather than a slip. See
# docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md.
#
# Building WHEELS (rather than installing the source tree) also sidesteps
# [tool.uv.sources], which marks the two in-repo path deps `editable = true`.
# That is right for `uv run` in the package and wrong here: an editable install
# would leave the tool venv pointing into $tmp, which this script deletes on
# exit — an install that works until the next import.
set -euo pipefail

REPO="${CANOPY_WEB_REPO:-$HOME/emdash-projects/canopy-web}"
REF="origin/main"
DO_LAUNCHD=1
DO_AUTO_UPDATE=1
IF_STALE=0
DO_PAIR=""          # "" = pair only if there is no config; 1 = always check; 0 = never
PAIR_ARGS=()
RUNNER_SRC="runner/canopy_runner/canopy_runner"
LABEL="com.canopy.runner"
UPDATER_LABEL="com.canopy.runner.updater"
CONFIG="$HOME/.canopy/runner.json"

# Kept for the stage-2 handover below: the updater runs a COPY of this script
# frozen at the last install, so by the time we know the target ref we may
# discover the archived tree carries a different installer than the one
# executing.
ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a git ref}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a path}"; shift 2 ;;
    --config) CONFIG="${2:?--config needs a path}"; shift 2 ;;
    --no-launchd) DO_LAUNCHD=0; shift ;;
    --no-auto-update) DO_AUTO_UPDATE=0; shift ;;
    --if-stale) IF_STALE=1; shift ;;
    --pair) DO_PAIR=1; shift ;;
    --no-pair) DO_PAIR=0; shift ;;
    --workspace|--name|--agents|--base-url|--runner-id|--cdp-port|--hook-port)
      PAIR_ARGS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "uv not found — https://docs.astral.sh/uv/" >&2; exit 1; }
# `rev-parse`, not `[ -d "$REPO/.git" ]`: in a git WORKTREE .git is a file, and
# the directory test would reject a perfectly good checkout.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "not a git checkout: $REPO (set CANOPY_WEB_REPO)" >&2; exit 1; }

# --- auto-update mode -------------------------------------------------------
# Ask the INSTALLED runner whether this box should update right now, and pin the
# install to the sha the control plane expects.
#
# Pinning matters: this must NOT track origin/main. The expected sha is the runner
# source in the DEPLOYED image — already through CI, the merge queue and a deploy.
# Installing main instead would run code nothing has deployed, and would leave
# code_sha != expected_code_sha permanently, so the staleness banner would fire
# forever on exactly the boxes auto-updating correctly.
if [ "$IF_STALE" -eq 1 ]; then
  CHECK_BIN="$(uv tool dir --bin 2>/dev/null)/canopy-runner"
  [ -x "$CHECK_BIN" ] || CHECK_BIN="$(command -v canopy-runner || true)"
  if [ -z "$CHECK_BIN" ] || [ ! -x "$CHECK_BIN" ]; then
    echo "$(date -u +%FT%TZ) --if-stale: no installed runner to check; nothing to do."
    exit 0
  fi
  [ -f "$CONFIG" ] || { echo "$(date -u +%FT%TZ) --if-stale: no config at $CONFIG."; exit 0; }
  read -r STATUS EXPECTED <<<"$("$CHECK_BIN" update-check --config "$CONFIG" 2>&1 | tail -1)"
  case "$STATUS" in
    stale)
      echo "$(date -u +%FT%TZ) --if-stale: behind — installing ${EXPECTED:0:12}"
      REF="$EXPECTED"
      ;;
    current|busy|unknown)
      # All "do nothing", and all exit 0: this runs on a timer, so a non-zero
      # exit for the ordinary case would fill launchd's log with false failures
      # and train everyone to ignore it.
      echo "$(date -u +%FT%TZ) --if-stale: $STATUS — nothing to do."
      exit 0
      ;;
    *)
      # Anything else is the installed runner not understanding `update-check` —
      # i.e. it predates auto-update. Fail CLOSED (an unparseable answer is not
      # evidence of anything) but say why, because "nothing to do" forever would
      # otherwise look exactly like a healthy up-to-date box.
      echo "$(date -u +%FT%TZ) --if-stale: update-check unavailable from $CHECK_BIN" >&2
      echo "    (it answered: ${STATUS:-<nothing>}). This runner predates auto-update;" >&2
      echo "    run install-runner.sh once by hand to pick it up." >&2
      exit 0
      ;;
  esac
fi

echo "==> fetching $REPO"
git -C "$REPO" fetch --quiet origin || echo "    (fetch failed — using local refs)"
if ! git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}" >/dev/null; then
  # In auto-update mode this is survivable and self-correcting (the sha may not
  # have reached this clone yet), so log and wait for the next cycle rather than
  # failing loudly every 30 minutes.
  [ "$IF_STALE" -eq 1 ] && { echo "    ref $REF not in this clone yet; will retry."; exit 0; }
  echo "no such ref: $REF" >&2; exit 1
fi

# The provenance the runner reports and the server compares against: the last
# commit that touched the runner's OWN source, NOT the repo HEAD (which moves on
# every canopy-web commit and would mark every runner stale on a CSS change).
RUNNER_SHA="$(git -C "$REPO" log -1 --format=%H "$REF" -- "$RUNNER_SRC")"
# Committer epoch of the SAME commit. The sha says WHICH code; this says WHEN, and
# only the pair can tell "this box is behind" from "this box is ahead of the
# deploy" — which the supervisor was reporting as the former either way.
RUNNER_COMMITTED_AT="$(git -C "$REPO" log -1 --format=%ct "$REF" -- "$RUNNER_SRC")"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [ -n "$RUNNER_SHA" ]; then
  echo "==> ref $REF | runner source at ${RUNNER_SHA:0:12}"
else
  # Say so rather than installing an anonymous runner. Empty is FAIL-SAFE (the
  # supervisor stays silent rather than alerting on unknown provenance), but
  # silence here is indistinguishable from the feature working.
  echo "==> ref $REF | WARNING: could not resolve the runner source sha (shallow clone?)."
  echo "    This runner will report unknown provenance and the staleness alert" >&2
  echo "    will never fire for it. Unshallow the checkout to fix." >&2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
git -C "$REPO" archive "$REF" | tar -x -C "$TMP"

# --- stage-2 handover -------------------------------------------------------
# The updater plist runs ~/.canopy/canopy-runner-update — a copy of this script
# frozen at the LAST install, which nothing between installs refreshes. So when
# the repo layout moves (this script's own path included), that stale copy would
# build from directories the archived ref no longer has. If the archived ref
# carries a different installer, hand over to THAT copy: it knows its own
# layout, and installs itself over the frozen copy on its way out. The
# guard env stops recursion; the mktemp copy survives `rm -rf $TMP` (exec never
# runs the EXIT trap) and is one tiny file /tmp cleanup reaps.
if [ -z "${CANOPY_INSTALLER_STAGE2:-}" ]; then
  ARCHIVED=""
  for cand in runner/canopy_runner/scripts/install-runner.sh packages/canopy_runner/scripts/install-runner.sh; do
    [ -f "$TMP/$cand" ] && { ARCHIVED="$TMP/$cand"; break; }
  done
  if [ -n "$ARCHIVED" ] && ! cmp -s "$0" "$ARCHIVED"; then
    echo "==> installer differs at $REF — handing over to the archived copy"
    STAGE2="$(mktemp /tmp/canopy-install-runner.XXXXXX)"
    cp "$ARCHIVED" "$STAGE2"
    rm -rf "$TMP"
    trap - EXIT
    CANOPY_INSTALLER_STAGE2=1 exec bash "$STAGE2" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
  fi
fi

# Stamp build provenance into the TEMP tree only — never the working checkout.
cat > "$TMP/$RUNNER_SRC/_build_info.py" <<EOF
"""Build provenance, stamped by install-runner.sh. Generated — do not edit."""
from __future__ import annotations

SHA = "$RUNNER_SHA"
BUILT_AT = "$BUILT_AT"
COMMITTED_AT = ${RUNNER_COMMITTED_AT:-0}
EOF

echo "==> building wheels"
for pkg in packages/canopy_cron packages/canopy_transcript runner/canopy_runner; do
  uv build --quiet --wheel -o "$TMP/dist" "$TMP/$pkg"
done

WHEEL="$(ls "$TMP"/dist/canopy_runner-*.whl)"
echo "==> installing $(basename "$WHEEL")"
# --find-links supplies canopy-cron / canopy-transcript (on no index) while the
# real index still serves croniter and the realtime extra's websocket-client.
# The runner itself is named by direct file:// URL so no index can shadow it.
#
# --python: ask for what the package REQUIRES, read from the snapshot, rather than
# letting uv take the first python3 on PATH. On a fresh macOS account with no
# ~/.zprofile, Homebrew sits behind /usr/bin, so that is Apple's 3.9 and the resolve
# fails outright ("the current Python version (3.9.6) does not satisfy >=3.11") —
# hit provisioning a third account on 2026-09-28. uv downloads a match if none exists.
PY_REQ="$(sed -n 's/^requires-python *= *"\(.*\)"/\1/p' "$TMP/runner/canopy_runner/pyproject.toml" | head -1)"
uv tool install --force ${PY_REQ:+--python "$PY_REQ"} \
  --find-links "$TMP/dist" "canopy-runner[realtime] @ file://$WHEEL"

# Ask uv where it PUT the executable rather than trusting PATH order — an older
# canopy-runner earlier on PATH would otherwise be what the plist points at, and
# the daemon would keep running the version this script just replaced.
BIN="$(uv tool dir --bin 2>/dev/null)/canopy-runner"
[ -x "$BIN" ] || BIN="$(command -v canopy-runner || true)"
[ -n "$BIN" ] && [ -x "$BIN" ] \
  || { echo "installed, but the canopy-runner executable was not found" >&2; exit 1; }

echo "==> provisioning the CDP sidecar's node deps"
"$BIN" install-sidecar

# --- pairing (a new account) --------------------------------------------------
# Before the daemon is loaded, because the daemon needs the config this writes.
# Never in --if-stale mode: the timer must not talk a box into a new identity. With
# no config there is nothing for a daemon to run as, so pairing is the only sensible
# move and is implied; `pair` itself refuses to create a second runner for a config
# that already names one, so --pair on a paired box is a cheap verification.
if [ "$IF_STALE" -eq 0 ] && { [ "$DO_PAIR" = "1" ] || { [ -z "$DO_PAIR" ] && [ ! -f "$CONFIG" ]; }; }; then
  echo "==> pairing this account (canopy-runner pair)"
  "$BIN" pair --config "$CONFIG" ${PAIR_ARGS[@]+"${PAIR_ARGS[@]}"} \
    || { echo "pairing did not complete — daemon NOT (re)started. Fix the above and re-run." >&2; exit 1; }
elif [ "${#PAIR_ARGS[@]}" -gt 0 ] && [ "$IF_STALE" -eq 0 ]; then
  echo "WARNING: pairing options ignored — $CONFIG exists (add --pair to verify it)." >&2
fi

# Render a plist template, validate it, then reload the job. Shared by the runner
# and the updater so their failure handling can't drift.
#
# Render to a staging file and VALIDATE before going anywhere near the running
# job: writing straight to the live path and then booting out means a malformed
# render leaves the job stopped with nothing to bootstrap — an "update" that
# silently kills the fleet's laptop. Validate first, swap last.
#
# `bootout` returns BEFORE launchd has finished tearing the job down, and a
# bootstrap issued into a domain still holding the old job fails with
# `Bootstrap failed: 5: Input/output error`, leaving it STOPPED. Hit on the very
# first real install (2026-07-29); a hand-run retry seconds later succeeded with
# no other change — the signature of a race, not a bad plist. So wait for the
# domain to clear, then retry anyway, because the print check races too.
#
# One job must never be bounced this way: the one we are RUNNING UNDER.
# `launchctl bootout` tears down the job's whole process tree, and in --if-stale
# mode that tree contains this script — so bootout kills us, `bootstrap` never
# runs, and the timer is left UNLOADED. Observed on a real box 2026-07-29: the
# updater fired, installed a new runner, and its log stops mid-sentence at
# "rendering …com.canopy.runner.updater.plist" — "(re)started" never printed and
# the job was gone from `launchctl list`. It comes back at the next login (or
# whenever something else runs an install), so this is not permanent; it just
# means auto-update goes dark after every successful self-update, which is
# precisely the run where it mattered.
#
# For our own job we therefore write the plist and stop. launchd reads it at
# next load, so a CHANGED definition takes effect at the next login rather than
# immediately — a fair price for a file that changes about never, and strictly
# better than a job that isn't running at all.
self_job() {
  [ "$IF_STALE" -eq 1 ] && [ "$1" = "$UPDATER_LABEL" ]
}

install_job() {
  local label="$1" src="$2" dest="$HOME/Library/LaunchAgents/$1.plist"
  local uid_n booted=0 err="" attempt changed=1
  uid_n="$(id -u)"

  echo "==> rendering $dest"
  mkdir -p "$HOME/Library/LaunchAgents"
  plutil -lint "$src" >/dev/null \
    || { echo "rendered plist for $label is not valid — leaving the running job alone" >&2; return 1; }
  cmp -s "$src" "$dest" 2>/dev/null && changed=0
  cp "$src" "$dest"

  if self_job "$label"; then
    if [ "$changed" -eq 1 ]; then
      echo "==> $label definition updated — loads at next login (not bounced: this install is running under it)"
    else
      echo "==> $label unchanged — left running (this install is running under it)"
    fi
    return 0
  fi

  launchctl bootout "gui/$uid_n/$label" 2>/dev/null || true
  for _ in $(seq 1 20); do
    launchctl print "gui/$uid_n/$label" >/dev/null 2>&1 || break
    sleep 0.5
  done
  for attempt in $(seq 1 5); do
    if err="$(launchctl bootstrap "gui/$uid_n" "$dest" 2>&1)"; then booted=1; break; fi
    [ "$attempt" -lt 5 ] && sleep 1
  done
  if [ "$booted" -eq 1 ]; then
    echo "==> (re)started $label"
    return 0
  fi
  # Loud, and non-zero: the job is now STOPPED, which is the one outcome nobody
  # notices on their own until turns stop being claimed.
  echo "ERROR: launchctl bootstrap failed after 5 attempts — $label is NOT running." >&2
  echo "       launchd said: $err" >&2
  echo "       Retry with: launchctl bootstrap gui/$uid_n $dest" >&2
  return 1
}

if [ "$DO_LAUNCHD" -eq 1 ]; then
  sed -e "s|__CANOPY_RUNNER_BIN__|$BIN|g" -e "s|__HOME__|$HOME|g" \
    "$TMP/runner/canopy_runner/launchd/$LABEL.plist.template" > "$TMP/runner.plist"
  install_job "$LABEL" "$TMP/runner.plist" || exit 1

  if [ "$DO_AUTO_UPDATE" -eq 1 ]; then
    # Install THIS script to a stable, self-describing path and point the timer
    # job at that — not at the copy in the repo.
    #
    # macOS names every entry in System Settings › Login Items › "App Background
    # Activity" after the basename of ProgramArguments[0]. Pointing at the repo
    # made canopy's updater show up as `install-runner.sh` — "Item from
    # unidentified developer" — a bare shell script running from a git checkout,
    # indistinguishable at a glance from something you would want to kill, and
    # with nothing tying it to canopy. It now reads `canopy-runner-update`.
    #
    # Copied from the SNAPSHOT ($TMP), not the working tree, for the same reason
    # everything else here is: what gets installed is the ref, never whatever
    # happens to be checked out. That also means a `git checkout` in the repo can
    # no longer change the script the timer executes — only an install can.
    INSTALLER="$HOME/.canopy/canopy-runner-update"
    mkdir -p "$HOME/.canopy"
    if install -m 755 "$TMP/runner/canopy_runner/scripts/install-runner.sh" "$INSTALLER"; then
      sed -e "s|__INSTALLER__|$INSTALLER|g" -e "s|__REPO__|$REPO|g" -e "s|__HOME__|$HOME|g" \
        "$TMP/runner/canopy_runner/launchd/$UPDATER_LABEL.plist.template" > "$TMP/updater.plist"
      # A failed updater is NOT fatal: the runner itself is installed and running,
      # and losing auto-update is strictly less bad than aborting the install.
      install_job "$UPDATER_LABEL" "$TMP/updater.plist" \
        || echo "WARNING: auto-update job not installed; updates stay manual." >&2
    else
      echo "WARNING: could not install $INSTALLER — auto-update job not installed." >&2
    fi
  fi
fi

# --- menu-bar app (macOS) ---------------------------------------------------
# The daemon and the menu-bar app are separate PROCESSES by design (the daemon must
# run headless), but nothing required installing them separately — and nothing did:
# a fresh account got a running runner and no status icon, and had to find
# runner/menubar/build.sh by hand (2026-09-28). Install it alongside the daemon.
#
# Rebuilt only when its source changes: build.sh replaces the bundle and relaunches
# the app, and --if-stale runs this every 30 minutes — an unconditional rebuild would
# blink the icon on every auto-update. Best-effort: no swiftc (Xcode CLT) is a
# warning, never a failed install. Opt out with CANOPY_MENUBAR=0.
MENUBAR="$TMP/runner/menubar"
MENUBAR_STAMP="$HOME/.canopy/menubar.sha"
MENUBAR_APP="${CANOPY_RUNNER_APP:-$HOME/Applications/Canopy Runner.app}"
if [ "$DO_LAUNCHD" -eq 1 ] && [ "$(uname -s)" = "Darwin" ] \
   && [ "${CANOPY_MENUBAR:-1}" = "1" ] && [ -f "$MENUBAR/build.sh" ]; then
  # The icons are part of the app: an icon-only change (assets/brand) must rebuild
  # it too, or a new mark never reaches a box whose Swift source did not move.
  want="$(cat "$MENUBAR/Sources/main.swift" "$MENUBAR/build.sh" \
            "$TMP/assets/brand/AppIcon.icns" "$TMP/assets/brand"/menubar-mark*.png 2>/dev/null \
          | shasum | cut -d' ' -f1)"
  have="$(cat "$MENUBAR_STAMP" 2>/dev/null || true)"
  if [ -d "$MENUBAR_APP" ] && [ "$want" = "$have" ]; then
    echo "==> menu-bar app unchanged"
  elif ! command -v swiftc >/dev/null 2>&1; then
    echo "WARNING: swiftc not found (xcode-select --install) — menu-bar app not built." >&2
  elif bash "$MENUBAR/build.sh"; then
    mkdir -p "$HOME/.canopy" && printf '%s\n' "$want" > "$MENUBAR_STAMP"
  else
    echo "WARNING: menu-bar app build failed — the runner itself is installed and running." >&2
  fi
fi

echo
"$BIN" --version

# The one step no script may take for you: emdash needs THIS account's debug port,
# and the port is a launch argument — so emdash has to be quit and relaunched, which
# kills every session running inside it. Say so only when it is actually needed.
if [ "$IF_STALE" -eq 0 ] && [ -f "$CONFIG" ]; then
  CDP_PORT="$(sed -n 's/.*"cdp_port" *: *\([0-9][0-9]*\).*/\1/p' "$CONFIG" | head -1)"
  if [ -n "$CDP_PORT" ] && ! curl -fsS -m 2 "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; then
    echo
    echo "!! emdash is not reachable on its debug port ($CDP_PORT), so the runner will report"
    echo "!! cdp_down and claim nothing. Relaunch emdash with the launcher:"
    echo "!!     open ~/Applications/\"Emdash CDP.app\"      (Spotlight: \"Emdash CDP\")"
    echo "!! It QUITS emdash first — every session running inside emdash is killed. Pick your moment."
  fi
fi
echo "done."
