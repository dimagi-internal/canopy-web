#!/usr/bin/env bash
#
# Install (or update) the canopy laptop runner as a SNAPSHOT of a git ref.
#
#   packages/canopy_runner/scripts/install-runner.sh                 # origin/main
#   packages/canopy_runner/scripts/install-runner.sh --ref my-branch # deliberately, a branch
#   packages/canopy_runner/scripts/install-runner.sh --no-launchd    # install only, don't touch the daemon
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
RUNNER_SRC="packages/canopy_runner/canopy_runner"
LABEL="com.canopy.runner"

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a git ref}"; shift 2 ;;
    --repo) REPO="${2:?--repo needs a path}"; shift 2 ;;
    --no-launchd) DO_LAUNCHD=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v uv >/dev/null || { echo "uv not found — https://docs.astral.sh/uv/" >&2; exit 1; }
# `rev-parse`, not `[ -d "$REPO/.git" ]`: in a git WORKTREE .git is a file, and
# the directory test would reject a perfectly good checkout.
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "not a git checkout: $REPO (set CANOPY_WEB_REPO)" >&2; exit 1; }

echo "==> fetching $REPO"
git -C "$REPO" fetch --quiet origin || echo "    (fetch failed — using local refs)"
git -C "$REPO" rev-parse --verify --quiet "$REF^{commit}" >/dev/null \
  || { echo "no such ref: $REF" >&2; exit 1; }

# The provenance the runner reports and the server compares against: the last
# commit that touched the runner's OWN source, NOT the repo HEAD (which moves on
# every canopy-web commit and would mark every runner stale on a CSS change).
RUNNER_SHA="$(git -C "$REPO" log -1 --format=%H "$REF" -- "$RUNNER_SRC")"
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

# Stamp build provenance into the TEMP tree only — never the working checkout.
cat > "$TMP/$RUNNER_SRC/_build_info.py" <<EOF
"""Build provenance, stamped by install-runner.sh. Generated — do not edit."""
from __future__ import annotations

SHA = "$RUNNER_SHA"
BUILT_AT = "$BUILT_AT"
EOF

echo "==> building wheels"
for pkg in canopy_cron canopy_transcript canopy_runner; do
  uv build --quiet --wheel -o "$TMP/dist" "$TMP/packages/$pkg"
done

WHEEL="$(ls "$TMP"/dist/canopy_runner-*.whl)"
echo "==> installing $(basename "$WHEEL")"
# --find-links supplies canopy-cron / canopy-transcript (on no index) while the
# real index still serves croniter and the realtime extra's websocket-client.
# The runner itself is named by direct file:// URL so no index can shadow it.
uv tool install --force --find-links "$TMP/dist" "canopy-runner[realtime] @ file://$WHEEL"

# Ask uv where it PUT the executable rather than trusting PATH order — an older
# canopy-runner earlier on PATH would otherwise be what the plist points at, and
# the daemon would keep running the version this script just replaced.
BIN="$(uv tool dir --bin 2>/dev/null)/canopy-runner"
[ -x "$BIN" ] || BIN="$(command -v canopy-runner || true)"
[ -n "$BIN" ] && [ -x "$BIN" ] \
  || { echo "installed, but the canopy-runner executable was not found" >&2; exit 1; }

echo "==> provisioning the CDP sidecar's node deps"
"$BIN" install-sidecar

if [ "$DO_LAUNCHD" -eq 1 ]; then
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  echo "==> rendering $PLIST"
  mkdir -p "$HOME/Library/LaunchAgents"
  # Render to a staging file and VALIDATE before going anywhere near the running
  # job. Writing straight to $PLIST and then booting out means a malformed
  # render leaves the daemon stopped with nothing to bootstrap — an "update"
  # that silently kills the fleet's laptop. Validate first, swap last.
  sed -e "s|__CANOPY_RUNNER_BIN__|$BIN|g" -e "s|__HOME__|$HOME|g" \
    "$TMP/packages/canopy_runner/launchd/$LABEL.plist.template" > "$TMP/rendered.plist"
  plutil -lint "$TMP/rendered.plist" >/dev/null \
    || { echo "rendered plist is not valid — leaving the running daemon alone" >&2; exit 1; }
  cp "$TMP/rendered.plist" "$PLIST"

  # bootout+bootstrap rather than kickstart: ProgramArguments changed, and
  # launchd keeps the OLD definition until the job is reloaded.
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
    echo "==> restarted $LABEL"
  else
    # Loud, and non-zero: the daemon is now STOPPED, which is the one outcome
    # nobody would notice on their own until turns stopped being claimed.
    echo "ERROR: launchctl bootstrap failed — the runner is NOT running." >&2
    echo "       Retry with: launchctl bootstrap gui/$(id -u) $PLIST" >&2
    exit 1
  fi
fi

echo
"$BIN" --version
echo "done."
