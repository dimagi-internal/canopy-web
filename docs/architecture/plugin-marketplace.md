# A workspace's agents as a Claude Code plugin marketplace

canopy-web#1376. Code: `apps/plugins/` (services, views, model). Tests:
`tests/test_plugin_marketplace.py`.

Every workspace serves its agents' plugins as a Claude Code marketplace, so a
member can install them **without a GitHub account**. Agent repos are private
`dimagi-internal` repos, and a `github` plugin source is fetched with the user's
own `git`, so before this only people with GitHub access could install an agent.
An `archive` source is a zip over HTTPS, pinned by `sha256`, fetched with the
caller's canopy token.

The **canopy** plugin itself is not served here. It stays publicly installable
from GitHub, because it is what gives a new user the token this marketplace
needs.

## URLs

| URL | What it returns |
| --- | --- |
| `GET https://canopy.dimagi.com/w/<ws>/marketplace.json` | The marketplace: `name` = `canopy-<ws>`, one `archive` entry per agent in `<ws>` that has a github.com repo |
| `GET https://canopy.dimagi.com/w/<ws>/plugins/<agent-slug>/<short-sha>.zip` | One agent's plugin at one commit. The bytes never change for a URL (`Cache-Control: immutable`) |

Both URLs need a canopy identity and membership of `<ws>`, at any role, viewer
included:

- no identity: **401** with `WWW-Authenticate: Bearer`. This is never a sign-in
  redirect, because Claude Code can't follow one.
- not a member, or no such workspace: **404**. The two cases look the same, so
  the response doesn't reveal whether a workspace exists.
- an agent that isn't in `<ws>`, or a version that was never built: **404**.

## The auth header contract

Send **`Authorization: Bearer <canopy token>`**. This is the same header and token
(a PAT from `/canopy:canopy-web-pat-mint`, at `~/.claude/canopy/workbench-token`)
that the canopy plugin's `scripts/canopy-web-mcp-headers.js` already sends to
`/api/mcp/`. `BearerTokenAuthMiddleware` resolves it, as it does for every other
route. A delegated token also works, because it resolves to a user. A contact
token does not, because it has no user, so it gets a 401.

Claude Code attaches headers to a hosted marketplace in only one way: a
`headersHelper` (or a static `headers` map) on a **`url` marketplace source
declared in settings**. A helper declared there runs before every fetch of
`marketplace.json` **and** before every archive download on the same origin.
Claude Code reuses its output for up to 60 seconds. Every archive URL is on
canopy's own origin (`CANOPY_PUBLIC_BASE_URL`), so one helper covers both. The
entries deliberately set no per-entry `headersHelper`. Claude Code would ask the
user to approve one on each install, and it would require `"strict": false`.

### Registering it (what a user, or the canopy plugin's setup, writes)

In `~/.claude/settings.json` (user settings, so the helper runs without a
prompt):

```json
{
  "extraKnownMarketplaces": {
    "canopy-<ws>": {
      "source": {
        "source": "url",
        "url": "https://canopy.dimagi.com/w/<ws>/marketplace.json",
        "headersHelper": "node /ABSOLUTE/PATH/canopy-web-mcp-headers.js"
      }
    }
  }
}
```

Then, in a session:

```
/plugin install <agent>@canopy-<ws>        e.g. /plugin install echo@canopy-family
/plugin marketplace update canopy-<ws>     pick up merges (or turn on auto-update under /plugin → Marketplaces)
```

`/plugin marketplace add https://canopy.dimagi.com/w/<ws>/marketplace.json` on
its own has no way to attach a header, so it gets a 401. Use the settings entry
above, which registers the marketplace with no `add` step.

### What the helper must do (the canopy-side contract)

Wiring this into the canopy plugin is canopy's job, not canopy-web's. These are
the constraints it has to meet:

- **Print** exactly one JSON object on stdout, `{"Authorization": "Bearer <token>"}`,
  and exit 0 within 10 seconds. `canopy-web-mcp-headers.js` already does this.
- **Use an absolute path.** Settings have no `${CLAUDE_PLUGIN_ROOT}`, and the
  helper's working directory is `~/.claude`. The plugin cache path changes with
  each canopy version, so the canopy setup should install the helper at a
  stable path, or expose it as a stable CLI such as
  `canopy marketplace-headers`, and write that path into the settings entry.
- **An empty or missing token is a 401 here.** On `/api/mcp/`, emitting no
  header is a working state, because the 401 leads to the MCP OAuth sign-in.
  A marketplace has no OAuth fallback, so the person must mint a PAT first.
- **Confined sessions** (`cct_…` caller tokens) resolve to no user and get a
  401, which is correct: a caller's session should not install the owner's
  agents.
- Credential-looking environment variables are stripped only for helpers
  declared in a `marketplace.json` entry or in project settings. A helper in
  user settings keeps its environment. Read the token from the file anyway,
  as the existing helper does.

## Sample `marketplace.json`

```json
{
  "name": "canopy-family",
  "owner": { "name": "Family", "url": "https://canopy.dimagi.com/w/family/agents" },
  "description": "The agents of the Family workspace on canopy, as Claude Code plugins.",
  "plugins": [
    {
      "name": "echo",
      "description": "Echo tells stories",
      "version": "1.2.0+aaaaaaaaaaaa",
      "source": {
        "source": "archive",
        "url": "https://canopy.dimagi.com/w/family/plugins/echo/aaaaaaaaaaaa.zip",
        "sha256": "6bfa50e3d2e00c052b46abe51fff89346ac803e45771f76dcf6df1ab74cca5e1"
      },
      "metadata": {
        "agent": "echo",
        "repo": "dimagi-internal/echo",
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  ]
}
```

## How a merge reaches users

GitHub stays the source of truth. Agents keep shipping branch → PR → merge, and
canopy-web never edits a plugin.

1. A marketplace read resolves each agent's `repo_url` @ `repo_ref` (default
   `main`) to a commit sha. It caches the result for **5 minutes**
   (`HEAD_TTL`), so a merge is visible to users within that window.
2. If no archive exists for that commit, canopy builds one. It downloads
   GitHub's zipball **as the agent's owner**: the owner's GitHub delegation
   (Settings → Credentials → GitHub), else the owner's GitHub App connection,
   else anonymously, which works for a public repo. The person reading the
   marketplace never lends a GitHub identity.
3. The build repackages the zipball so the **plugin root is the zip root**. It
   finds the root at `.claude-plugin/plugin.json`, or, in a marketplace repo, at
   the single local-source plugin, or the one named after the agent. Every file
   gets the same fixed timestamp. The build rewrites `plugin.json`'s `version` to
   **`<repo version>+<12-char sha>`**. Claude Code updates an installed plugin
   only when its version changes, and `plugin.json` wins over the marketplace
   entry. Without this rewrite, a merge that didn't bump the version would leave
   every user on their old copy.
4. The bytes are stored in `plugins_pluginarchive`, one immutable row per
   (repo, commit), and the entry pins their `sha256`. GitHub doesn't promise a
   byte-stable zipball, so the stored row is what keeps the pin true.

Claude Code allows a `marketplace.json` fetch **10 seconds**. A read builds new
archives for up to 6 seconds (`BUILD_BUDGET_SECONDS`). After that, an agent
whose new head isn't built yet is listed at its **last built version**, and the
next read builds it. When GitHub fails, the last built version is listed too.
An agent that has never built successfully is left out. To see why an agent is
missing, run `manage.py build_plugin_archives [--workspace <slug>]`: it builds
every head now and prints each failure's reason. It is also a way to pre-warm
after a burst of merges.

## Known limits

- Two agents in one workspace whose repos declare the same plugin name: the
  first agent by slug is listed, and the second is not.
- The marketplace follows `Agent.repo_ref`. An agent pinned to a branch serves
  that branch.
- Old archive rows are never deleted. Each one is a few MB, and an installed
  user's URL keeps working.
