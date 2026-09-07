#!/usr/bin/env bash
# Bootstrap the canopy agent secret topology in 1Password.
#
# Idempotent. Creates the two-tier vault structure the Agent Runtime Registry
# reconciler resolves against:
#   Canopy-Shared      — secrets EVERY canopy agent needs
#   Agent-<Slug>       — one per agent: its own identity + integration secrets
#
# You run this yourself (via `!` in the Claude session, or a normal terminal) so
# the service-account token — the one credential that unlocks everything — stays
# in your hands and is never pasted into the conversation unless you choose to.
#
# Usage:
#   ./bootstrap_1password.sh echo ada hal eva
#
# Prereq: `op` CLI signed in to the Business account:
#   eval "$(op signin --account dimagi.1password.com)"
# (or enable the 1Password desktop-app CLI integration for Touch ID per command.)

set -euo pipefail

ACCOUNT="${OP_ACCOUNT:-dimagi.1password.com}"
# Overridable for the same reason bootstrap_agents.sh and wire.sh take it:
# tenants hold different values under the same item names, so the vault is a
# tenant fact, not a constant (Jonathan, 2026-09-07).
SHARED_VAULT="${CANOPY_SHARED_VAULT:-Canopy-Shared}"

# Per-agent items we scaffold as empty placeholders so `op://` references resolve
# immediately (the reconciler treats an empty field as "not provisioned yet" and
# surfaces it as needs-bootstrap rather than 404ing). Values are filled in later,
# either interactively (claude setup-token / gog login) or by the reconciler's
# write-back. Add fields here as the fleet's needs grow.
#   canopy-pat         — the agent's canopy-web PAT (pairs the runner, posts turns)
#   claude-oauth-token — the agent's `claude setup-token` (long-lived, non-rotating)
#   gog-token          — the agent's long-lived gog refresh token (In-Production app)
AGENT_ITEMS=("canopy-pat" "claude-oauth-token" "gog-token")

# Shared items every agent resolves from Canopy-Shared. The gog OAuth *client*
# (client_id+secret — "the app") is shared fleet-wide; only the per-agent MAILBOX
# (gog-token above) is the identity that must never bleed. An agent that runs its
# OWN gog client (echo is grandfathered on one) simply also gets a gog-oauth-client
# item in ITS vault — the reconciler resolves [Agent-<Slug>, Canopy-Shared] in
# order, so the per-agent client shadows the shared one with no special-casing.
SHARED_ITEMS=("gog-oauth-client")

log() { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }

ensure_signed_in() {
  if ! op whoami --account "$ACCOUNT" >/dev/null 2>&1; then
    echo "Not signed in to $ACCOUNT. Run:  eval \"\$(op signin --account $ACCOUNT)\"" >&2
    exit 1
  fi
}

ensure_vault() {
  local name="$1"
  if op vault get "$name" --account "$ACCOUNT" >/dev/null 2>&1; then
    log "vault exists: $name"
  else
    log "creating vault: $name"
    op vault create "$name" --account "$ACCOUNT" >/dev/null
  fi
}

ensure_placeholder_item() {
  # An "API Credential" item whose single 'credential' field is empty until minted.
  local vault="$1" title="$2"
  if op item get "$title" --vault "$vault" --account "$ACCOUNT" >/dev/null 2>&1; then
    log "  item exists: $vault/$title"
  else
    log "  scaffolding item: $vault/$title"
    op item create --category "API Credential" --title "$title" \
      --vault "$vault" --account "$ACCOUNT" "credential[password]=" >/dev/null
  fi
}

main() {
  if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <agent-slug> [agent-slug ...]" >&2
    exit 2
  fi
  ensure_signed_in

  log "=== Shared vault ==="
  ensure_vault "$SHARED_VAULT"
  for item in "${SHARED_ITEMS[@]}"; do
    ensure_placeholder_item "$SHARED_VAULT" "$item"
  done

  for slug in "$@"; do
    # Capitalize first letter for the vault display name: echo -> Agent-Echo.
    local vault="Agent-$(printf '%s' "${slug:0:1}" | tr '[:lower:]' '[:upper:]')${slug:1}"
    log "=== Agent: $slug -> vault $vault ==="
    ensure_vault "$vault"
    for item in "${AGENT_ITEMS[@]}"; do
      ensure_placeholder_item "$vault" "$item"
    done
  done

  cat <<EOF

$(printf '\033[1;32m✓ Vault topology ready.\033[0m')

Next (owner-only) — mint the service-account token the runner uses. Grant it
READ on the shared vault, and READ+WRITE on the agent vaults it may run so the
reconciler can persist minted tokens (claude setup-token) back for cold boxes:

  op service-account create "canopy-cloud-runner" \\
    --account $ACCOUNT \\
    --vault "$SHARED_VAULT:read_items" \\$(for slug in "$@"; do
      v="Agent-$(printf '%s' "${slug:0:1}" | tr '[:lower:]' '[:upper:]')${slug:1}"
      printf '\n    --vault "%s:read_items,write_items" \\' "$v"
    done)
    --expires-in 90d

The command prints the token ONCE. Then store it for the cloud runner:

  aws secretsmanager put-secret-value \\
    --secret-id canopy/cloud-runner/op-service-account-token \\
    --secret-string "<TOKEN>"   # or create-secret the first time

For your laptop, export it (or the reconciler reads it from your op signin):
  export OP_SERVICE_ACCOUNT_TOKEN="<TOKEN>"
EOF
}

main "$@"
