#!/bin/bash
# In-VM idle watchdog for an on-demand capability (layer 2 of 3).
#
# The consumer touches the activity marker over SSM whenever it uses the box
# (canopy_sdk.ondemand.OnDemandInstance.touch). When the marker is older than
# IDLE_SECONDS, halt: the instance is launched with
# InstanceInitiatedShutdownBehavior=stop, so a halt STOPS it (never terminates).
# The CloudWatch IdleStopAlarm in the capability's stack is layer 3.
#
# A MISSING marker is "not idle yet": /var/run is tmpfs, so every boot starts
# without one, and a box must not halt before its first consumer has touched it.
#
# Config (environment; ondemand-idle-shutdown.service reads
# /etc/default/ondemand-idle-shutdown):
#   CAPABILITY     capability name; sets the default marker path
#   ACTIVITY_FILE  marker path (default /var/run/$CAPABILITY/last-activity)
#   IDLE_SECONDS   idle threshold in seconds (default 3600)
# Pass --dry-run to print the decision without halting.
set -euo pipefail

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

if [ -z "${ACTIVITY_FILE:-}" ]; then
  : "${CAPABILITY:?set CAPABILITY (or ACTIVITY_FILE) in /etc/default/ondemand-idle-shutdown}"
  ACTIVITY_FILE="/var/run/${CAPABILITY}/last-activity"
fi
IDLE_SECONDS="${IDLE_SECONDS:-3600}"
stamp() { date -u +%FT%TZ; }

if [ ! -e "$ACTIVITY_FILE" ]; then
  echo "[$(stamp)] $ACTIVITY_FILE absent (not touched since boot); staying up"
  exit 0
fi

# `date -r FILE` prints FILE's mtime on both GNU and BSD date.
last=$(date -r "$ACTIVITY_FILE" +%s)
delta=$(( $(date +%s) - last ))

if (( delta < IDLE_SECONDS )); then
  echo "[$(stamp)] $ACTIVITY_FILE idle ${delta}s (< ${IDLE_SECONDS}s); staying up"
  exit 0
fi

echo "[$(stamp)] $ACTIVITY_FILE idle ${delta}s (>= ${IDLE_SECONDS}s); halting"
if (( dry_run )); then
  echo "would shut down"
else
  shutdown -h now
fi
