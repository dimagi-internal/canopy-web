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

# gogcli's own account -> OAuth-client map: ace and echo keep dedicated clients;
# ada/eva/hal share the fleet's `canopy` app. See docs/architecture/shared-gog-gdrive.md.
#
# DO NOT "fix" this by reading each agent's config/agent.json `gog_client`. That was
# tried on 2026-09-05 (PR #661) and reverted the same day, because the inference is
# wrong: an OAuth token is minted FOR a specific client and only works with that
# client, so what a token can use is a property of the TOKEN, not of the agent's
# declared intent. All five agents declare `canopy`; measured on a fresh box:
#
#   echo under echo   -> LIVE          echo under canopy -> cannot authenticate
#   ada  under canopy -> LIVE
#   ace  under ace    -> cannot authenticate  (its op://Agent-Ace/gog-token is stale,
#                                              minted 2026-05-01 — broken either way)
#
# So pointing echo at `canopy` because its repo says so takes a WORKING mailbox and
# breaks it. This table must track where each account's token actually lives; the
# declaration in config/agent.json is the intent, and the two only converge once the
# 1Password item is re-minted under the shared client.
#
# The real invariant, if this is ever automated: pick the client whose token
# AUTHENTICATES (bootstrap already probes with `gog gmail search`), not the one
# some file names.
declare -A GOG_CLIENT=( [ace]=ace [ada]=canopy [echo]=echo [eva]=canopy [hal]=canopy )

# The client an agent's TURNS present, which is NOT necessarily the one whose
# token is live. `/ace:turn` and every sibling read `config/agent.json.gog_client`
# — the shared fleet app — and present THAT to gog. The map above tracks where
# each token actually lives, deliberately (see the note); these two are allowed
# to disagree, and when they do the agent's turns are dead while its mailbox
# verifies green.
#
# That is not hypothetical. On 2026-09-08 ACE reported `mailbox_ok: true,
# gog_client: canopy-web` for a full day while every inbound email turn aborted
# at preflight with `No auth for gmail ace@dimagi-ai.com` — the box had tokens
# under `ace` and `canopy-web`, and `/ace:turn` asked for `canopy`. Both halves
# were internally consistent. Nothing compared them.
FLEET_GOG_CLIENT="${FLEET_GOG_CLIENT:-canopy}"

# What this agent's turns will ask for. Prefer the agent's own declaration when
# the repo is on the box; fall back to the fleet client, which is what all five
# agents declare today.
turn_client_for() {  # <slug> -> client name
  local slug="$1" cfg="$AGENT_ROOT/$slug/config/agent.json" declared=""
  if [[ -r "$cfg" ]]; then
    declared="$(CFG="$cfg" python3 -c '
import json, os
try:
    print((json.load(open(os.environ["CFG"])).get("gog_client") or "").strip())
except Exception:
    pass
' 2>/dev/null || true)"
  fi
  printf '%s\n' "${declared:-$FLEET_GOG_CLIENT}"
}

# ── gog's own XDG resolution on Linux (mirrors canopy's agent_email.py
# _default_gog_config_dir — $GOG_HOME override, else $XDG_CONFIG_HOME/gogcli, else
# ~/.config/gogcli; there is no macOS branch on this box). ──────────────────────
# The shared vault's name when canopy-web serves none for this tenant. Was
# compiled into ensure_client_creds, which is correct for exactly one tenant;
# canopy-web now serves it per workspace and this is only the fallback.
# $CANOPY_SHARED_VAULT is the same override wire.sh and
# deploy/secrets/bootstrap_1password.sh take — one name across all three.
DEFAULT_SHARED_VAULT="${CANOPY_SHARED_VAULT:-Canopy-Shared}"

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

agent_registry() {  # -> "slug<TAB>repo_url" per agent, or nothing
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
          "${base%/}/api/agents/?limit=200" 2>/dev/null)" || return 0
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
  local out="" slug rt repo
  while IFS= read -r slug; do
    [[ -n "$slug" ]] || continue
    # The repo pointer lives on /runtime, not on the list payload.
    rt="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
          "${base%/}/api/agents/${slug}/runtime" 2>/dev/null)" || continue
    repo="$(printf '%s' "$rt" | python3 -c '
import json, sys
try:
    print((json.load(sys.stdin).get("repo_url") or "").strip())
except Exception:
    pass
' 2>/dev/null)" || repo=""
    out+="${slug}"$'\t'"${repo}"$'\n'
  done <<<"$slugs"
  _REGISTRY_CACHE="$out"
  printf '%s' "$out"
}

resolve_roster() {  # -> comma-joined slugs; registry first, AGENT_SLUGS offline
  local reg; reg="$(agent_registry)"
  local slugs
  slugs="$(printf '%s' "$reg" | cut -f1 | grep -v '^$' | paste -sd, - 2>/dev/null || true)"
  # An EMPTY registry is treated as "could not read", not as "provision nothing":
  # a canopy-web with zero agents is indistinguishable from an outage, and the
  # second reading would strand the box.
  printf '%s\n' "${slugs:-${AGENT_SLUGS:-}}"
}

agent_repo_url() {  # slug -> clone url; registry pointer, else the org convention
  local slug="$1" reg url
  reg="$(agent_registry)"
  url="$(printf '%s' "$reg" | awk -F'\t' -v s="$slug" '$1==s {print $2; exit}')"
  printf '%s\n' "${url:-https://github.com/${AGENT_REPO_ORG:-dimagi-internal}/${slug}}"
}

# THE authoritative account->client answer: the token names its own client.
#
# A gog token is JSON carrying {email, client, services, scopes, refresh_token},
# and `client` is the OAuth app it was minted for. A token works ONLY with that
# client, so nothing else can be authoritative — not this file's table, and not
# the agent's config/agent.json, which states INTENT. Making that declaration
# authoritative on 2026-09-05 pointed echo at `canopy`, whose client had no token
# for it, and broke a working mailbox (canopy-web#661, reverted in #662).
#
# The table below survives only as the FIRST-BOOT fallback: step 2 writes the map
# before step 3 has fetched any token, so there is a window with nothing to read.
# Never returns empty — an empty client points gog at the wrong app silently.
# Merge ONE account->client entry into gog's config.json, once the token has
# told us the truth. Step 2 writes the whole map before any token exists.
upsert_account_client() {
  local email="$1" client="$2"
  [[ -n "$email" && -n "$client" ]] || return 0
  command -v gog >/dev/null 2>&1 || return 0
  local dir; dir="$(gog_config_dir)"
  mkdir -p "$dir"
  UAC_CFG="$dir/config.json" UAC_EMAIL="$email" UAC_CLIENT="$client" python3 -c '
import json, os
path = os.environ["UAC_CFG"]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data.setdefault("account_clients", {})[os.environ["UAC_EMAIL"]] = os.environ["UAC_CLIENT"]
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
'
}

token_client() {  # <token-file> <slug>
  local tokfile="$1" slug="$2" declared=""
  if [[ -s "$tokfile" ]]; then
    declared="$(TOKF="$tokfile" python3 -c '
import json, os
try:
    print((json.load(open(os.environ["TOKF"])).get("client") or "").strip())
except Exception:
    pass
' 2>/dev/null || true)"
  fi
  printf '%s\n' "${declared:-${GOG_CLIENT[$slug]:-$slug}}"
}

# Materialize a gog OAuth client's id+secret to ~/.config/gogcli/credentials-<client>.json.
#
# gog refreshes a token by presenting the client_id+client_secret of the app the
# token was minted for, so this file is what makes a token usable rather than
# merely present. Which vault holds it depends on the client, not on the agent:
#
#   canopy      the shared fleet DESKTOP client (loopback redirect; ada/eva/hal)
#   canopy-web  canopy-web's WEB client — the only kind Google lets run a browser
#               redirect, and therefore the only one the "Connect Google mailbox"
#               button can mint with. Tokens from that button declare it.
#   <other>     an agent with its own client (echo) keeps it in its own vault.
#
# Never fails the bootstrap: a missing client file means gmail won't authorize,
# which the warning says, and every other part of the agent still provisions.
# What this pass actually achieved, per agent — the values POSTed by
# report_bootstrap below. Set as the facts are learned rather than inferred at
# the end: "the mailbox works" is only knowable by having made a call.
# Record one readiness fact. A function, not a bare `ARR[$slug]=v`, because an
# assignment to an UNDECLARED name makes bash treat it as an INDEXED array and
# evaluate the subscript arithmetically — so `CLIENT_CREDS_OK[ace]=1` dies with
# `ace: unbound variable` under `set -u` wherever the global is not in scope.
# That is not hypothetical: it made ensure_client_creds unusable in isolation and
# broke four tests the moment they ran on a bash that could execute them.
# `declare -gA` is idempotent and preserves an existing array's contents.
mark() {  # <array-name> <slug> <value>
  declare -gA "$1"
  printf -v "$1[$2]" '%s' "$3"
}

declare -A CLIENT_CREDS_OK=()
declare -A MAILBOX_OK=()
declare -A GOG_CLIENT_USED=()
declare -A TURN_CLIENT=()
# Tri-state: unset = not checked. NEVER default to 0 — a box that could not
# check must not report the agent as broken.
declare -A TURN_READY=()
declare -A BOOTSTRAP_DETAIL=()

# Tell canopy-web what this box could actually materialize for <slug>.
#
# The control plane otherwise knows only what it STORED, and on 2026-09-07 that
# was not the same thing for a whole day: a valid gog-token sat in canopy-web
# while every gmail call on this box failed, because the OAuth client id+secret
# it is useless without had not materialized. The credentials screen showed a
# green tick throughout. Nothing outside journald could see the difference.
#
# Best-effort by construction: a box that cannot reach the control plane must
# still finish bootstrapping. A missing report reads as "no box has said", which
# is honest — unlike a stale PASS, which is what the old silence amounted to.
report_bootstrap() {  # <slug>
  local slug="$1" base="${CANOPY_BASE_URL:-}" tok="${CANOPY_TOKEN:-}"
  # Idempotent, and preserves an existing array. Present so this function works
  # in isolation: READING ARR[$slug] on an undeclared name has the same
  # arithmetic-subscript hazard as writing it.
  declare -gA CLIENT_CREDS_OK MAILBOX_OK GOG_CLIENT_USED BOOTSTRAP_DETAIL TURN_CLIENT TURN_READY
  [[ -n "$base" && -n "$tok" ]] || return 0
  local rn="${RUNNER_NAME:-$(hostname)}"
  SLUG="$slug" RN="$rn" \
  CC="${CLIENT_CREDS_OK[$slug]:-0}" MB="${MAILBOX_OK[$slug]:-0}" \
  GC="${GOG_CLIENT_USED[$slug]:-}" DT="${BOOTSTRAP_DETAIL[$slug]:-}" \
  TC="${TURN_CLIENT[$slug]:-}" TR="${TURN_READY[$slug]-unset}" \
  python3 -c '
import json, os
print(json.dumps({
    "runner_name": os.environ["RN"],
    "client_creds_ok": os.environ["CC"] == "1",
    "mailbox_ok": os.environ["MB"] == "1",
    "gog_client": os.environ.get("GC", ""),
    "turn_client": os.environ.get("TC", ""),
    # Tri-state on the wire. "unset" -> null: the box did not check, which is
    # NOT the same as "checked and broken" and must not be reported as False.
    "turn_ready": (None if os.environ.get("TR") == "unset"
                   else os.environ.get("TR") == "1"),
    "detail": os.environ.get("DT", ""),
}))' > /tmp/.bootstrap-report.$$ 2>/dev/null || return 0
  curl -fsSL --max-time 20 -X POST \
    -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
    --data @/tmp/.bootstrap-report.$$ \
    "${base%/}/api/agents/${slug}/bootstrap-report" >/dev/null 2>&1 \
    && ok "$slug: readiness reported to canopy-web" \
    || warn "$slug: could not report readiness to canopy-web (bootstrap itself is unaffected)"
  rm -f /tmp/.bootstrap-report.$$
}

ensure_client_creds() {  # <client> <agent-vault> <slug> [shared-vault] [shared-token]
  local client="$1" agent_vault="$2" slug="$3"
  # Idempotent, and preserves an existing array. Present so this function works
  # in isolation: READING ARR[$slug] on an undeclared name has the same
  # arithmetic-subscript hazard as writing it.
  declare -gA CLIENT_CREDS_OK MAILBOX_OK GOG_CLIENT_USED BOOTSTRAP_DETAIL TURN_CLIENT TURN_READY
  # The tenant's shared vault and its OWN key, from canopy-web. Blank on a
  # deployment that has not configured one, which falls back to the historical
  # constant and today's (agent) key — i.e. exactly current behaviour.
  local shared_vault="${4:-}" shared_token="${5:-}"
  [[ -n "$shared_vault" ]] || shared_vault="${DEFAULT_SHARED_VAULT:-Canopy-Shared}"
  [[ -n "$client" ]] || return 0
  if ! command -v op >/dev/null 2>&1; then
    warn "$slug: op unavailable — cannot materialize gog client creds for $client"
    return 0
  fi

  # NOTE the vault split, and that it crosses the per-agent key boundary:
  # bootstrap_one_agent exports a SCOPED OP_SERVICE_ACCOUNT_TOKEN that canopy-web
  # issues per agent, and a per-agent key can read op://Agent-<Slug> and nothing
  # else. Both shared clients live in Canopy-Shared, so those two arms need a key
  # this function may not hold. Measured 2026-09-07 on cloud-ec2-1: inside ONE
  # bootstrap pass, seconds apart, op://Agent-Ace reads succeeded (op inject, the
  # gog-token read, credentials-ace.json) while op://Canopy-Shared/gog-oauth-client-web
  # failed — so ACE imported a browser-minted token bound to `canopy-web` and then
  # had no client id+secret to use it with. Every gmail call died on
  # `No auth for gmail ace@dimagi-ai.com` with a perfectly good token beside it.
  local client_vault client_item is_shared=0
  case "$client" in
    canopy)     client_vault="$shared_vault"; client_item="gog-oauth-client";     is_shared=1 ;;
    canopy-web) client_vault="$shared_vault"; client_item="gog-oauth-client-web"; is_shared=1 ;;
    *)          client_vault="$agent_vault";  client_item="gog-oauth-client" ;;
  esac

  local gog_dir; gog_dir="$(gog_config_dir)"
  local client_file="$gog_dir/credentials-${client}.json"
  # Already present counts as OK — this function is a materializer, and the
  # question the report answers is "does the box have it", not "did this pass
  # fetch it".
  if [[ -f "$client_file" ]]; then mark CLIENT_CREDS_OK "$slug" 1; return 0; fi

  mkdir -p "$gog_dir"
  # Capture op's stderr rather than discarding it — the same lesson the token
  # import below already learned, in the same file, twenty lines down. A bare
  # "op read ... failed" names the path that failed and nothing about WHY, so the
  # 2026-09-07 outage above was indistinguishable from a missing item, a revoked
  # key, a throttle, or an outage. Five diagnostic round trips to a cloud box
  # recovered one line op had already written and this function threw away.
  # A shared vault needs the SHARED key. The caller has already exported this
  # agent's per-agent key, and that key reads Agent-<Slug> and nothing else —
  # so using it here is the 2026-09-07 outage. Scoped to this one command
  # rather than exported, so nothing downstream inherits a broader credential.
  local -a openv=()
  if (( is_shared )) && [[ -n "$shared_token" ]]; then
    openv=(env "OP_SERVICE_ACCOUNT_TOKEN=$shared_token")
  fi

  # `${openv[@]+"${openv[@]}"}` and not `"${openv[@]}"`: expanding an EMPTY
  # array under `set -u` is an unbound-variable error on bash 3.2, which turns
  # the per-agent path — the common one — into a silent failed read. Caught by
  # the tests in this directory, which run on 3.2 deliberately.
  local readerr
  if readerr="$(${openv[@]+"${openv[@]}"} op read "op://${client_vault}/${client_item}/credential" 2>&1 >"$client_file")" \
     && [[ -s "$client_file" ]]; then
    chmod 0600 "$client_file"
    mark CLIENT_CREDS_OK "$slug" 1
    ok "$slug: gog client creds ($client) -> $client_file"
  else
    rm -f "$client_file"
    mark CLIENT_CREDS_OK "$slug" 0
    mark BOOTSTRAP_DETAIL "$slug" "op read op://${client_vault}/${client_item}/credential failed: ${readerr:-(no output)}"
    warn "$slug: op read op://${client_vault}/${client_item}/credential failed: ${readerr:-(no output)}"
    if (( is_shared )); then
      if [[ -n "$shared_token" ]]; then
        warn "$slug: $client is a SHARED client, read with this tenant's shared-vault key — check that key can read $client_vault"
      else
        warn "$slug: $client is a SHARED client and this tenant has NO shared-vault key, so the read used the per-agent key — which reads $agent_vault and nothing else. Set the workspace's shared_op_vault + token in canopy-web."
      fi
    fi
    warn "$slug: gmail will not authorize as $client until this resolves"
  fi
}

# A gog token can now arrive from TWO places, and the box must not silently
# prefer the stale one.
#
#   op://<vault>/gog-token/credential   the vault copy, minted at a terminal
#   canopy-web /credentials/resolve     minted in a browser ("Connect Google
#                                       mailbox"), which is the whole point of
#                                       provisioning an agent without 1Password
#
# canopy-web CANNOT write back to the vault — its service account is read-only,
# deliberately — so a browser mint lands in canopy-web only, and a box that reads
# the vault alone keeps the old token forever. Measured 2026-09-07: a token
# minted at 13:34Z was invisible to this box, which still held one from
# 2026-05-01 under a client whose mailbox had been dead for four months.
#
# NEWEST WINS, decided by the token's own `created_at`. Not "prefer canopy-web",
# which would make a fresh vault rotation lose to a stale browser mint; not
# "prefer the vault", which loses every browser mint. The token already declares
# its own client and its own age — the same reason `token_client` reads it rather
# than consulting a table (canopy-web#673).
token_created_at() {  # <file> -> epoch seconds, 0 when absent/unparseable
  local f="$1"
  [[ -s "$f" ]] || { printf '0\n'; return 0; }
  TOKF="$f" python3 -c '
import json, os, calendar, time
try:
    v = (json.load(open(os.environ["TOKF"])).get("created_at") or "").strip()
    print(int(calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ"))) if v else 0)
except Exception:
    print(0)
' 2>/dev/null || printf '0\n'
}

# Write canopy-web's stored gog-token for <slug> to <outfile>. Empty file when
# canopy-web has none, which is the normal case for an agent nobody has minted.
fetch_canopy_web_token() {  # <slug> <outfile>
  local slug="$1" out="$2" base="${CANOPY_BASE_URL:-}" tok="${CANOPY_TOKEN:-}"
  : >"$out"
  [[ -n "$base" && -n "$tok" ]] || return 0
  local body
  body="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
          "${base%/}/api/agents/${slug}/credentials/resolve" 2>/dev/null)" || return 0
  BODY="$body" OUT="$out" python3 -c '
import json, os
try:
    v = json.loads(os.environ["BODY"]).get("values", {}).get("gog-token")
except Exception:
    v = None
if v:
    open(os.environ["OUT"], "w").write(v)
' 2>/dev/null || true
}

vault_name() {  # ace -> Agent-Ace (bash 5, shipped on Ubuntu 24.04: ${var^} title-cases)
  local slug="$1"
  printf 'Agent-%s\n' "${slug^}"
}

# Ask canopy-web which vault this agent's secrets live in, and for a service
# token scoped to it. Prints "<vault>\t<token>"; either half may be empty.
#
# canopy-web is the CUSTODIAN of this pair, not the consumer: it stores the vault
# name and the key, and the RUNNER does the resolving (Jonathan, 2026-09-06 —
# "the service account and vault should be used on the runner"). That keeps
# canopy-web from becoming a second copy of every agent's credentials.
#
# Both halves ride /credentials/resolve because that route already carries the
# only plaintext gate in the system — bearer-only, caller must pair a live runner
# this agent routes to, every read audited. A second route would be a second gate
# to keep correct.
# Four fields, not two: this agent's own vault+key, and its TENANT's shared
# vault+key. The shared pair is separate because a per-agent key reads
# Agent-<Slug> and nothing else by design, so it cannot reach the shared gog
# OAuth clients — see ensure_client_creds. All four are blank-safe; a
# deployment that serves none behaves exactly as it did before this existed.
agent_vault_config() {  # <slug> -> "<vault>\t<token>\t<shared-vault>\t<shared-token>"
  local slug="$1" base="${CANOPY_BASE_URL:-}" tok="${CANOPY_TOKEN:-}"
  [[ -n "$base" && -n "$tok" ]] || { printf '\t\t\t\n'; return 0; }
  local body
  body="$(curl -fsSL --max-time 20 -H "Authorization: Bearer $tok" \
          "${base%/}/api/agents/${slug}/credentials/resolve" 2>/dev/null)" || { printf '\t\t\t\n'; return 0; }
  BODY="$body" python3 -c '
import json, os
try:
    d = json.loads(os.environ["BODY"])
except Exception:
    d = {}
print("%s\t%s\t%s\t%s" % (d.get("op_vault") or "", d.get("op_sa_token") or "",
                           d.get("shared_op_vault") or "", d.get("shared_op_sa_token") or ""))
' 2>/dev/null || printf '\t\t\t\n'
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
  # Registry-first (see agent_registry above); AGENT_SLUGS is the offline fallback.
  AGENT_SLUGS="$(resolve_roster)"
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

# ── an agent's DECLARED plugin dependencies ─────────────────────────────────────
# An agent's capabilities can come from a SIBLING plugin, and installing only its
# own leaves it passing every structural check while missing the tools it actually
# calls. Eva's Salesforce / Drive work is `mcp__plugin_chrome-sales_*`; her drill on
# the freshly rebuilt box (2026-08-12) came back green on identity, rails, hooks,
# gog auth and the board, and FAILED on `Required plugins` — chrome-sales was never
# installed, because nothing here installed it.
#
# Declared per-agent in config/agent.json `required_plugins`, the same contract
# `canopy agent doctor`'s check reads: a bare name, or an object with `marketplace`
# ("owner/repo"), optional `marketplace_name` (defaults to the plugin name) and a
# `note` naming any follow-up a human still has to do.
#
# Cloned, then added as a DIRECTORY source — the same shape as an agent's own
# plugin, and for the same two reasons: these repos are private, so this reuses the
# git credential store already staged for the agent clones rather than needing the
# marketplace to authenticate; and the clone IS the marketplace, so the `git pull`
# on a later run is also how the dependency updates. No second copy to keep in sync.
install_required_plugins() {
  local slug="$1" dest="$2"
  local cfg="$dest/config/agent.json"
  [[ -f "$cfg" ]] || return 0
  if ! command -v claude >/dev/null 2>&1; then
    warn "$slug: claude CLI not on PATH — required plugins not installed"
    return 0
  fi

  local specs
  specs="$(python3 "$SCRIPT_DIR/required_plugins.py" "$cfg" 2>/dev/null)" || return 0
  [[ -n "$specs" ]] || return 0

  local pname market mname note pdir
  while IFS=$'\t' read -r pname market mname note; do
    [[ -n "$pname" ]] || continue
    if claude plugin list 2>/dev/null | grep -q "${pname}@${mname}"; then
      ok "$slug: required plugin ${pname}@${mname} already installed"
      continue
    fi
    if [[ -z "$market" ]]; then
      warn "$slug: required plugin '$pname' declares no marketplace — cannot install it here"
      continue
    fi
    pdir="$PLUGIN_DEPS_ROOT/$pname"
    if ! mkdir -p "$PLUGIN_DEPS_ROOT" 2>/dev/null; then
      warn "$slug: cannot create $PLUGIN_DEPS_ROOT — required plugin '$pname' not installed"
      continue
    fi
    # Keep the REAL error. The first version swallowed stderr and guessed
    # "is the staged GitHub token valid?", which sent the diagnosis at a token that
    # was working fine while the actual fault (an unwritable /opt) went unmentioned.
    local clone_err
    if ! clone_err="$(clone_or_pull "https://github.com/${market}.git" "$pdir" 2>&1 >/dev/null)"; then
      warn "$slug: clone/pull of $market failed: ${clone_err:-(no output)}"
      continue
    fi
    if ! claude plugin marketplace list 2>/dev/null | grep -qE "(^|[[:space:]])${mname}\$"; then
      claude plugin marketplace add "$pdir" >/dev/null 2>&1 \
        || { warn "$slug: claude plugin marketplace add $pdir failed"; continue; }
    fi
    if claude plugin install "${pname}@${mname}" >/dev/null 2>&1; then
      ok "$slug: installed required plugin ${pname}@${mname}"
      [[ -n "$note" ]] && log "$slug: follow-up for ${pname}: ${note}"
    else
      warn "$slug: claude plugin install ${pname}@${mname} failed"
    fi
  done <<<"$specs"
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

# Bring the gmail token up to date for one agent. Extracted so the
# credentials-only pass runs EXACTLY this, rather than a second copy that can
# drift from it — the drift between two implementations of one rule is the
# original sin behind most of this file's history.
refresh_gmail_token() {  # <slug> <account> <client> <vault> <shared-vault> <shared-token>
  local slug="$1" account="$2" client="$3" vault="$4" shared_vault="$5" shared_token="$6"
  # Idempotent, and preserves an existing array. Present so this function works
  # in isolation: READING ARR[$slug] on an undeclared name has the same
  # arithmetic-subscript hazard as writing it.
  declare -gA CLIENT_CREDS_OK MAILBOX_OK GOG_CLIENT_USED BOOTSTRAP_DETAIL TURN_CLIENT TURN_READY
  if ! command -v gog >/dev/null 2>&1; then
    warn "$slug: gog unavailable — skipping gmail token import"
  elif gog gmail search --account "$account" --client "$client" in:inbox --max 1 >/dev/null 2>&1; then
    mark MAILBOX_OK "$slug" 1; mark GOG_CLIENT_USED "$slug" "$client"
    ok "$slug: gmail token already live (account=$account client=$client)"
  else
    log "$slug: gmail token not live — taking the NEWEST of the vault and canopy-web"
    local tokfile; tokfile="$(mktemp)"
    local vaultfile webfile; vaultfile="$(mktemp)"; webfile="$(mktemp)"
    op read "op://${vault}/gog-token/credential" >"$vaultfile" 2>/dev/null || : >"$vaultfile"
    fetch_canopy_web_token "$slug" "$webfile"

    # Both 0 (no python3, unparseable dates, or neither store has one) falls to
    # the vault copy — today's behaviour. Degrading toward the OLD path is the
    # right direction: it can leave a stale token in place, where degrading the
    # other way would import canopy-web's copy over a good vault rotation.
    local vage wage; vage="$(token_created_at "$vaultfile")"; wage="$(token_created_at "$webfile")"
    if (( wage > vage )); then
      cp "$webfile" "$tokfile"
      ok "$slug: using canopy-web's token (newer: $wage > $vage)"
    else
      cp "$vaultfile" "$tokfile"
      (( vage > 0 )) && log "$slug: using the vault's token (canopy-web has none newer)"
    fi
    rm -f "$vaultfile" "$webfile"

    if [[ -s "$tokfile" ]]; then
      # Capture stderr instead of discarding it. Swallowing it here is what hid a
      # fleet-wide failure for weeks: the `file` keyring backend wants a password
      # it can only PROMPT for, so on this TTY-less box EVERY import died with
      # "no TTY available ... set GOG_KEYRING_PASSWORD" and all anyone ever saw
      # was a bare "import failed".
      local importerr
      if importerr="$(gog auth tokens import "$tokfile" 2>&1 >/dev/null)"; then
        ok "$slug: gmail token imported"
        mark MAILBOX_OK "$slug" 0  # imported != usable; the call below decides
        # The token has just told us which client it belongs to. Step 2 could
        # only have written the fallback, so correct the map from the fact.
        local tclient; tclient="$(token_client "$tokfile" "$slug")"
        if [[ -n "$tclient" && "$tclient" != "$client" ]]; then
          warn "$slug: token declares client '$tclient', map said '$client' — using the token"
          # And it needs that client's id+secret on disk to refresh with. The
          # materialization above could only have used the fallback name, so a
          # token minted under a client the table doesn't know — every token from
          # canopy-web's browser mint — would import and then fail to refresh,
          # with the client file for a DIFFERENT app sitting right next to it.
          ensure_client_creds "$tclient" "$vault" "$slug" "$shared_vault" "$shared_token"
        fi
        upsert_account_client "$account" "$tclient"
        # Record WHICH client this token belongs to, for two consumers that both
        # got it wrong without it.
        #
        # verify_mailbox falls back to the GOG_CLIENT map, and that map is the
        # stale half of this whole story: it says `ace` while a browser-minted
        # token declares `canopy-web`. Presenting a refresh token to Google with
        # a DIFFERENT client's credentials returns `invalid_grant` — which reads
        # as "token expired or revoked" and sends the next person to re-mint a
        # token that was never the problem. Measured 2026-09-07 on cloud-ec2-1,
        # where the readiness report said exactly that with gog_client empty.
        #
        # And the report itself: `gog_client` is the single most diagnostic field
        # it carries, because which client is live is most of the diagnosis. It
        # was blank on the first real report this system ever produced.
        mark GOG_CLIENT_USED "$slug" "${tclient:-$client}"
      else
        warn "$slug: gog auth tokens import failed: ${importerr:-(no output)}"
        [[ -n "${GOG_KEYRING_PASSWORD:-}" ]] || \
          warn "$slug: GOG_KEYRING_PASSWORD is unset — stage it with ./secrets.sh gog"
      fi
    else
      warn "$slug: no gog token anywhere — neither op://${vault}/gog-token/credential nor canopy-web has one for $slug"
    fi
    shred -u "$tokfile" 2>/dev/null || rm -f "$tokfile"  # never leave the token on disk, even on failure
  fi
}

# Whether the mailbox actually works, decided by MAKING THE CALL.
#
# Everything upstream can succeed and still leave a mailbox that cannot
# authenticate: on 2026-09-07 a valid token imported cleanly and then had no
# OAuth client to use it with, and `gog gmail search` returned the same error as
# holding no token at all. Configuration that looks right is not the question,
# which is why this is the only thing the readiness report treats as the verdict.
verify_mailbox() {  # <slug> <account> <fallback-client>
  local slug="$1" account="$2" client="$3"
  # Idempotent, and preserves an existing array. Present so this function works
  # in isolation: READING ARR[$slug] on an undeclared name has the same
  # arithmetic-subscript hazard as writing it.
  declare -gA CLIENT_CREDS_OK MAILBOX_OK GOG_CLIENT_USED BOOTSTRAP_DETAIL TURN_CLIENT TURN_READY
  # The mailbox verdict is a CALL, never an inference. Everything above can
  # succeed and still leave a mailbox that cannot authenticate — that is exactly
  # what happened on 2026-09-07, when a valid token imported cleanly and then had
  # no OAuth client to use. Configuration that looks right is not the question.
  if command -v gog >/dev/null 2>&1 && [[ "${MAILBOX_OK[$slug]:-0}" != "1" ]]; then
    local vclient="${GOG_CLIENT_USED[$slug]:-$client}"
    local mberr
    if mberr="$(gog gmail search --account "$account" --client "$vclient" in:inbox --max 1 2>&1 >/dev/null)"; then
      mark MAILBOX_OK "$slug" 1; mark GOG_CLIENT_USED "$slug" "$vclient"
      ok "$slug: mailbox verified live (account=$account client=$vclient)"
    else
      mark MAILBOX_OK "$slug" 0
      mark BOOTSTRAP_DETAIL "$slug" "${BOOTSTRAP_DETAIL[$slug]:+${BOOTSTRAP_DETAIL[$slug]}; }gmail check failed: $(printf '%s' "$mberr" | head -1)"
      warn "$slug: mailbox NOT live (account=$account client=$vclient): $(printf '%s' "$mberr" | head -1)"
    fi
  fi
}

# Whether the mailbox works for the client the agent's TURNS present — the only
# question a turn's success actually depends on.
#
# `verify_mailbox` above answers "does SOME client work", and that is the right
# question for the mailbox. It is the WRONG question for readiness, and the two
# were conflated until 2026-09-08, when `mailbox_ok: true / gog_client:
# canopy-web` stood for a day next to an inbox no turn could open. The verifier
# had picked the client whose token authenticates; the consumer presents the one
# its config declares; nothing ever compared them.
#
# Cheap when they agree — the common case short-circuits without a second call.
verify_turn_client() {  # <slug> <account>
  local slug="$1" account="$2"
  declare -gA CLIENT_CREDS_OK MAILBOX_OK GOG_CLIENT_USED BOOTSTRAP_DETAIL TURN_CLIENT TURN_READY
  command -v gog >/dev/null 2>&1 || return 0   # unset stays unset: "not checked"
  local tclient; tclient="$(turn_client_for "$slug")"
  [[ -n "$tclient" ]] || return 0
  mark TURN_CLIENT "$slug" "$tclient"

  # Already proven under this exact client by verify_mailbox — no second call.
  if [[ "${MAILBOX_OK[$slug]:-0}" == "1" && "${GOG_CLIENT_USED[$slug]:-}" == "$tclient" ]]; then
    mark TURN_READY "$slug" 1
    ok "$slug: turns can read the mailbox (client=$tclient)"
    return 0
  fi

  local terr
  if terr="$(gog gmail search --account "$account" --client "$tclient" in:inbox --max 1 2>&1 >/dev/null)"; then
    mark TURN_READY "$slug" 1
    ok "$slug: turns can read the mailbox (client=$tclient)"
  else
    mark TURN_READY "$slug" 0
    # Loud, and it names BOTH clients: "the mailbox is fine" and "turns are dead"
    # are simultaneously true here, and a warning that omits either one reads as
    # a contradiction rather than a diagnosis.
    warn "$slug: TURNS CANNOT READ THE MAILBOX — account=$account needs a token under client '$tclient' (config/agent.json), but the live token is under '${GOG_CLIENT_USED[$slug]:-none}'. Mint one for '$tclient' and store it as this agent's gog-token: $(printf '%s' "$terr" | head -1)"
    mark BOOTSTRAP_DETAIL "$slug" "${BOOTSTRAP_DETAIL[$slug]:+${BOOTSTRAP_DETAIL[$slug]}; }turns need client '$tclient', live token is '${GOG_CLIENT_USED[$slug]:-none}'"
  fi
}

bootstrap_one_agent() {
  local slug="$1"
  local dest="$AGENT_ROOT/$slug"
  local client="${GOG_CLIENT[$slug]:-$slug}"
  local account="${slug}@dimagi-ai.com"
  # Vault + key from canopy-web when it has them; otherwise the derived name and
  # the runner-wide token, so an agent nobody has configured behaves exactly as
  # it did before this existed.
  local vault op_token shared_vault shared_token cfg
  cfg="$(agent_vault_config "$slug")"
  IFS=$'\t' read -r vault op_token shared_vault shared_token <<<"$cfg"
  if [[ -n "$vault" ]]; then
    ok "$slug: vault $vault (from canopy-web)"
  else
    vault="$(vault_name "$slug")"
  fi
  # Scoped key wins over the runner-wide one for THIS agent's reads only.
  local OP_SERVICE_ACCOUNT_TOKEN="${op_token:-${OP_SERVICE_ACCOUNT_TOKEN:-}}"
  export OP_SERVICE_ACCOUNT_TOKEN

  log "── agent $slug ──"

  # The expensive, disruptive half — skipped on a credentials-only pass. Cloning
  # and re-provisioning under a live agent is how a working box gets broken, and
  # none of it is needed to materialize a credential.
  if (( CREDENTIALS_ONLY )); then
    ensure_client_creds "$client" "$vault" "$slug" "$shared_vault" "$shared_token"
    refresh_gmail_token "$slug" "$account" "$client" "$vault" "$shared_vault" "$shared_token"
    verify_mailbox "$slug" "$account" "$client"
    verify_turn_client "$slug" "$account"
    report_bootstrap "$slug"
    READY_AGENTS+=("$slug")
    return
  fi

  local repo_url; repo_url="$(agent_repo_url "$slug")"
  if ! clone_or_pull "${repo_url%.git}.git" "$dest"; then
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
  install_required_plugins "$slug" "$dest"
  run_agent_provisioner "$slug" "$dest"

  # The gog OAuth-client credential FILE — see ensure_client_creds. Materialized
  # from the FALLBACK client name here, because the token that names the real one
  # has not been fetched yet; the call is repeated after the import below.
  ensure_client_creds "$client" "$vault" "$slug" "$shared_vault" "$shared_token"

  refresh_gmail_token "$slug" "$account" "$client" "$vault" "$shared_vault" "$shared_token"
  verify_mailbox "$slug" "$account" "$client"
  verify_turn_client "$slug" "$account"

  report_bootstrap "$slug"
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

# --credentials-only: the cheap, idempotent half. Everything here short-circuits
# when already satisfied, which is what makes it safe to run on a timer — and
# running it on a timer is the only path a CONFIG change has to this box, since
# nothing else ever restarts the service. See update_runner.sh.
#
# Deliberately NOT a full bootstrap: cloning repos, `op inject` and plugin
# installs are slow, and re-running them under a live agent is a way to break a
# box that was working.
CREDENTIALS_ONLY=0
for _arg in "$@"; do
  case "$_arg" in
    --credentials-only) CREDENTIALS_ONLY=1 ;;
  esac
done

main() {
  if (( CREDENTIALS_ONLY )); then
    log "credentials-only pass (client creds + gmail token + readiness report)"
    step2_gog_config
    step3_agents
    return 0
  fi
  step1_tooling
  step2_gog_config
  step3_agents
  step4_claude_plugins
  step5_summary
}

main "$@"
