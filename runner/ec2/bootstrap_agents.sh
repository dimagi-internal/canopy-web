#!/usr/bin/env bash
# runner/ec2/bootstrap_agents.sh — idempotent agent-fleet bootstrap for the
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
CANOPY_PLUGIN_URL="${CANOPY_PLUGIN_URL:-https://github.com/dimagi-internal/canopy.git}"

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
    # --retry: this download is flaky in practice. Standing up a box on 2026-08-12
    # took three attempts (a 503, then a dropped connection, then success), and a
    # bare curl turns each blip into the same silent outcome as the layout bug —
    # no gog, so no keyring, no client map, and no gmail token for any agent.
    curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused "$url" -o "$tmp/gog.tar.gz" \
      || { fail "curl download of $url failed after retries"; return 1; }
    tarball="$tmp/gog.tar.gz"
  fi

  # Extract EVERYTHING, then locate the binary — never name the member. gogcli
  # 0.35.0 stores it as `./gog`, and `tar -xzf … gog` does not match that: a
  # from-scratch rebuild on 2026-08-12 died here with "tar: gog: Not found in
  # archive", taking gog with it and so silently skipping the keyring, the
  # account->client map, and the gmail-token import for ALL FIVE agents. The box
  # came up healthy in every other respect and could not send a single email.
  # This is a third-party archive on a `latest` pin, so its layout is not ours to
  # rely on; find the binary wherever the tarball happens to put it.
  tar -xzf "$tarball" -C "$tmp" || { fail "could not unpack $tarball"; return 1; }
  local gogbin
  gogbin="$(find "$tmp" -type f -name gog -perm -u+x 2>/dev/null | head -1)"
  [[ -n "$gogbin" ]] || { fail "no executable 'gog' inside $tarball (layout changed?)"; return 1; }
  if sudo -n install -m 0755 "$gogbin" /usr/local/bin/gog 2>/dev/null; then
    return 0
  fi
  # No passwordless sudo (unexpected on the stock Ubuntu cloud-init AMI, but don't
  # brick the run over it) — fall back to the user's own bin dir.
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$gogbin" "$HOME/.local/bin/gog"
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

# ── an agent's OWN plugin + provisioner ─────────────────────────────────────────
# Cloning an agent's repo and rendering its .env is not the same as making the
# agent USABLE. An agent whose capability set ships as a Claude Code plugin
# (skills, slash commands AND mcpServers, declared in .claude-plugin/) has none
# of it until the plugin is installed — step 4 only ever installed `canopy`.
#
# Observed on cloud-ec2-1, 2026-07-28: a turn targeting `ace` had no /ace:*
# commands and no ace MCP tools at all, because the ACE plugin was never
# installed. `claude plugin list` showed canopy and nothing else.
#
# Both helpers are declarative on the agent side: an agent OPTS IN by shipping
# `.claude-plugin/marketplace.json` / `bin/<slug>-setup`. Nothing here knows
# anything ACE-specific, and an agent that ships neither is untouched.

install_agent_plugin() {
  local slug="$1" dest="$2"
  [[ -f "$dest/.claude-plugin/marketplace.json" ]] || return 0
  if ! command -v claude >/dev/null 2>&1; then
    warn "$slug: claude CLI not on PATH — plugin not installed"
    return 0
  fi
  # Directory source: the marketplace IS the clone, so `git pull` above is also
  # how the plugin updates. No second copy to keep in sync.
  if claude plugin marketplace list 2>/dev/null | grep -qE "(^|[[:space:]])${slug}\$"; then
    ok "$slug: marketplace already added"
  else
    claude plugin marketplace add "$dest" >/dev/null 2>&1 \
      && ok "$slug: added marketplace from $dest" \
      || { warn "$slug: claude plugin marketplace add failed"; return 0; }
  fi
  if claude plugin list 2>/dev/null | grep -q "${slug}@${slug}"; then
    ok "$slug: plugin already installed"
  else
    claude plugin install "${slug}@${slug}" >/dev/null 2>&1 \
      && ok "$slug: installed plugin ${slug}@${slug}" \
      || warn "$slug: claude plugin install ${slug}@${slug} failed"
  fi
}

run_agent_provisioner() {
  local slug="$1" dest="$2"
  local setup="$dest/bin/${slug}-setup"
  [[ -x "$setup" || -f "$setup" ]] || return 0

  # The agent's own installer knows what it needs (service-account key documents,
  # npm deps, CLI jars). Reimplementing any of that here would fork it.
  local data_dir="$HOME/.claude/plugins/data/${slug}-${slug}"
  mkdir -p "$data_dir"
  if CLAUDE_PLUGIN_DATA="$data_dir" timeout 900 bash "$setup" --skip-doctor >/dev/null 2>&1; then
    ok "$slug: ran bin/${slug}-setup"
  else
    warn "$slug: bin/${slug}-setup failed or timed out — agent may be partially ready"
  fi

  # A DIRECTORY-source plugin runs from the clone, not from
  # plugins/cache/<mp>/<plugin>/<version>/ — so the cache-path derivation an MCP
  # server uses to find its data dir yields nothing, and it falls back to
  # <plugin-root>/.gws-sa-key.json. The provisioner wrote the canonical data-dir
  # copy (right for a git-source install); mirror it to the fallback so BOTH
  # layouts resolve.
  #
  # Without this the MCP server starts, exposes its tools, and every call fails
  # with Google's "Method doesn't allow unregistered callers" — which reads like
  # a permissions problem and is really a path problem. Verified: staging this
  # file is what turned that error into a successful Drive listing.
  if [[ -f "$data_dir/gws-sa-key.json" ]]; then
    install -m 600 "$data_dir/gws-sa-key.json" "$dest/.gws-sa-key.json" 2>/dev/null \
      && ok "$slug: staged service-account key at the plugin root" \
      || warn "$slug: could not stage the service-account key at $dest"
  fi
  if [[ -f "$data_dir/.env" && ! -f "$dest/.env" ]]; then
    install -m 600 "$data_dir/.env" "$dest/.env" 2>/dev/null || true
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

  install_agent_plugin "$slug" "$dest"
  run_agent_provisioner "$slug" "$dest"

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

# Plugin MCP servers are declared as `npx tsx <plugin-root>/mcp/<server>.ts`, and
# Claude Code spawns them with cwd set to the TURN's working directory — which,
# for a session turn, is a bare scratch dir with no node_modules. `npx` then
# tries to fetch tsx from the registry on every server start and races Claude
# Code's ~30s MCP connection timeout, so the tools silently never appear ("those
# MCP servers are still connecting"). Same trap ace-web recorded in
# docs/learnings/mcp-bootstrap-container-traps.md.
#
# A global tsx makes that resolution instant (measured on this box: 0.46s from a
# cwd with no node_modules, versus a registry install). Idempotent and cheap, so
# it runs on every bootstrap rather than only at instance creation — cloud-init's
# runcmd fires once per INSTANCE, and this file has to work on the boxes already
# running.
ensure_plugin_runtime() {
  command -v npm >/dev/null 2>&1 || { warn "npm not on PATH — skipping tsx"; return 0; }
  if command -v tsx >/dev/null 2>&1; then
    ok "tsx already on PATH (plugin MCP servers resolve without a registry fetch)"
    return 0
  fi
  # --prefix "$HOME/.local", NOT a bare `npm i -g`: this script runs as the
  # SERVICE user (ubuntu), which cannot write /usr/lib/node_modules, so a plain
  # global install fails with EACCES. $HOME/.local/bin is already first on the
  # runner unit's PATH (see runner.cfn.yaml), so a binary here is found by the
  # `npx` that plugin MCP servers spawn.
  npm i -g --prefix "$HOME/.local" tsx >/dev/null 2>&1 \
    && ok "installed tsx into $HOME/.local (plugin MCP servers resolve locally)" \
    || warn "could not install tsx — plugin MCP servers may time out connecting"
}

step3_agents() {
  log "step 3: per-agent clone + provision + gmail token"
  ensure_plugin_runtime
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
