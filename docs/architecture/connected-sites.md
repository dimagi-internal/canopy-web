# Connected sites

How an agent is put on another website (Connect Labs, ace-web, …) and what a conversation held there
may do. This is the one page that walks the whole path; the rule itself is
[access.md](access.md) (§ The Turn Context, § Read mode and connected sites), the host's guide is
[embedding-a-canopy-agent.md](embedding-a-canopy-agent.md), and the gateway's wire contract is
[host-grant-contract.md](host-grant-contract.md).

## Two records, set by two people

A site is "connected" for an agent only when **both** of these exist. They are separate rows, set on
separate pages, and neither creates the other.

```
 WORKSPACE OWNER                                   AGENT OWNER / AGENT ADMIN
 Settings › Connected sites                        Agent settings › Channels › Read mode and connected sites
 ─────────────────────────────                     ─────────────────────────────────────────────────────
 AppCredential (workspace, name)                   one entry in Agent.interface["capabilities"]
   signing keys / JWKS, frame origins,               { sites: [<AppCredential.name>],
   host_issuer + host_mcp_resource                     description, entry, tools, bash,
 AppCredentialAgent (app, agent)                       read_paths, write_paths,
   "Agents it may offer"                               ceiling, own_login, data: sensitive }
```

| Record | What it says | Who sets it | Code |
|---|---|---|---|
| **The site** (`AppCredential`) | canopy knows this host: it may vouch for its visitors with a signed assertion and frame the widget; with `host_issuer` + `host_mcp_resource` it also issues act-as-you permissions (`issues_host_grants`) | workspace owner | `apps/tokens/models.py`, `apps/embedding/embed_apps.py` (`register`, `update`, `revoke`) |
| **Agents it may offer** (`AppCredentialAgent`) | the site's picker may show this agent | workspace owner, on the site's card | `embed_apps.set_agents` |
| **The agent's connected site** (one entry of `Agent.interface`) | how this agent behaves in a conversation held on that site: the confined profile a visitor gets, and the site's narrowing and login rules for everyone there | agent owner or agent admin | `apps/agents/answering.py` (`set_site`, `remove_site`), routes `…/agents/{slug}/connected-sites/{site}` |

The host's readiness check (`GET /api/sites/agents`, below) needs both: an offered agent with no entry for
the site is `ready: false` with the problem "is not connected to <site>".

### Names

| Word | Means | Example |
|---|---|---|
| site name | `AppCredential.name`, unique per workspace; what `sites:` lists and what a session's `embed_app` holds | `connect-labs` |
| entry key | the agent's own key for its connected-site entry (`answering.key_for`, never renamed). It is what a turn is stamped with (`Turn.capability`), what an access line names (`use:<key>`, `AgentGrant.capability`) and what the envelope's `capability` / `granted_by: capability:<key>` say | `connect` |
| connected app | **not this.** The same page's API and MCP tools still say "connected app" (`connect_app`, `update_connected_app`, `/settings/connected-apps`); `tokens_list_connected_apps` is OAuth MCP clients a person authorised, and `list_connection_apps` is OAuth provider clients (`apps/connections`) | — |

The entry key and the site name differ. An access line opens an entry by **key**, per agent.

## Setting one up

1. **Workspace owner:** register the site (Settings › Connected sites): its name, signing key or JWKS URL,
   frame origins; for act-as-you, its `host_issuer` and MCP resource URL. Tick the agents it may offer.
2. **Agent owner or admin:** add the site under the agent's Read mode and connected sites. A new entry
   starts from `answering.new_site_defaults`; set its tools and paths, and whether it handles sensitive data.
3. **Access list:** who gets what on the site is the agent's access list, never the entry. Someone holding
   `read` reaches the entry on that site; a `use:<key>` line opens exactly that entry; `write`, owner and
   admin reach the whole agent (narrowed on the site's pages, below).
4. **Runner:** some runner must take the agent's turns, with the `sensitive_data` flag if the entry handles
   sensitive data.
5. **Host:** checks `GET /api/sites/agents` (`canopy_sdk.site_agents.fetch_site_agents`); `ready` and
   `problems` / `problem_links` say what is missing and where to fix it.

## How a conversation gets its site

A session created through a site's delegated token is stamped server-side with that site's name
(`canopy_sessions.services.stamp_host_metadata` → `metadata["embed_app"]`). Nothing the page or the caller
sends can set or change it. The Session Context reports it as `site` (`interface.session_site`), and its
`page` (`page_state.resource`) is a fact about the conversation that selects nothing.

## What a turn there may do

The Turn Context resolver (`turn_context.resolve_turn_context`) decides, in the order access.md § How it is
resolved gives. For a conversation on a site:

| Asker | Profile | Access |
|---|---|---|
| owner, agent admin, `system`, a session writer, or holds `write` | `whole_agent`, but every call to the site goes through the person's own act-as-you permission, and the agent's own login there is withheld (below) | `full` |
| named an entry (`requested`) their permissions open | that entry | `confined` |
| named an entry they cannot open | none, **no fall-through** | `refused` (`profile_not_open`) |
| named nothing; the site's entry opens for them | that `connected_site` entry | `confined` |
| named nothing; holds `read` but not the entry | `read_mode` (the entry is skipped, not fatal) | `confined` |
| otherwise | none | `refused` |

Then the narrowing rules: a review chat is `session_review` for anyone not refused, and an entry with
**Handles sensitive data** confines **everyone** in a conversation on that site to that entry, the owner
included; anything else in a sensitive conversation is refused (`sensitive_not_open`).

## What an entry does beyond picking a profile

An entry for a site changes more than what a visitor may do:

* **Gateway tools.** Its profile gains canopy's site tools (`site_tools`, `site_call`, `site_read`,
  `interface.GATEWAY_TOOLS`) and the page's own actions (`page_*`).
* **Everyone reaches the site as themselves.** On a site the agent has an entry for, every turn held on
  that site's page, the owner's included, reaches the site only under the sender's act-as-you permission
  (`HostGrant`, confirmed with a passkey, 30 minutes), never with the agent's own credentials
  (`interface.page_site_of`, `page_turn`; the runner withholds the entry's `own_login` names).
* **Ceiling.** `ceiling` optionally narrows the host's tools on the owner's word, for every turn on that
  site (`interface.site_ceiling`). Absent, the host's grant decides.
* **Contacts.** A contact (no passkey, so no act-as-you permission) is not offered an agent that has an
  entry for the site (`contacts/contact_api._for_contacts`).
* **Sensitive.** See the table above; the site's runner requirements also pin the conversation to a
  `sensitive_data` runner.

## The gateway

`site_tools` / `site_call` / `site_read` (`apps/mcp/tools/site.py`) do what `apps/host_grants/host_gateway.py`
says: resolve the turn's site from the conversation (never an argument), refuse unless the site is live and
issues host grants, the turn may use that site (a confined turn's stamped entry names it; a full turn is on
a site the agent has an entry for), and the sender holds a confirmed, unexpired grant for it; then call the
host's MCP server with DPoP, as the sender. There is no fallback to the agent's own credential.

## What the host sees

`GET /api/sites/agents` (signed request, signed answer; [api-surface.md](api-surface.md)) lists the
agents the site may offer, each with its entries for the site (`capabilities[]`: `name`, `description`,
`handles_sensitive`, `sensitive_set_by` / `_at`), its runners, `ready` and `problems`.
`sensitive_set_by` / `_at` are the last save of **any** of the agent's read mode or connected sites, not
of that flag. Sign-offs (`apps/host_grants/sign_offs*`) are a separate site-to-person approval flow and
touch neither record.

## Names that stay

Stored or on the wire, so not renamed (access.md § Read mode and connected sites): the `Agent.interface`
column and its `capabilities` key, the entry key, `Turn.capability`, `AgentGrant.capability`,
`use:<key>`, `data: sensitive`, `metadata["embed_app"]`, the envelope's `capability` and
`granted_by: capability:<key>`, and the site-agents API's `capabilities` / `AgentCapability`
(`canopy_sdk.site_agents`, read by connect-labs).

## Code map

| Question | Where |
|---|---|
| Register / change / revoke a site, which agents it offers | `apps/embedding/embed_apps.py` |
| The agent's entries: read, add, change, remove | `apps/agents/answering.py` |
| Parse and validate an entry | `apps/agents/interface.py::parse` |
| Which site is this conversation on | `interface.session_site` (`embed_app`) |
| Which entry does a site select for this asker | `turn_context._entry_for` → `interface.site_entry` |
| Does this turn run on a site's page; what is withheld | `interface.page_site_of`, `page_turn`, `own_login_of` |
| The ceiling | `interface.site_ceiling` |
| Sensitive | `apps/agents/sensitive_data.py`, `turn_context._sensitive_rule` |
| The gateway | `apps/host_grants/host_gateway.py`, `apps/mcp/tools/site.py` |
| What the host is told | `apps/host_grants/site_agents.py` |
