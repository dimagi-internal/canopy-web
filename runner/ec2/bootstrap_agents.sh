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
# stages them via wire.sh after the runner first appears in the fleet). Preparing the
# agents (`canopy agent prepare --runner`, which clones private repos and reads each
# agent's vault) needs that credential bundle — see the ordering note in
# cloud_runner.py where this is invoked.
#
# WHAT THIS SCRIPT IS NOW (2026-10-15, canopy docs/architecture/agent-setup-lifecycle.md):
# the BOX's half only — tooling, gog's file keyring, the canopy plugin + CLI, caches. Each
# AGENT is prepared by canopy's `canopy agent prepare --runner` (clone, env, plugins,
# provisioner, Gmail, readiness report): ONE implementation, shared with laptops and with
# cloud_runner's lazy path. The 1,000 lines of per-agent bash that used to live here — and
# every lesson they recorded — moved into canopy's agent_prepare.py and its tests, after a
# standby drill (cloud-ec2-prep) matched this box's readiness agent for agent.
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
# Where an agent's DECLARED plugin dependencies are cloned. Two constraints:
#   - NOT under AGENT_ROOT: everything there is an AGENT, and a turn resolves its
#     working dir from that tree, so a plugin clone would read as a sixth agent.
#   - NOT elsewhere under /opt: that is root-owned and this script runs as the
#     service user. `mkdir -p /opt/agent-plugins` fails with EACCES, which is how
#     the first version of this silently installed nothing.
# So: beside the marketplace clones the Claude CLI already keeps in $HOME.
PLUGIN_DEPS_ROOT="${PLUGIN_DEPS_ROOT:-$HOME/.claude/plugins/agent-deps}"
SCRIPT_DIR="${SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
CANOPY_PLUGIN_URL="${CANOPY_PLUGIN_URL:-https://github.com/dimagi-internal/canopy.git}"

log()  { printf '[bootstrap-agents] %s\n' "$*"; }
ok()   { printf '[bootstrap-agents] OK: %s\n' "$*"; }
warn() { printf '[bootstrap-agents] WARN: %s\n' "$*" >&2; }
fail() { printf '[bootstrap-agents] FAIL: %s\n' "$*" >&2; }

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

# ── The fleet roster comes from canopy-web (Agent Runtime Registry) ────────────
# It used to be pinned in THREE places — the AgentSlugs CFN parameter, AGENT_SLUGS
# in runner.env, and this script's default — so adding an agent meant editing the
# CloudFormation stack. canopy-web already serves the answer
# (`GET /api/agents/{slug}/runtime`: "what a runner needs from canopy-web to run
# this agent"); nothing consumed it. Now it does, and AGENT_SLUGS is the OFFLINE
# fallback rather than the source. Register an agent in canopy-web and the next
# bootstrap picks it up — no stack change, no SSH.
#
# Spec: docs/superpowers/specs/2026-07-20-agent-runtime-registry-design.md
#
# EVERY failure path here yields an EMPTY registry, never a non-zero exit: under
# `set -e` an unreachable canopy-web would otherwise abort bootstrap and strand a
# box that could perfectly well provision the agents it already knows.
_REGISTRY_CACHE=""
_REGISTRY_READ=""
agent_registry() {  # -> one slug per line, or nothing
  # `:-` so the function does not depend on the initialisers above having run
  # (extracted-function tests, and any future sourcing order).
  if [[ -n "${_REGISTRY_READ:-}" ]]; then
    printf '%s' "${_REGISTRY_CACHE:-}"
    return 0
  fi
  _REGISTRY_READ=1
  local base="${CANOPY_BASE_URL:-}" tok="${CANOPY_TOKEN:-}"
  [[ -n "$base" && -n "$tok" ]] || return 0
  local list
  list="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
          "${base%/}/api/agents/?limit=200" 2>/dev/null)" || list=""
  if [[ -z "$list" ]]; then
    # A token whose workspaces span more than one tenant tree gets a 422 on the
    # flat list (canopy-web's read rule, 2026-10-11): name each workspace instead.
    local _wss _ws
    _wss="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
            "${base%/}/api/workspaces/" 2>/dev/null | python3 -c '
import json, sys
try:
    print("\n".join(w["slug"] for w in json.load(sys.stdin)))
except Exception:
    pass
' 2>/dev/null)" || _wss=""
    list="$(while IFS= read -r _ws; do
        [[ -n "$_ws" ]] || continue
        curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
          "${base%/}/api/w/${_ws}/agents/?limit=200" 2>/dev/null || true
        echo
      done <<<"$_wss" | python3 -c '
import json, sys
seen = {}
for line in sys.stdin:
    try:
        d = json.loads(line)
    except Exception:
        continue
    for a in (d.get("items") if isinstance(d, dict) else d) or []:
        if (a or {}).get("slug"):
            seen.setdefault(a["slug"], a)
if seen:
    print(json.dumps({"items": list(seen.values())}))
' 2>/dev/null)" || list=""
  fi
  [[ -n "$list" ]] || return 0
  local slugs
  slugs="$(printf '%s' "$list" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
items = d.get("items") if isinstance(d, dict) else d
for a in (items or []):
    slug = (a or {}).get("slug") or ""
    if slug:
        print(slug)
' 2>/dev/null)" || return 0
  [[ -n "$slugs" ]] || return 0
  # Slugs only. Each agent's repo, vault and keys are read by `canopy agent prepare` itself
  # (from canopy-web), so the roster no longer fetches /runtime per agent here.
  local out="${slugs}"$'\n'
  _REGISTRY_CACHE="$out"
  printf '%s' "$out"
}

# ── Only the agents this box serves ────────────────────────────────────────────
# The registry is every agent the box's TOKEN can list, which for an owner's token
# is the whole fleet, other people's agents included. A box asked canopy-web for
# each one's vault every 30 minutes; a zero-knowledge agent refuses a box it does
# not route to, and each refusal emailed that agent's owner a device-proof alert
# (fizzy, 2026-10-17, canopy-web#1563). canopy-web answers which agents this box
# serves by the same rule it checks the proof against.
#
# ANSWERED (HTTP 200 with a JSON list) is final, an empty list included: a new box
# serves nothing, and reading [] as "unknown" sent cloud-ec2-fork down the whole
# registry, each resolve refused "does not run echo" (2026-10-18). Only UNKNOWN (no
# runner id yet, an older canopy-web, an error, a body that is not a list) falls
# back to the registry. A box that later gains an agent prepares it then
# (request_setup, or the runner's lazy clone on its first turn).
served_agents() {  # -> one slug per line; exit 0 = answered (even empty), 3 = unknown
  local base="${CANOPY_BASE_URL:-}" tok="${CANOPY_TOKEN:-}"
  local state="${STATE_FILE:-${HOME:-}/.canopy-cloud-runner.json}" rid body
  [[ -n "$base" && -n "$tok" && -r "$state" ]] || return 3
  rid="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("runner_id") or "")' \
         "$state" 2>/dev/null)" || rid=""
  [[ -n "$rid" ]] || return 3
  body="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
       "${base%/}/api/harness/runners/${rid}/agents" 2>/dev/null)" || return 3
  printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    raise SystemExit(3)
if not isinstance(d, list):
    raise SystemExit(3)
for s in d:
    if isinstance(s, str) and s:
        print(s)
' 2>/dev/null || return 3
}

resolve_roster() {  # -> comma-joined slugs; served first, registry, AGENT_SLUGS offline
  # What canopy-web says this box RUNS is the answer whenever it gives one: it
  # spans every workspace the box is enrolled in (marshellis as well as dimagi),
  # and it does not depend on the token being able to list the whole fleet. It
  # used to be only a filter over the registry, so an empty registry (a revoked
  # PAT, 2026-10-17) threw it away and fell back to AGENT_SLUGS.
  local reg served slugs
  if served="$(served_agents)"; then
    slugs="$(printf '%s' "$served" | grep -v '^$' | paste -sd, - 2>/dev/null || true)"
    [[ -n "$slugs" ]] || printf '[bootstrap-agents] this box serves no agents yet\n' >&2
    printf '%s\n' "$slugs"
    return 0
  fi
  reg="$(agent_registry)"
  slugs="$(printf '%s' "$reg" | cut -f1 | grep -v '^$' | paste -sd, - 2>/dev/null || true)"
  # An EMPTY registry is treated as "could not read", not as "provision nothing":
  # a canopy-web with zero agents is indistinguishable from an outage, and the
  # second reading would strand the box.
  printf '%s\n' "${slugs:-${AGENT_SLUGS:-}}"
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

  # The read-only labs telemetry profile (`aws ... --profile labs-observe`): its keys come
  # from canopy-aws-broker on this box (runner/ec2/aws_broker.py) via `canopy aws
  # credentials`, so a turn needs no SSO for metrics, alarms or ECS/ALB state. Idempotent;
  # a canopy CLI older than the command only warns.
  if command -v canopy >/dev/null 2>&1; then
    canopy aws profile --role labs-observe >/dev/null 2>&1 \
      && ok "aws profile labs-observe present" \
      || warn "could not add aws profile labs-observe (canopy CLI too old?)"
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
    # `gh release download` with no tag pulls the LATEST release. The repo is
    # public; `gh` still wants a login, and this box holds none any more (GitHub
    # is per turn), so this usually falls through to the curl path below, which
    # needs no token.
    if ! gh release download -R steipete/gogcli \
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

# ── Step 2: gog's keyring ───────────────────────────────────────────────────────
# The FILE backend: a headless box has no OS keychain, and GOG_KEYRING_PASSWORD (runner.env)
# unlocks it for bootstrap and for every turn. Which client each mailbox uses is no longer
# written here ahead of time: `canopy agent prepare` maps an account to the client its TOKEN
# names, once it has the token (the token is the fact; a pre-written map was a guess).
step2_gog_config() {
  log "step 2: gog keyring"
  if ! command -v gog >/dev/null 2>&1; then
    warn "gog not installed — skipping keyring setup"
    return
  fi
  gog auth keyring file >/dev/null 2>&1 && ok "gog keyring backend = file" \
    || warn "could not set gog keyring backend to 'file'"
  AGENT_SLUGS="$(resolve_roster)"
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

# Step 3: each agent prepared by `canopy agent prepare --runner`, which
# reads its credentials from canopy-web with this box's token (CANOPY_BASE_URL /
# CANOPY_TOKEN from runner.env), posts its own readiness report and exits non-zero on any
# problem. An agent counts as READY when its clone is on the box — the same bar as the bash
# path, where a warning (no mailbox, no token) never made an agent unready.
step3_agents() {
  log "step 3: per-agent prepare (canopy agent prepare --runner)"
  ensure_plugin_runtime
  mkdir -p "$AGENT_ROOT"
  local args=() slug
  (( CREDENTIALS_ONLY )) && args+=(--credentials-only)
  local slugs=(); IFS=',' read -ra slugs <<<"$AGENT_SLUGS"
  for slug in "${slugs[@]}"; do
    [[ -n "$slug" ]] || continue
    log "── agent $slug ──"
    if ! command -v canopy >/dev/null 2>&1; then
      fail "$slug: canopy CLI not on PATH — cannot prepare (step 1 installs it)"
      FAILED_AGENTS+=("$slug"); continue
    fi
    AGENT_ROOT="$AGENT_ROOT" canopy agent prepare --runner --slug "$slug" "${args[@]}" \
      || warn "$slug: canopy agent prepare reported problems (its readiness report says which)"
    if (( CREDENTIALS_ONLY )) || [[ -d "$AGENT_ROOT/$slug/.git" ]]; then
      READY_AGENTS+=("$slug")
    else
      FAILED_AGENTS+=("$slug")
    fi
  done
  # A box-wide pass is never given the agents' GitHub tokens (canopy-web hands
  # it only to a turn), so a private agent repo it cannot clone is NOT unprepared: its
  # first turn clones it with that turn's token (cloud_runner._ensure_agent_clone), and
  # only a failure THERE is reported (`agents_unprepared`, routing.cannot_serve).
  # Reporting it here would stop canopy-web from ever sending the turn that fixes it.
  if (( ! CREDENTIALS_ONLY )); then
    : >"$AGENT_ROOT/.unprepared" 2>/dev/null || true
  fi
}

# ── Step 4: claude plugins ───────────────────────────────────────────────────────
# Claude Code itself. cloud-init installs it ONCE (`npm i -g` as root, into
# /usr), and nothing updated it afterwards: a box's Claude Code was whatever was
# current the day the instance launched. Installed into $HOME/.local instead —
# the service user cannot write /usr, and $HOME/.local/bin is first on the
# runner unit's PATH, so this copy shadows the frozen one. Only on drift: an
# `npm view` is one registry call, an install is ~30s.
ensure_claude_current() {
  command -v npm >/dev/null 2>&1 || { warn "npm not on PATH — cannot update Claude Code"; return 0; }
  local latest have
  latest="$(npm view @anthropic-ai/claude-code version 2>/dev/null || true)"
  have="$(claude --version 2>/dev/null | awk '{print $1}')"
  if [[ -z "$latest" ]]; then
    warn "could not read the latest Claude Code version (registry unreachable?) — keeping ${have:-none}"
  elif [[ "$have" == "$latest" ]]; then
    ok "Claude Code $have is current"
  elif npm i -g --prefix "$HOME/.local" "@anthropic-ai/claude-code@$latest" >/dev/null 2>&1; then
    hash -r
    ok "Claude Code ${have:-none} -> $(claude --version 2>/dev/null | awk '{print $1}')"
  else
    warn "Claude Code update ${have:-none} -> $latest failed — keeping ${have:-none}"
  fi
}

step4_claude_plugins() {
  log "step 4: claude plugin marketplace + install"
  ensure_claude_current
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
  # Adding the marketplace once is not keeping it current. Nothing here used to
  # pull it: the clone advanced only when some session's async SessionStart hook
  # happened to update it, so a refresh re-ran bootstrap against whatever canopy
  # the box last saw, and the CLI sync below faithfully matched that stale clone
  # (hal's drill, 2026-09-22: plugin 0.2.509, origin 0.2.511). Every agent
  # plugin already updates by `git pull` in its own clone; this is canopy's.
  claude plugin marketplace update canopy >/dev/null 2>&1 \
    && ok "canopy marketplace updated" \
    || warn "claude plugin marketplace update canopy failed — continuing with the clone as it is"
  if claude plugin list 2>/dev/null | grep -q 'canopy@canopy'; then
    claude plugin update canopy@canopy >/dev/null 2>&1 \
      && ok "canopy@canopy updated to the marketplace's version" \
      || warn "claude plugin update canopy@canopy failed — keeping the installed version"
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
  # The guard is BOTH provenance AND version. It used to be provenance alone,
  # which made this a one-shot repair that could never update: once the receipt
  # recorded a directory it returned early forever, so the CLI froze at whatever
  # the clone happened to be on the day it was first re-pointed while the clone
  # itself kept advancing underneath it. A laptop hides this because a human runs
  # /canopy:update; nobody types that on a cloud box.
  #
  # Measured 2026-09-09: CLI 0.2.471 against a 0.2.479 clone, eight releases
  # behind, and `bin/hal-email`'s engine-staleness guard refused every send —
  # so the box could think but could not answer anyone. Found by a readiness
  # drill, not by the box reporting it.
  local clone_version installed_version
  clone_version="$(sed -nE 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' \
                   "$clone/pyproject.toml" 2>/dev/null | head -1)"
  installed_version="$(canopy --version 2>/dev/null \
                       | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  if grep -q 'directory = ' "$receipt" 2>/dev/null \
     && [[ -n "$clone_version" && "$installed_version" == "$clone_version" ]]; then
    ok "canopy CLI at $installed_version, matching the marketplace clone"
    return
  fi
  if [[ -n "$clone_version" && -n "$installed_version" && "$installed_version" != "$clone_version" ]]; then
    log "canopy CLI $installed_version lags the clone's $clone_version — reinstalling"
  fi
  # Keep what was installed beside canopy (the KB engine canopy-kb, readers): a bare
  # reinstall drops every `--with`, and `canopy kb build` then fails on this box
  # (2026-10-23, the GiveWell republish). uv's receipt lists them; cli_extras.py spells them.
  local extras_py with=() value
  # `:-$0` and `${with[@]+…}` keep this working under `set -u` on bash 3.2 (macOS),
  # where BASH_SOURCE is empty under `bash -c` and an empty array counts as unset.
  extras_py="$(dirname "${BASH_SOURCE[0]:-$0}")/../canopy_runner/canopy_runner/cli_extras.py"
  if [[ -f "$extras_py" ]]; then
    while IFS= read -r value; do
      [[ -n "$value" ]] && with+=(--with "$value")
    done < <(python3 "$extras_py" 2>/dev/null)
  fi
  if uv tool install --force --reinstall ${with[@]+"${with[@]}"} "$clone" >/dev/null 2>&1; then
    ok "canopy CLI re-pointed at the marketplace clone ($clone)${with:+, keeping ${#with[@]} --with args}"
  else
    warn "could not re-point the canopy CLI at $clone — doctor will flag its provenance"
  fi
}

# ── Step 4b: prune caches ────────────────────────────────────────────────────────
# AFTER step 4, so the plugin versions just installed are already recorded and
# kept. cloud-ec2-1's 20 GB root reached 100% on 2026-10-04 because nothing ever
# removed a plugin version (33 canopy, 24 ace, ~9 GB) or an npm/uv download.
# Full bootstrap only — never the --credentials-only timer pass — so it runs at a
# service start or the daily self-refresh, when no turn is running.
step4b_prune_caches() {
  log "step 4b: prune plugin / npm / uv caches"
  local before after
  before="$(df -P / 2>/dev/null | awk 'NR==2 {print $5}')"
  python3 "$SCRIPT_DIR/prune_caches.py" \
    || warn "cache prune failed — disk usage unchanged (see the runner's disk health check)"
  after="$(df -P / 2>/dev/null | awk 'NR==2 {print $5}')"
  ok "root volume ${before:-?} -> ${after:-?} used"
}

# ── Step 5: readiness summary ────────────────────────────────────────────────────
step5_summary() {
  log "step 5: readiness summary"
  if [[ -z "$AGENT_SLUGS" ]]; then
    # An answered, empty roster (served_agents): success, not "nothing came up".
    ok "this box serves no agents yet — nothing to prepare"
    return 0
  fi
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

# --credentials-only: the cheap, idempotent half. Everything here short-circuits
# when already satisfied, which is what makes it safe to run on a timer — and
# running it on a timer is the only path a CONFIG change has to this box, since
# nothing else ever restarts the service. See update_runner.sh.
#
# Deliberately NOT a full bootstrap: cloning repos, re-filling each agent's env and
# plugin installs are slow, and re-running them under a live agent is a way to break a
# box that was working (`canopy agent prepare --credentials-only` skips them).
# (A turn's `~/.<slug>/.env` does not need this: the runner refreshes it from the agent's
# vault when it claims that agent's turn — `canopy cred sync`, via `_refresh_agent_env`.)
CREDENTIALS_ONLY=0
for _arg in "$@"; do
  case "$_arg" in
    --credentials-only) CREDENTIALS_ONLY=1 ;;
  esac
done

main() {
  # The CLI RUNS step 3, so it is brought current first: an agent is prepared by the canopy
  # that ships with the plugin its turns use.
  if (( CREDENTIALS_ONLY )); then
    log "credentials-only pass (CLI sync, then gmail + readiness per agent)"
    step2_gog_config
    reinstall_cli_from_marketplace_clone
    step3_agents
    return 0
  fi
  step1_tooling
  step2_gog_config
  step4_claude_plugins
  step3_agents
  step4b_prune_caches
  step5_summary
}

main "$@"
