# Your canopy agents as one Claude Code plugin catalog

Code: `apps/plugins/` (`services.py` is the reference; its module docstring states the
rules below; `views.py` is the bare view). Tests: `tests/test_plugin_marketplace.py`.

Every signed-in person gets **one** catalog, the marketplace **`canopy-agents`**
(`services.MARKETPLACE_NAME`), at `GET /plugins/marketplace.json`
(`services.CATALOG_PATH`; wired in `config/urls.py`), listing the agent plugins of
every workspace they are a member of, at any role, across all their organisations
(`services.member_agents`). The install name never depends on which workspace an agent
lives in: `/plugin install ace@canopy-agents`. The **canopy** plugin itself is not
listed; it stays publicly installable from GitHub because it is what gives a new user
the token this catalog needs. The catalog is not at `/agents/marketplace.json` because
`/agents/*` is the SPA's redirect.

## Rules

1. **One catalog per person, not per workspace.** Membership decides what is *listed*
   (`wsvc.user_workspace_slugs`: direct membership at any role plus the descendants of
   a workspace you own). A person with no memberships gets a valid, empty catalog.
2. **canopy never lends GitHub access.** Every entry is a **`github` plugin source** on
   the agent's repo (`services.plugin_source`), which Claude Code fetches with the
   installing person's **own** git credentials, so installing needs their own read
   access to the repo; being listed is not being able to install. canopy builds no
   archives and holds no plugin bytes. Its one GitHub read is **anonymous**: it fetches
   `.claude-plugin/plugin.json` from `raw.githubusercontent.com` for the entry's name
   (`services.plugin_name`, cached 1 hour); a private repo falls back to the repo name,
   so keep the plugin name equal to the repo name.
3. **Plugins, not instances.** The catalog lists code; an `Agent` row is an instance.
   Entries are keyed by the plugin source `(repo, ref)` (`services.source_key`), so
   several instances of one repo give **one** entry (installing it twice would load the
   same skills and MCP servers twice), named from `plugin.json`, never from
   `Agent.slug`. Each entry's `metadata.instances` lists the `{agent, workspace}`
   instances the caller can see. Two different sources sharing a plugin name: the
   oldest instance's source keeps it, the others are listed as
   `<name>--<owner>-<repo>[--<ref>]` and logged (`services.plugins_for`); nothing is
   dropped. Which instance a local install binds to is chosen at setup by the agent's
   own `/<plugin>:setup`, not by the catalog; secrets live in the agent's Canopy vault,
   which the catalog never names. `GET /api/agents/` carries `plugin: {repo, name}` from
   the same function (`services.plugin_refs`), so the two cannot disagree.
4. **A plugin sits at its repo's root** (`.claude-plugin/plugin.json`): `Agent` has no
   subdirectory field.
5. `Agent.repo_ref` blank or `main` omits `ref` from the source, so Claude Code follows
   the default branch (`services._ref`).

## Auth and retired URLs

No canopy identity is a **401** with `WWW-Authenticate: Bearer`, never a sign-in
redirect, because Claude Code cannot follow one (`views._unauthorized`; the view is
self-enforcing, which is why `LoginRequiredMiddleware` admits the path). Send
`Authorization: Bearer <canopy token>`, the same PAT the canopy plugin's
`canopy-web-mcp-headers.js` sends to `/api/mcp/` (`BearerTokenAuthMiddleware` resolves
it); a delegated token also works; a contact token or a confined session's `cct_…`
token has no user and gets a 401. The header is needed for `marketplace.json` only;
plugins come from GitHub via the user's git (SSH key, or `gh auth login && gh auth
setup-git`). Users with no GitHub SSH key set `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1`:
installs and updates do not fall back to HTTPS on their own.

`GET /w/<ws>/marketplace.json` and `/w/<ws>/plugins/…` answer **410** naming the new
address, for anyone, signed in or not (`views.moved`); remove any `canopy-<ws>` entry.

### Registering it (what `/canopy:setup` writes)

In `~/.claude/settings.json` (user settings, so the helper runs without a prompt):

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

`/plugin marketplace add <url>` alone cannot attach a header, so it gets a 401; use the
settings entry. Then `/plugin install <plugin>@canopy-agents` and
`/plugin marketplace update canopy-agents` (or enable auto-update).

The helper must print exactly one JSON object, `{"Authorization": "Bearer <token>"}`,
exit 0 within 10 seconds, and be referenced by an absolute path (settings have no
`${CLAUDE_PLUGIN_ROOT}`; the working directory is `~/.claude`). An empty or missing
token is a 401: a marketplace has no OAuth fallback, so the person must mint a PAT first.

## How a merge reaches users

GitHub is the source of truth and canopy-web is out of the path. Claude Code computes an
installed plugin's version from `plugin.json`'s `version`, else the entry's `version`,
else the **commit sha**, and fetches a new copy only when that changes. Entries
deliberately carry no `version` (`tests/test_plugin_marketplace.py` pins it) and canopy
never rewrites one. So **an agent must bump `plugin.json`'s `version` on every merge**
it wants users to receive; one that merges without bumping leaves users on their cached
copy, and one that drops `version` is tracked by sha, so every merge is an update.
Background auto-update uses stored git credentials and never prompts; a credential
helper that would need to prompt makes the update fail quietly.
