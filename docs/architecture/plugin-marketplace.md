# Your canopy agents as one Claude Code plugin catalog

Code: `apps/plugins/` (services, views). Tests: `tests/test_plugin_marketplace.py`.
Replaces the per-workspace marketplaces of canopy-web#1376 (owner decision,
Jonathan Jackson, 2026-10-14).

Every signed-in person gets **one** catalog, the marketplace **`canopy-agents`**,
listing the agent plugins of every workspace they are a member of, at any role,
across all their organisations. The install name never depends on which
workspace an agent lives in: `/plugin install ace@canopy-agents`, whether ACE is
in `connect` or anywhere else, and it keeps working if the agent moves.

The **canopy** plugin itself is not listed here. It stays publicly installable
from GitHub, because it is what gives a new user the token this catalog needs.

## Two rules

1. **One catalog per person, not per workspace.** Membership decides what is
   *listed*: direct membership of a workspace at any role, plus the descendants
   of a workspace you own (the only access the workspace tree grants). A person
   with no memberships gets a valid, empty catalog.
2. **canopy never lends GitHub access.** Every entry is a **`github` plugin
   source** on the agent's repo, which Claude Code fetches with the installing
   person's **own** git credentials. To install an agent you need your own read
   access to its GitHub repo (ada, eva, echo, hal and ace are private
   `dimagi-internal` repos). canopy builds no archives and holds no plugin
   bytes. The one GitHub read canopy makes is **anonymous**: it fetches
   `.claude-plugin/plugin.json` from `raw.githubusercontent.com` for the entry's
   name (cached 1 hour). That works for a public repo; a private one falls back
   to the repo name.

## Plugins, not instances

The catalog lists **code**. An `Agent` row is an *instance*: an agent running in
one workspace, with its own vault and config. Two workspaces could each run an
instance of the same agent (two rows, different slugs, the same repo + ref).
They are **one plugin**; installing it twice would load the same skills and MCP
servers twice under one `plugin.json` name. So:

- entries are keyed by the plugin source, `(repo, ref)`; several instances of
  one repo give **one** entry, and its `metadata.instances` lists the
  `{agent, workspace}` pairs you can see;
- the entry is named from the plugin's own `plugin.json` `name`, else the repo
  name, never from `Agent.slug`, so the install name is `ace@canopy-agents`
  whichever instance you can see;
- two *different* sources that share a plugin name (a fork, or another ref):
  the source of the oldest instance keeps the name, and each other one is
  listed as `<name>--<owner>-<repo>[--<ref>]` and logged. Nothing is dropped.

Choosing **which** instance you work with (workspace, 1Password vault, config)
is the agent's own setup command's job, not the catalog's. Today every agent has
exactly one instance.

`Agent` has no plugin-subdirectory field, so a plugin must sit at its repo's
root (`.claude-plugin/plugin.json`); every fleet agent's does. A plugin nested
in a subdirectory would need a stored path and a `git-subdir` source.

## URLs

| URL | What it returns |
| --- | --- |
| `GET https://canopy.dimagi.com/plugins/marketplace.json` | Your catalog: `name` = `canopy-agents`, one `github` entry per plugin |
| `GET /w/<ws>/marketplace.json`, `GET /w/<ws>/plugins/…` | **410 Gone** (retired), JSON `detail`: "Agent plugins moved to one catalog: register https://canopy.dimagi.com/plugins/marketplace.json (re-run /canopy:setup).", plus `url` and `marketplace` |

The catalog is not at `/agents/marketplace.json` because `/agents/*` is the
SPA's redirect to `/w/<ws>/agents` (and allowlisted for the PWA shell).

Auth: no canopy identity is a **401** with `WWW-Authenticate: Bearer`, never a
sign-in redirect, because Claude Code can't follow one. The 410s answer anyone,
signed in or not, and reveal nothing.

## The auth header contract

Send **`Authorization: Bearer <canopy token>`**: the same header and token (a
PAT from `/canopy:canopy-web-pat-mint`, at `~/.claude/canopy/workbench-token`)
that the canopy plugin's `scripts/canopy-web-mcp-headers.js` sends to
`/api/mcp/`. `BearerTokenAuthMiddleware` resolves it, as for every other route.
A delegated token also works (it resolves to a user). A contact token, or a
confined session's `cct_…` caller token, has no user and gets a 401.

The header is needed for `marketplace.json` only. The plugins themselves come
from GitHub, authenticated by the user's git (SSH key in `ssh-agent`, or a
stored HTTPS credential, e.g. `gh auth login && gh auth setup-git`). Users with
no GitHub SSH key should set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`: installs from
`/plugin` and plugin updates don't fall back to HTTPS on their own.

### Registering it (what `/canopy:setup` writes)

In `~/.claude/settings.json` (user settings, so the helper runs without a
prompt):

```json
{
  "extraKnownMarketplaces": {
    "canopy-agents": {
      "source": {
        "source": "url",
        "url": "https://canopy.dimagi.com/plugins/marketplace.json",
        "headersHelper": "node /ABSOLUTE/PATH/canopy-web-mcp-headers.js"
      }
    }
  }
}
```

Then, in a session:

```
/plugin install <plugin>@canopy-agents     e.g. /plugin install echo@canopy-agents
/plugin marketplace update canopy-agents   pick up new agents and updates (or enable auto-update)
```

`/plugin marketplace add https://canopy.dimagi.com/plugins/marketplace.json`
alone can't attach a header, so it gets a 401. Use the settings entry. Old
`canopy-<ws>` entries should be removed; their URL now answers 410.

### What the helper must do

- **Print** exactly one JSON object on stdout, `{"Authorization": "Bearer <token>"}`,
  and exit 0 within 10 seconds.
- **Use an absolute path.** Settings have no `${CLAUDE_PLUGIN_ROOT}` and the
  helper's working directory is `~/.claude`; the canopy setup installs the
  helper at a stable path and writes that path into the entry.
- **An empty or missing token is a 401.** A marketplace has no OAuth fallback,
  so the person must mint a PAT first.

## Sample `marketplace.json`

For someone who is a member of `dimagi` and `connect`:

```json
{
  "name": "canopy-agents",
  "owner": { "name": "canopy", "url": "https://canopy.dimagi.com/" },
  "description": "The canopy agents in your workspaces, as Claude Code plugins. Installing one needs your own read access to its GitHub repo.",
  "plugins": [
    {
      "name": "ace",
      "description": "ACE runs Connect opportunities end to end",
      "source": { "source": "github", "repo": "dimagi-internal/ace" },
      "metadata": { "instances": [{ "agent": "ace", "workspace": "connect" }] }
    },
    {
      "name": "ada",
      "description": "Conducts the canopy agent fleet",
      "source": { "source": "github", "repo": "dimagi-internal/ada" },
      "metadata": { "instances": [{ "agent": "ada", "workspace": "dimagi" }] }
    }
  ]
}
```

An agent whose `Agent.repo_ref` is set and isn't `main` gets `"ref": "<ref>"` in
its source. `main` and blank omit `ref`, so Claude Code follows the repo's
default branch.

## How a merge reaches users

GitHub is the source of truth and canopy-web is out of the path: Claude Code
clones the repo at the ref itself. It computes an installed plugin's version
from `plugin.json`'s `version`, else the entry's `version`, else the **commit
sha**, and fetches a new copy only when that computed version changes. Entries
deliberately carry no `version`.

So **an agent must bump `plugin.json`'s `version` on every merge** it wants users
to receive (the fleet agents' auto-bump workflows do this). An agent that merges
without bumping leaves users on their cached copy until it next bumps. An agent
that drops `version` from `plugin.json` is tracked by commit sha instead, and
every merge is an update. (The retired archive path rewrote the version to
`<version>+<sha>` to hide this; with `github` sources canopy no longer touches
the plugin.)

Users receive updates with `/plugin marketplace update canopy-agents`, or by
turning on auto-update for the marketplace under `/plugin` → Marketplaces.
Background auto-update uses the user's stored git credentials and never
prompts; a credential helper that would need to prompt makes the update fail
quietly, leaving the installed copy in place.

## Known limits

- Plugins in a repo subdirectory are not supported (no stored path; see above).
- A private repo's entry is named after the repo, because canopy reads
  `plugin.json` anonymously. Keep the plugin name equal to the repo name.
- Being listed is not being able to install: someone listed an agent whose repo
  they can't read gets a git error at install. That is the intended boundary.
