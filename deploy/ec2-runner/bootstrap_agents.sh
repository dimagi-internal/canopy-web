#!/usr/bin/env bash
# deploy/ec2-runner/bootstrap_agents.sh — idempotent agent-fleet bootstrap for the
# canopy cloud runner (EC2, Ubuntu 24.04). Runs as the service user (`ubuntu`), NOT
# root — cloud-init's runcmd pre-creates $AGENT_ROOT / /opt/canopy-web with ubuntu
# ownership (see runner.cfn.yaml) so this script never needs sudo for its own state,
# only (optionally) to drop a fetched binary into /usr/local/bin.
#
# Invoked from cloud_runner.py's main(), AFTER fetch_and_stage_credential() has
# populated CANOPY_TOKEN / OP_SERVICE_ACCOUNT_TOKEN / the git credential store —
# deliberately NOT from cloud-init's ExecStartPre, which fires on every service
# start before those credentials exist (a fresh pairing has none yet; the operator
# stages them via wire.sh after the runner first appears in the fleet). Cloning the
# PRIVATE per-agent repos (github.com/dimagi-internal/<slug>) and provisioning
# their env via `op inject` (1Password reads) both need that credential bundle —
# see the ordering note in cloud_runner.py where this is invoked.
#
# Steps below mirror docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md
# §1. Each step is OK-skipped when already satisfied. Deliberately NOT `set -e`:
# one agent's failure must not take down the other four (step 5) — the runner still
# comes up and serves whichever agents bootstrapped clean; a readiness drill is the
# per-agent verdict, not this script's exit code.
set -uo pipefail

AGENT_SLUGS="${AGENT_SLUGS:-ace,ada,echo,eva,hal}"
AGENT_ROOT="${AGENT_ROOT:-/opt/agents}"
AGENT_REPO_ORG="${AGENT_REPO_ORG:-dimagi-internal}"
CANOPY_PLUGIN_URL="${CANOPY_PLUGIN_URL:-https://github.com/jjackson/canopy.git}"

log()  { printf '[bootstrap-agents] %s\n' "$*"; }
ok()   { printf '[bootstrap-agents] OK: %s\n' "$*"; }
warn() { printf '[bootstrap-agents] WARN: %s\n' "$*" >&2; }
fail() { printf '[bootstrap-agents] FAIL: %s\n' "$*" >&2; }

# gogcli's own account -> OAuth-client map (verified against a live ~/.config/gogcli
# aka macOS "Library/Application Support/gogcli" config.json, `account_clients`
# key): ace and echo keep dedicated clients; ada/eva/hal share the fleet's `canopy`
# app. See docs/architecture/shared-gog-gdrive.md.
declare -A GOG_CLIENT=( [ace]=ace [ada]=canopy [echo]=echo [eva]=canopy [hal]=canopy )

# ── gog's own XDG resolution on Linux (mirrors canopy's agent_email.py
# _default_gog_config_dir — $GOG_HOME override, else $XDG_CONFIG_HOME/gogcli, else
# ~/.config/gogcli; there is no macOS branch on this box). ──────────────────────
gog_config_dir() {
  if [[ -n "${GOG_HOME:-}" ]]; then
    printf '%s\n' "${GOG_HOME/#\~/$HOME}"
  else
    printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/gogcli"
  fi
}

vault_name() {  # ace -> Agent-Ace (bash 5, shipped on Ubuntu 24.04: ${var^} title-cases)
  local slug="$1"
  printf 'Agent-%s\n' "${slug^}"
}

FAILED_AGENTS=()
READY_AGENTS=()

# ── Step 1: tooling ─────────────────────────────────────────────────────────────
step1_tooling() {
  log "step 1: tooling"

  if ! command -v uv >/dev/null 2>&1; then
    log "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh || warn "uv install failed"
  fi
  export PATH="$HOME/.local/bin:$PATH"

  if command -v uv >/dev/null 2>&1; then
    if ! command -v canopy >/dev/null 2>&1; then
      log "installing canopy CLI (uv tool, ${CANOPY_PLUGIN_URL})"
      uv tool install --force "git+${CANOPY_PLUGIN_URL}" || warn "canopy CLI install failed"
    else
      ok "canopy CLI already installed ($(canopy --version 2>/dev/null || echo '?'))"
    fi
  else
    warn "uv not on PATH — cannot install/verify the canopy CLI"
  fi

  if ! command -v gog >/dev/null 2>&1; then
    log "installing gog (latest steipete/gogcli linux release)"
    if install_gog; then ok "gog installed"; else warn "gog install failed — per-agent gmail steps below will be skipped"; fi
  else
    ok "gog already on PATH ($(gog --version 2>/dev/null | head -1 || echo '?'))"
  fi

  for bin in op gh claude git; do
    if command -v "$bin" >/dev/null 2>&1; then
      ok "$bin on PATH"
    else
      warn "$bin NOT on PATH — an earlier cloud-init step likely failed; see /var/log/cloud-init-output.log"
    fi
  done
}

install_gog() {
  # No version pin — always the latest release; tolerate failure loudly (this is
  # the one step with no local fallback if it fails, so per-agent gmail work is
  # simply unavailable this run, not a bootstrap-wide failure).
  local tmp
  tmp="$(mktemp -d)" || return 1
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN

  if command -v gh >/dev/null 2>&1; then
    # `gh release download` with no tag pulls the LATEST release; a token is
    # optional for a public repo but avoids the unauthenticated 60/hr rate limit.
    # Fall back to the inherited GH_TOKEN: cloud_runner now exports it when it
    # stages the credential bundle, and a bare `GH_TOKEN="${GITHUB_TOKEN:-}"`
    # would BLANK that inherited value for this one call.
    if ! GH_TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}" gh release download -R steipete/gogcli \
        --pattern 'gogcli_*_linux_amd64.tar.gz' --dir "$tmp" --clobber 2>&1; then
      warn "gh release download failed; falling back to the GitHub API + curl"
    fi
  fi

  local tarball
  tarball="$(find "$tmp" -maxdepth 1 -name 'gogcli_*_linux_amd64.tar.gz' | head -1)"
  if [[ -z "$tarball" ]]; then
    local url
    url=$(curl -fsSL https://api.github.com/repos/steipete/gogcli/releases/latest \
      | python3 -c 'import json,sys
d=json.load(sys.stdin)
for a in d.get("assets", []):
    if a["name"].endswith("_linux_amd64.tar.gz"):
        print(a["browser_download_url"]); break' 2>/dev/null)
    [[ -n "$url" ]] || { fail "could not resolve the latest gogcli linux_amd64 asset"; return 1; }
    curl -fsSL "$url" -o "$tmp/gog.tar.gz" || { fail "curl download of $url failed"; return 1; }
    tarball="$tmp/gog.tar.gz"
  fi

  tar -xzf "$tarball" -C "$tmp" gog || { fail "could not extract 'gog' from $tarball"; return 1; }
  if sudo -n install -m 0755 "$tmp/gog" /usr/local/bin/gog 2>/dev/null; then
    return 0
  fi
  # No passwordless sudo (unexpected on the stock Ubuntu cloud-init AMI, but don't
  # brick the run over it) — fall back to the user's own bin dir.
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$tmp/gog" "$HOME/.local/bin/gog"
}

# ── Step 2: gog keyring + account->client map ───────────────────────────────────
step2_gog_config() {
  log "step 2: gog keyring + account/client map"
  if ! command -v gog >/dev/null 2>&1; then
    warn "gog not installed — skipping keyring + config.json setup"
    return
  fi
  # Headless Linux has no OS keychain/Secret Service; `file` stores tokens
  # encrypted-at-rest under gog's own config dir instead. Idempotent (re-setting
  # the same backend is a no-op).
  gog auth keyring file >/dev/null 2>&1 && ok "gog keyring backend = file" \
    || warn "could not set gog keyring backend to 'file'"

  local dir; dir="$(gog_config_dir)"
  mkdir -p "$dir"
  local cfg="$dir/config.json"
  # Bash associative arrays don't cross into a heredoc's subshell, so resolve
  # slug->client->email into plain "email=client" pairs here and hand those to
  # python (below) to merge into config.json's `account_clients` map.
  local slugs=(); IFS=',' read -ra slugs <<<"$AGENT_SLUGS"
  local pairs=()
  for slug in "${slugs[@]}"; do
    local client="${GOG_CLIENT[$slug]:-$slug}"
    pairs+=("${slug}@dimagi-ai.com=${client}")
  done
  python3 - "$cfg" "${pairs[@]}" <<'PY'
import json, sys
cfg_path, pairs = sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(cfg_path))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
data.setdefault("account_clients", {})
for p in pairs:
    email, client = p.split("=", 1)
    data["account_clients"][email] = client
with open(cfg_path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  ok "wrote $cfg (account_clients for: ${AGENT_SLUGS})"
}

# ── Step 3: per-agent clone + provision + gmail token ───────────────────────────
clone_or_pull() {  # url dest
  local url="$1" dest="$2"
  if [[ -d "$dest/.git" ]]; then
    git -C "$dest" pull --ff-only
  else
    git clone --depth 1 "$url" "$dest"
  fi
}

bootstrap_one_agent() {
  local slug="$1"
  local dest="$AGENT_ROOT/$slug"
  local client="${GOG_CLIENT[$slug]:-$slug}"
  local account="${slug}@dimagi-ai.com"
  local vault; vault="$(vault_name "$slug")"

  log "── agent $slug ──"

  if ! clone_or_pull "https://github.com/${AGENT_REPO_ORG}/${slug}.git" "$dest"; then
    fail "$slug: clone/pull of ${AGENT_REPO_ORG}/${slug} failed (private repo — is the staged GitHub token valid?)"
    FAILED_AGENTS+=("$slug")
    return
  fi
  ok "$slug: repo at $dest"

  # Provision the agent's env the 1Password-NATIVE way: `op inject` resolves the
  # tracked `.env.tpl` (KEY=op://... lines) into the worktree-clean global home
  # ~/.<slug>/.env. This is the fleet standard — one injector (op inject), no
  # bespoke manifest tool. `bin/_env.py` in each agent reads ~/.<slug>/.env.
  local env_tpl="$dest/.env.tpl"
  local env_out="$HOME/.${slug}/.env"
  if [[ -f "$env_tpl" ]]; then
    mkdir -p "$(dirname "$env_out")"
    # --account isn't needed with a service-account token (OP_SERVICE_ACCOUNT_TOKEN);
    # op inject writes the resolved file, or errors and writes nothing.
    if op inject -i "$env_tpl" -o "$env_out" -f >/dev/null 2>&1; then
      chmod 0600 "$env_out"
      ok "$slug: op inject .env.tpl -> $env_out"
    else
      warn "$slug: op inject failed (unresolved op:// ref, or .env.tpl not migrated to a per-agent vault?) — agent may be partially ready"
    fi
  else
    warn "$slug: no .env.tpl in the repo — nothing to inject (does this agent declare .env.tpl provisioning?)"
  fi

  # The gog OAuth-client credential FILE (not an env var): a single 1Password
  # field materialized with native `op read` (no second injector). The shared
  # `canopy` client's item lives in Canopy-Shared; an agent with its OWN client
  # (echo, ace) keeps it in that agent's vault.
  local gog_dir; gog_dir="$(gog_config_dir)"
  local client_file="$gog_dir/credentials-${client}.json"
  local client_vault; [[ "$client" == "canopy" ]] && client_vault="Canopy-Shared" || client_vault="$vault"
  if command -v op >/dev/null 2>&1 && [[ ! -f "$client_file" ]]; then
    mkdir -p "$gog_dir"
    if op read "op://${client_vault}/gog-oauth-client/credential" >"$client_file" 2>/dev/null && [[ -s "$client_file" ]]; then
      chmod 0600 "$client_file"
      ok "$slug: gog client creds -> $client_file"
    else
      rm -f "$client_file"
      warn "$slug: op read op://${client_vault}/gog-oauth-client/credential failed — gmail may not authorize"
    fi
  fi

  if ! command -v gog >/dev/null 2>&1; then
    warn "$slug: gog unavailable — skipping gmail token import"
  elif gog gmail search --account "$account" --client "$client" in:inbox --max 1 >/dev/null 2>&1; then
    ok "$slug: gmail token already live (account=$account client=$client)"
  else
    log "$slug: gmail token not live — importing from op://${vault}/gog-token/credential"
    local tokfile; tokfile="$(mktemp)"
    if op read "op://${vault}/gog-token/credential" >"$tokfile" 2>/dev/null && [[ -s "$tokfile" ]]; then
      # Capture stderr instead of discarding it. Swallowing it here is what hid a
      # fleet-wide failure for weeks: the `file` keyring backend wants a password
      # it can only PROMPT for, so on this TTY-less box EVERY import died with
      # "no TTY available ... set GOG_KEYRING_PASSWORD" and all anyone ever saw
      # was a bare "import failed".
      local importerr
      if importerr="$(gog auth tokens import "$tokfile" 2>&1 >/dev/null)"; then
        ok "$slug: gmail token imported"
      else
        warn "$slug: gog auth tokens import failed: ${importerr:-(no output)}"
        [[ -n "${GOG_KEYRING_PASSWORD:-}" ]] || \
          warn "$slug: GOG_KEYRING_PASSWORD is unset — stage it with ./secrets.sh gog"
      fi
    else
      warn "$slug: op read op://${vault}/gog-token/credential failed — is the item staged for this vault?"
    fi
    shred -u "$tokfile" 2>/dev/null || rm -f "$tokfile"  # never leave the token on disk, even on failure
  fi

  READY_AGENTS+=("$slug")
}

step3_agents() {
  log "step 3: per-agent clone + provision + gmail token"
  mkdir -p "$AGENT_ROOT"
  local slugs=(); IFS=',' read -ra slugs <<<"$AGENT_SLUGS"
  for slug in "${slugs[@]}"; do
    bootstrap_one_agent "$slug"
  done
}

# ── Step 4: claude plugins ───────────────────────────────────────────────────────
step4_claude_plugins() {
  log "step 4: claude plugin marketplace + install"
  if ! command -v claude >/dev/null 2>&1; then
    warn "claude CLI not on PATH — skipping plugin setup"
    return
  fi
  if claude plugin marketplace list 2>/dev/null | grep -qE '(^|[[:space:]])canopy$'; then
    ok "canopy marketplace already added"
  else
    claude plugin marketplace add "$CANOPY_PLUGIN_URL" \
      && ok "added canopy marketplace" \
      || warn "claude plugin marketplace add failed"
  fi
  if claude plugin list 2>/dev/null | grep -q 'canopy@canopy'; then
    ok "canopy@canopy already installed"
  else
    claude plugin install canopy@canopy \
      && ok "installed canopy@canopy" \
      || warn "claude plugin install canopy@canopy failed"
  fi
  reinstall_cli_from_marketplace_clone
}

reinstall_cli_from_marketplace_clone() {
  # Step 1 installs the canopy CLI with `uv tool install git+<url>` because the
  # marketplace clone does not exist yet at that point. That leaves a VCS install,
  # and `canopy doctor` fails its CLI-install-source check for it:
  #   "uv-receipt.toml records no directory requirement"
  # It is not cosmetic — provenance is what lets /canopy:update track the local
  # clone rather than silently reinstalling from a moving remote. Now that step 4
  # has materialized the clone, re-point the CLI at it. Idempotent: skipped once
  # the receipt already records a directory requirement.
  local clone="$HOME/.claude/plugins/marketplaces/canopy"
  local receipt="$HOME/.local/share/uv/tools/canopy/uv-receipt.toml"
  if [[ ! -d "$clone" ]]; then
    warn "marketplace clone not at $clone — leaving the CLI on its VCS install"
    return
  fi
  if grep -q 'directory = ' "$receipt" 2>/dev/null; then
    ok "canopy CLI already installed from a directory requirement"
    return
  fi
  if uv tool install --force --reinstall "$clone" >/dev/null 2>&1; then
    ok "canopy CLI re-pointed at the marketplace clone ($clone)"
  else
    warn "could not re-point the canopy CLI at $clone — doctor will flag its provenance"
  fi
}

# ── Step 5: readiness summary ────────────────────────────────────────────────────
step5_summary() {
  log "step 5: readiness summary"
  log "agents attempted: ${AGENT_SLUGS}"
  log "agents with a clone + provision pass: ${READY_AGENTS[*]:-(none)}"
  if [[ ${#FAILED_AGENTS[@]} -gt 0 ]]; then
    warn "agents that failed to clone: ${FAILED_AGENTS[*]}"
  fi
  # Fail loud only on TOTAL failure — a partial fleet still leaves the runner
  # serving whichever agents came up clean; readiness drills are the per-agent
  # verdict, not this exit code.
  if [[ ${#READY_AGENTS[@]} -eq 0 && -n "$AGENT_SLUGS" ]]; then
    fail "no agent bootstrapped cleanly out of: ${AGENT_SLUGS}"
    return 1
  fi
  return 0
}

main() {
  step1_tooling
  step2_gog_config
  step3_agents
  step4_claude_plugins
  step5_summary
}

main "$@"
