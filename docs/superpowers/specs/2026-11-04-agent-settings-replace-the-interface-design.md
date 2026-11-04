# Agent settings replace the interface: read mode, connected sites, sensitive data

**Status:** design for review. Nothing here is built. Phase 2 of
`2026-11-03-agent-read-mode-and-doors-design.md`, which left it as direction only ("site connections
become an agent setting, sensitive data becomes an agent-level setting, the word interface leaves the
product"). This spec makes that concrete. It changes no behaviour a person, a host site, the SDK or a
runner can observe on day one; it changes where the settings are stored, what they are called, and how
many concepts an admin has to hold.

Written from the code at `dbd6db32` and from all 18 live agents' interfaces and access lists, read
through the canopy API on 2026-11-04 (section 4). Phase 1 (read mode) is merged but, per its own
spec, not yet deployed; this work should start only after that deploy is verified (section 8, risk R1).

## 1. In plain words (for anyone, not only engineers)

Today every agent has an "interface": a block of YAML nobody on the team was comfortable editing. It
was written to answer "who may ask this agent for what", but that job moved to the **access list**
(read, write, agent_edit, admin, own). What the YAML still holds is three unrelated things, and an admin
has to understand "capability", "door", "ask", "entry", "ceiling" and "own_login" to touch any of them.

After this change an agent has **four settings**, and each is a plain page section:

| What the admin sees | What it means | Where it is today |
|---|---|---|
| **Who can use it** | The access list, exactly as it is now: a person or group, and a role. | unchanged (`apps/agents/grants.py`) |
| **Read mode** | What someone who may only read gets. Every agent has one from the start. An admin can leave it on canopy's default, or press **Customise** to give this agent its own list of what it may do while answering. **Go back to the default** undoes it. | the default exists (Phase 1); "customise" is today a hand-written `ask` capability in the YAML |
| **Connected sites** | "This agent can be reached from Connect Labs." One row per site, each with: which of the site's tools it may use (default: whatever the site itself allows), the agent's own login on that site (kept out of the conversation), and optionally a customised list of what it may do there. The access list says who may use it from that site. | `sites`, `ceiling`, `own_login` inside a "capability" |
| **Sensitive data** | One switch: "this agent handles sensitive data". On, it answers only inside the safest confinement, for everyone including its owner, and only on boxes set aside for sensitive data. Off by default. Shows who turned it on and when. | `data: sensitive` inside one capability |

Words that **leave the product** (UI, docs, REST, MCP, CLI): *interface*, *capability* (in the agent
sense; the cloud-machine, runner, storyboard and workspace-permission senses are different things and
stay), *door*, *ask*, *entry*, *ceiling*, *own_login*, *pages*. Replacements are in section 3.

Two things that look like deletions are not: the access list still lets you give a person "use it only
from this site" (it always did, as `use:<name>`), and read mode is still the thing a `read_only` person
gets. What disappears is the YAML, the container called "interface", and the need to say the word
"capability" to an admin.

## 2. What the code does today (grounded)

### 2.1 One JSON document drives everything

`Agent.interface` (JSONField) and `Agent.interface_source` (the YAML text) plus
`interface_published_at/_by` live at `apps/agents/models/agent.py:46-60`. `interface.parse`
(`apps/agents/interface.py:207`) validates a document `{capabilities: {<name>: {description, entry,
tools, bash, read_paths, write_paths, pages, sites, ceiling, own_login, data}}}`. One capability name
can mean four things, decided by what it contains:

* `ask` / `read`: **read mode's override** (`interface.read_mode_override`, `interface.py:136`;
  `is_read_mode`, `interface_default.py:30`). No override means canopy's `default_read_profile`
  (`interface_default.py:77`), stamped `read`.
* a capability with **`sites`**: a Connected-site connection. It is selected when the conversation's
  `embed_app` is in its `sites` (`_site_capability`, `interface.py:411`) and the asker's permissions
  open it (`roles.can_use`, `roles.py:97`: `write` opens all, `use:<name>` opens that name, `read` opens
  only read mode). `profile()` appends canopy's gateway tools and the page tools to it
  (`interface.py:673-681`).
* a capability with **`pages`**: selected by what a page declares it shows (`_page_capability`,
  `interface.py:373`). Superseded by `sites` (module docstring `interface.py:61`).
* a capability with **`data: sensitive`**: forces confinement for everyone; structurally restricted
  (`sensitive_data.structural_refusals`, `sensitive_data.py:71`: must name `sites`, tools only the
  gateway's/`current_page`/`who_is_asking`/`page_*`/read-only locals, no bash, no writes).

Everything about "is this agent set up for site X" is derived from the capability list: `page_sites`
(`interface.py:437`), `page_site_of` (`:449`), `site_ceiling` (`:460`, a union over capabilities),
`own_login_of` (`:471`), `page_turn` (`:479`).

### 2.2 The consumers that read the document

Decision: `access._sensitive_capability`/`_sensitive_rule` (`access.py:142-195`),
`access._capability_for` (`:228-246`), `access.roster` (`:388,409`), `interface.grant_facts` /
`_held_permissions` (`interface.py:568-611`). Gateway: `host_gateway.py:239-257` (a confined turn's
capability must list the site; a full turn needs `page_site_of`), `:298` (sensitive conversations reach
only a `data: sensitive` capability). Statement: `execution.facts_for` (`execution.py:55-66`) with
`sensitive_data.as_claim` (`sensitive_data.py:105`: `declared_by`/`declared_at` are *whoever last
published the interface*). Site listing: `site_agents.site_capabilities` and `agent_entry`
(`site_agents.py:142-209`). Runner claim: `ClaimedTurnOut.page_site` from `interface.page_turn`
(`harness/schemas/turns.py:295,388`). Plus the views in section 6.

### 2.3 What the runner reads

The runner never reads the interface. It reads the *envelope* the server builds: `capability` as
`{name, description, entry, tools, bash, read_paths, write_paths, [default_profile_version], [data]}`
(`interface.profile`, `interface.py:647-682`; consumed by `runner/canopy_runner/canopy_runner/caller.py:228-300`
and `runner/ec2/cloud_runner.py:12650-12740`). It reads the *content* of the profile, but also uses the
**name** in two places:

* `session_naming.restricted_key` (`runner/canopy_runner/canopy_runner/session_naming.py:138`): a
  confined turn continues the session `<thread>#<capability name>`;
* `cloud_runner._kept_identity` (`runner/ec2/cloud_runner.py:4962`): a kept sensitive session resumes
  only under the same capability name.

So **a stamp's name is wire**: changing `ask` to `read`, or `connect` to `connect-labs`, would split every
live conversation's runner session. This is the reason section 4 keeps every key verbatim.

### 2.4 Who uses what today (live data, 18 agents)

Section 4 has the per-agent table. In short: 11 agents have no published capability (default read mode
only); 4 (echo, eva, hal, oolong) publish only an `ask` override; **3 have site connections**: ACE
(`canopy` on site `canopy-web`, `connect` on `connect-labs` with a ceiling and an own login), Trellis
(`canopy` on `canopy-web`, no ceiling), and labsdata (`data` on `connect-labs`, `data: sensitive`).
**No live agent uses `pages`, a second site on one capability, two capabilities on one site, or a named
door with no site.** That is what lets the model below be much smaller than the YAML it replaces.

## 3. The final vocabulary

| Old word | New word | UI | Docs | REST | MCP tool | CLI |
|---|---|---|---|---|---|---|
| interface | (no container; three settings) | Settings → Access has three cards | "the agent's access settings" | `/api/agents/{slug}/interface` → see below | see below | `canopy agent interface` → `canopy agent read-mode`, `sites`, `sensitive` |
| `ask` capability (override) | **Read mode, customised** | "Customise" / "Go back to the default" | read mode | `PUT/DELETE /api/agents/{slug}/read-mode` | `put_read_mode`, `reset_read_mode` | `canopy agent read-mode get|set --file|reset` |
| `read` | Read mode (default) | "canopy's default" | | `GET …/read-mode` shows `source: default|custom` | `get_read_mode` | `read-mode get` |
| capability with `sites` / "door" | **Connected site** (one per site) | "Connected sites" | | `GET …/connected-sites`; `PUT/DELETE …/connected-sites/{site}` | `list_connected_sites`, `put_connected_site`, `delete_connected_site` | `canopy agent sites list|set|remove` |
| `use:<name>` access line / "door" in the line form | "Only from <site>" | the line form lists the agent's connected sites | | unchanged wire: `capability: "<key>"` on the line | unchanged | unchanged |
| `ceiling` | **Site tools it may use** | | | field `ceiling` (kept, it is stored data) | | |
| `own_login` | **Its own login on this site** | "never carried into a conversation held on that site" | | field `own_login` (now a list of credential names) | | |
| `entry` | **Starts with** | on the customise panel | | field `entry` | | |
| `data: sensitive` | **Sensitive data** | switch + "turned on by X on Y" | | `PUT …/sensitive-data {on}` | `set_sensitive_data`, `get_sensitive_data` | `canopy agent sensitive on|off` |
| `pages` | removed | | | refused with a pointer | | |
| `validate_interface` | `dry_run: true` on each PUT | the form validates as you type | | | | |

Internally the word *profile* stays for "the lists of tools, commands and folders" (`tools`, `bash`,
`read_paths`, `write_paths`, `entry`); the UI says "what it may use". The word **stamp** stays for what a
turn carries in `Turn.capability`.

No umbrella noun replaces "interface" on purpose. "Reach" and "answering" were considered and rejected:
`reach` already means a runner-availability condition in admission rules
(`apps/admission/…`, the `reaches` choices) and in `runner-routing.md`. One read of all three settings
for an agent that explains what another agent offers (the Trellis use) goes in `GET /api/agents/{slug}`
as three additive fields (`read_mode`, `connected_sites`, `sensitive_data`), which Trellis can already
call because `get_agent` is in its allowlist (section 5).

## 4. The live data and how each agent maps

Read on 2026-11-04 with `get_interface` and `get_access_list`. `published_at` is the interface's
last save.

| Agent (workspace) | Stored today | New model |
|---|---|---|
| ada, budgy, fizzy, gshubdesk, jarvis, lex, muse, neo, otto, sift (dimagi/connect/strategy/global-solutions/commcare), bailey (marshellis) | `capabilities: {}` (`is_default: true`, never published) | nothing stored: default read mode, no connected sites, sensitive off. 11 agents. |
| echo, eva, hal, oolong | one `ask`; no sites. hal's has no `sites`/`ceiling` keys at all (older save); oolong's has no email and no `Write` (`entry /oolong:policy-qa`, bash `bin/oolong-confluence …`) | `Agent.read_profile` = the `ask` fields verbatim (custom read mode). Stamp stays `ask`. |
| trellis (dimagi) | `ask` (14 bash patterns, 18 tools including `mcp__*canopy-web__get_interface`) + `canopy` {sites: [canopy-web], no ceiling, bash `gh api …` (stale: `gh` is not logged in in a confined session)} | `read_profile` = `ask`. `SiteConnection(site=canopy-web, key=canopy, ceiling=[], profile=<canopy fields>)`. The access list has no `use:canopy` line, so today only `write` holders reach it on that site; unchanged. |
| ace (connect) | `ask` + `canopy` {sites [canopy-web], 4 `ceiling` globs} + `connect` {sites [connect-labs], ceiling `mcp__*connect_labs__*`, `own_login {connect-labs: [LABS_MCP_TOKEN]}`, gdrive tools} | `read_profile` = `ask`. `SiteConnection(canopy-web, key=canopy, ceiling=4 globs)`; `SiteConnection(connect-labs, key=connect, ceiling=[…], own_login=[LABS_MCP_TOKEN])`. Access lines 44, 45, 46 (`capability: connect`/`canopy`) keep working untouched. |
| labsdata (connect) | `data` {sites [connect-labs], `data: sensitive`, entry `/labsdata:answer-data`, ceiling `mcp__*connect_labs__*`, no bash, no writes}; published by ada@dimagi-ai.com at 2026-10-23T03:10:37Z | no `read_profile` (default read mode; labsdata has no mailbox so the default has only the repo `curl` reads). `SiteConnection(connect-labs, key=data, entry, ceiling, profile=…)`. `Agent.sensitive_data=True`, `sensitive_data_set_by=ada@dimagi-ai.com`, `_set_at=2026-10-23T03:10:37Z` (copied from `interface_published_by/_at`, so `declaration.declared_by/at` is byte-identical on day one). Lines 47/48 (`capability: data`) untouched. |

Checks run on this data: no `pages` anywhere; no capability lists two sites; no site is listed by two
capabilities of one agent; no named door without `sites`; no `# comment` is in any saved YAML (every save
came from the form, which re-dumps); no admission rule in `connect` or `dimagi` has a `capability`
condition (admission rules in `commcare`, `strategy`, `operations`, `global-solutions`, `marshellis` were
not read and are a preflight query, section 5.4). These facts are what the migration asserts before it
runs (section 5.2), so the simplifications are verified, not assumed.

## 5. The data model and the migration

### 5.1 Where each setting is stored

```
Agent
  read_profile            JSONField null   # the custom read mode: {description, entry, tools, bash, read_paths, write_paths}; null = canopy's default
  sensitive_data          BooleanField default False
  sensitive_data_set_by   FK(User) null, on_delete SET_NULL
  sensitive_data_set_at   DateTimeField null
  card_changed_at         DateTimeField null   # replaces interface_published_at for the A2A card/ETag (agent_card.py:211)

SiteConnection             # new, apps/agents/models/site_connection.py
  agent        FK(Agent, related_name="site_connections", CASCADE)
  site         CharField(100)   # a Connected site's name; matched to AppCredential(name, workspace) at use, as host_gateway.py:233 does now
  key          CharField(41)    # the stamp and wire name; immutable; ^[a-z][a-z0-9_]{0,40}$; not "read", "ask" or ""
  description  CharField(500, blank)
  profile      JSONField null   # null = canopy's site default profile; else {entry, tools, bash, read_paths, write_paths}
  ceiling      JSONField default list     # tool globs, as today
  own_login    JSONField default list     # credential names withheld on this site's pages
  created_at, updated_at, updated_by FK(User)
  UniqueConstraint(agent, site); UniqueConstraint(agent, key)
```

Why this shape:

* **A model for sites, columns for the rest.** Read mode and sensitive data are one value per agent: a
  nullable JSON and a boolean are the whole truth. Connected sites are a list with a natural key and
  queries ("which agents serve site X", `contact_api._for_contacts`, the Connected sites page), so they
  are rows, with a unique constraint instead of a `_caps_for_site` union (`interface.py:444,460`).
* **One connection per (agent, site)** deletes the only genuinely subtle function, `site_ceiling`'s
  "union unless any is unbounded" (`interface.py:460-468`). Live data has no agent that needs two.
* **`key` is kept** because it is wire (section 2.3, section 7). New connections default `key` to a slug
  of the site (`connect-labs`); migrated ones keep `canopy`, `connect`, `data`. The UI shows it as a
  small "internal name", editable only at creation.
* **`profile = null` is canopy's *site default profile*** (new, pinned like the read default by a test):
  `Read`, `Skill`, `ToolSearch`, `mcp__*canopy-web__who_is_asking`, `mcp__*canopy-web__current_page`,
  no bash, no writes, no entry; the gateway and page tools are appended by `profile()` as now. It
  satisfies the sensitive structural rule by construction, so "add a site, switch sensitive on" needs no
  customising. Migrated connections keep their explicit profile (exact behaviour); an admin can later
  press "go back to the default". The envelope gains an additive `default_profile: "read"|"site"` beside
  `default_profile_version`; nothing reads it but humans.
* **Not merged into `AppCredentialAgent`** (the site-owner's "this site may show this agent"). Two
  authorities set those two things (the site's owner allows; the agent's admin configures) and
  `site_agents.agent_entry` already reports the gap as a problem (`site_agents.py:185-188`).
* **`pages` is not carried over.** No live agent has it; the hosts' `setPageState` resource still feeds
  `current_page`/`page_*` tools, which are unaffected.
* **History is the existing plumbing.** Each write route is `@gate(..., setting=True)`; scopes
  `agent:<slug>/read-mode`, `agent:<slug>/site/<site>`, `agent:<slug>/sensitive-data` with
  `audit.note_change` before/after. No new history table (`docs/architecture/settings-audit.md`).
* **Sensitive data is agent-level**, as the parent spec directed: the effect is "every connection of this
  agent is a sensitive connection" (its profile must pass `structural_refusals`, sites required). It is
  *not* a property of read mode: a sensitive conversation is never read mode, exactly as
  `_sensitive_rule` (`access.py:188-195`) refuses today. The explicit profile choice the parent spec
  asked for is therefore: **a sensitive conversation runs in its site connection's profile, default or
  customised, and nowhere else.**

### 5.2 The migration (additive, then data, then cutover, then drop)

Three migrations, because CLAUDE.md requires that a `RunPython` that updates rows other tables
reference goes in its own migration ahead of any schema change to those tables, and `Agent` is
referenced everywhere.

* **M1 `agents/0120_answering_settings`** (schema only): add the `Agent` columns and `SiteConnection`.
  Reversible by dropping them.
* **M2 `agents/0121_backfill_answering_settings`** (data only, idempotent, reversible by clearing the new
  columns/rows). Per agent, from `agent.interface`:
  1. **Preflight assertions** (the migration aborts, naming the agent and the rule, rather than guess):
     no capability has non-empty `pages`; no capability lists more than one site; no site appears in two
     capabilities of one agent; every capability other than `ask`/`read` has `sites`; if any capability
     is `data: sensitive`, every site connection of that agent is. Live data satisfies all five
     (section 4); a staging copy of production is the dry run.
  2. `read_profile` = the `ask` (else `read`) capability's profile fields, dropping `pages/sites/ceiling/
     own_login/data`; the order matches `READ_MODE_NAMES = (ask, read)` (`interface_default.py:27`).
  3. One `SiteConnection` per (capability, site): `key` = the capability name, `site`, `description`,
     `profile` = the stored fields **without** the gateway/page tools (they are added at read time, so the
     stored form is what `parse` wrote), `ceiling`, `own_login` = `own_login[site]`.
  4. `sensitive_data=True`, `_set_by=interface_published_by`, `_set_at=interface_published_at` when a
     capability was sensitive. `card_changed_at = interface_published_at`.
  5. **Round-trip check inside the migration**: `legacy_interface(agent)` (the inverse mapping, also the
     rollback mirror, 5.3) must equal the stored `interface` after `parse`; any difference aborts. This
     is what makes "no behaviour change" a checked property, not a hope.
  The migration's test (`needs_migrations`, walks the graph) is deleted once a deploy includes it, per the
  CLAUDE.md rule; `tests/test_migrations_apply.py` stays.
* **M3 (final phase, own release) `agents/0122_drop_interface`**: drop `interface`, `interface_source`,
  `interface_published_at`, `interface_published_by`. Only after the rollback window (5.3) closes.

### 5.3 Rollback

Until M3, **the legacy `Agent.interface` JSON is still written**: every write through the new routes
rebuilds it with `legacy_interface(agent)` in the same transaction, so a code rollback to the previous
release finds a current interface and runs exactly as before. Conversely, during the alias window the
old PUT routes translate the old document into the new settings (`settings_from_interface`, the same
function M2 uses) in one transaction. Rollback steps: (1) redeploy the previous image (no migration to
undo; M1/M2 are additive); (2) if the new tables are suspect, clear them and re-run M2 from `interface`.
Data written *only* in the new model since the cutover is covered by the mirror. M3 is the one
non-rollbackable step, which is why it ships alone, at least 14 days after the last reader moved, and
after a snapshot (`SettingChange` rows plus a JSON dump of every `interface`, kept in the PR description
and in the deploy artifacts).

### 5.4 Stored stamps, in-flight turns, rules and lines

* `Turn.capability` (`harness/models/turn.py:215`) is never rewritten. Resolution is
  `profile(agent, stamp)`: `""` -> full; `read`/`ask` (`is_read_mode`) -> `read_profile` or the default;
  a `SiteConnection.key` -> that connection's profile; anything else (a retired or deleted key) -> the
  empty deny-all profile, exactly today's fail-closed behaviour (`interface.py:661-667`). Because migrated
  keys equal the old capability names, **an in-flight or queued turn stamped `connect` resolves to the
  same profile before and after the cutover**. A key cannot be renamed, only deleted; deleting a
  connection fails closed for turns already stamped with it.
* **Stamping `ask` vs `read` stays as Phase 1 left it** (`ask` when an override exists, `read` otherwise;
  `is_read_mode` treats them as one). Unifying them is the tempting tidy-up and a trap (section 9).
* `AdmissionRule.capability` is compared through `is_read_mode` and otherwise by string equality with
  `turn.capability` (`admission/evaluate.py`, `rules.py`). Unchanged, because the strings are unchanged.
  The UI label "Confined: a caller held to one capability" (in the rule form's `choices.access`) becomes
  "Confined: answered in read mode or from a connected site". Preflight query before M2, per workspace:
  `AdmissionRule.objects.exclude(capability="")` must be empty or each value must be `read`, `ask` or a
  migrated key.
* Access lines (`AgentGrant.capability`, `use:<key>` permissions): unchanged. The line form's picker
  (`doors` in `GET …/access-list`, `api_access_list.py:131`) is renamed `connected_sites` and lists
  `SiteConnection.key` with the site's name; the "line names a capability you have not published" warning
  (`api_access_list.py:106-114`) becomes "this agent has no connected site called X". Workspace default
  lines, which span agents, keep taking a typed key.

## 6. Every consumer to change

**Decision and envelope (P2a).** `apps/agents/access.py:142-195` (`_sensitive_capability`: the site
branch asks the connection; the `pages` branch is deleted; naming a key is "is it a connection of this
sensitive agent"), `:228-246` (`_capability_for`: connection for the conversation's site if the asker's
permissions open it, else read mode), `:388,409` (roster `iface`/`reachable`);
`apps/agents/roles.py:97` (`can_use` unchanged semantics); `apps/agents/interface.py` is dissolved:
`read_mode.py` (ex `interface_default.py`: `is_read_mode`, `default_read_profile`, `profile`),
`site_connections.py` (`page_sites`, `page_site_of`, `site_ceiling`, `own_login_of`, `page_turn`,
`GATEWAY_TOOLS`, `PAGE_TOOLS`, `connection_for_site`), and the standing helpers (`grant_facts`,
`_held_permissions`, `session_writer`, `offered_to`, `reachable`) move beside `access.py`;
`apps/agents/sensitive_data.py` shrinks to `structural_refusals`, `handles_sensitive`
(`agent.sensitive_data and capability in connection keys`), `as_claim`, and the constants (the 70 lines of
YAML surgery `with_sensitive`/`SourceEditError` are deleted); `apps/harness/caller_context.py:422,504,
676-703` (`page_turn`, `grant_facts`, `profile` imports); `apps/harness/caller_tokens.py:133,186-199`
(`profile`, `handles_sensitive`: unchanged semantics); `apps/harness/schemas/turns.py:295-390`
(`page_site` via `page_turn`); `apps/agents/credentials/delegations.py:451` (message text only);
`apps/agents/holding.py:38` (`sensitive_capabilities(agent.interface)` becomes `agent.sensitive_data`);
`apps/agents/read_mode_gate.py:28` (reads `read_profile`).

**Gateway and hosts (P2b).** `apps/host_grants/host_gateway.py:239-257` (a confined turn: the connection
whose key is the stamp must have `site == site`; a full turn: `connection_for_site`; `ceiling` is the
connection's), `:298` (`handles_sensitive`); `apps/host_grants/execution.py:55-66` (unchanged: it calls
`as_claim`); `apps/host_grants/site_agents.py:142-209` (`site_capabilities` reads the connection for this
site and yields the same dict; the "no capability naming {site} in `sites:`" problem text becomes "no
connected site for {site}: add it on its Access settings", with the same `#reach` anchor renamed in step
P4 and the old anchor kept as an alias in the SPA); `apps/host_grants/site_needs.py` and the Connected
sites page (`frontend/src/pages/connectedApps/SiteNeedsTable.tsx`); `apps/mcp_apps_host/
mcp_apps_views.py:376` (ceiling from the stamp's connection); `apps/mcp/tools/site.py:27` (docstring);
`apps/contacts/contact_api.py:25,497` (`page_sites`); `apps/host_grants/live_probe.py` (no change: it
opens its own ceiling and never reads the interface).

**Views (P2c).** `apps/agents/agent_card.py:99,150-163,211` and `schemas/agent_card.py:73` (skills = the
custom read mode as `ask` + each connection by key, with `description`; public card unchanged;
`card_changed_at`), `apps/agents/api_a2a.py`, `apps/agents/topology.py:71-90,121` (the
`no-interface` basis string is **kept** as wire, label changes to "offers nothing"; `capabilities` list
unchanged), `apps/agents/health.py:76-77` (`has_interface` keeps meaning "customised read mode or a
connected site"; both keys stay in the schema, `apps/workspaces/schemas.py:565`), `apps/workspaces/
schemas.py:439` (docstring), `apps/admission/models.py:89` (help text), `apps/slack/services.py:293`
(wording), `apps/agents/schemas/agents.py:684-760`.

**Routes and tools (P3).** `apps/agents/api.py:659-790` -> new routes (section 7). `apps/agents/
api_access_list.py:106-131`.

**Frontend (P4).** `frontend/src/components/agents/{AgentInterfaceView,InterfaceEditor,InterfaceEditorParts,
interfaceModel}.tsx` (+ `AgentInterfaceView.test.tsx`, `aceInterface.fixture.json`) are replaced by three
cards: `ReadModeCard`, `ConnectedSitesCard` (a row per site, an "Add site" picker fed by the workspace's
registered sites), `SensitiveDataCard`; `frontend/src/pages/agents/settings/ChannelsTab.tsx:2,21` mounts
them; `frontend/src/api/agents.ts:818-850`; `components/agents/{AccessLineForm,accessWords,
AgentAccessList,AgentAccessPeople}.tsx` (door -> connected site wording); `pages/WorkspaceAdmissionRulesPage.tsx`
(label), `pages/agentTopology.ts`, `pages/topology/TopologyMapPage.tsx`, `pages/health/WorkspaceHealthPage.tsx`,
`pages/connectedApps/SiteNeedsTable.tsx` (deep link to the site's row), `frontend/src/guide/*` for any
page text. `AgentCapabilitiesSection.tsx`/`CapabilityParts.tsx` are the *cloud*-capability UI and are not
touched. `generated.ts` is regenerated and committed.

**Runner (no change).** `caller.py:228-300`, `native_permissions.py`, `cloud_runner.py:12650-12740`,
`session_naming.py:138`, `page_site.py`, `cloud_runner.py:4962,6267`: all read the envelope, whose shape is
unchanged. Their tests stay as the guard. Comments that say "declared interface" are updated in the next
runner release, not for this work.

**Plugin / CLI / agent repos (P5).** In the canopy plugin repo (v0.2.839 inspected in the plugin cache):
`runtime/src/orchestrator/agent_cli.py:206-240` (`agent interface get|set`) and `agent_client.py:236-246`
(`get_interface`, `put_interface_source`) gain the new commands and keep `interface get|set` one release
as hidden aliases that print a one-line deprecation to stderr and call the new routes;
`agent-core/using-canopy.md:44` (the `canopy agent interface get` pointer); `hooks/profile_guard.py`,
`hooks/caller_context.py:110-131`, `runtime/src/orchestrator/caller.py:170-224`: the strings
`no-interface` and `capability:<name>` they read stay (section 7), only docstrings change. Agent repos
that mention it: ace (`commands/ask.md:9`, `skills/inbox-triage/SKILL.md:28`, `CLAUDE.md:75`), echo
(`commands/ask.md:10`), eva (`CLAUDE.md:41`), trellis (`CLAUDE.md:79`, `skills/explain-canopy/SKILL.md:79`,
`tests/test_agent_directory.py:11-14`, which asserts the allowlist contains `get_interface`).
**Trellis is the one real dependency**: its stored read mode lists `mcp__*canopy-web__get_interface` (and
its `canopy` connection lists it too). Before the alias window ends an agent admin edits those two tool
lists to `get_agent` through the new routes (audited), then the Trellis repo test and docs follow.

**Docs.** `docs/architecture/access.md` (the Interface section becomes "Access settings"),
`access-direct-use-and-admission.md`, `access-confinement-and-act-as.md`, `api-surface.md` (route table,
and the stale "Settings → Interface" line), `mcp-surface.md`, `embedding-actions-and-access.md`,
`embedding-internals.md`, `tenancy.md` (sensitive conversations), `roles.md`, `runner-routing.md`,
`runner-flags-and-schedules.md`, `runner-credentials.md`, `frontend.md`, `capabilities.md` (cross-reference
only), `CLAUDE.md` (the invariant "Read mode is the default confined door…" and the Where-to-read lines
that say "door"). `tests/test_docs_links_resolve.py` and `tests/test_claude_md_budget.py` stay green.

## 7. Wire contracts that must not change, and the tests that pin them

| Contract | Fields that stay byte-for-byte | Pinned by |
|---|---|---|
| `Canopy-Execution` statement (signed, verified by hosts) | `agent`, `capability`, `declaration {data, capability, declared_by, declared_at}` or null, `runner`, `runner_posture`, `requirements`, `turn`, `session`; `declaration.data == "sensitive"` is what `Execution.sensitive_clearance` checks (`sdk/.../host/execution.py:95-118`) | `tests/test_execution_statement.py` (`test_outside_sensitive_…`, `test_sensitive_clearance_refuses_each_missing_fact`, `test_turning_sensitive_off_closes_the_door`), `sdk/python/tests/test_host_execution.py` |
| Site-agents JWS (`GET /api/sites/agents`) | `v`, `site`, `runner_flags`, `agents[]` with `capabilities[] {name, description, handles_sensitive, sensitive_set_by, sensitive_set_at}`, `ready`, `problems`, `problem_links`, `runners {flags_available, ready_with}`; the `version` digest changes exactly when content does | `tests/test_site_agents.py` (`test_handles_sensitive_reflects_the_interface_setting`, `test_version_changes_exactly_when_the_content_does`, `test_the_response_is_signed_by_the_execution_key`), `sdk/python/tests/test_site_agents.py`, `test_django_site_agents.py` |
| Caller envelope | `profile` (`full|confined`), `capability {name, description, entry, tools, bash, read_paths, write_paths, default_profile_version, data}`, `permissions` (including `use:<key>`), `granted_by` values `capability:<name>`, `refused`, `owner`, `admin`, `no-interface` (read by the plugin, `runtime/src/orchestrator/caller.py:196`) | `tests/test_caller_envelope.py`, `tests/test_default_read_profile.py` (+ `_adversarial`), `tests/test_read_mode_alias.py` |
| Claim payload to the runner | `page_site {site, withheld}`, `caller_context`, `mcp_token` | `tests/test_site_gateway.py`, `tests/test_site_acts_within_its_agents.py`, runner tests for `page_site` and `caller` |
| Stored strings | `Turn.capability`, `AdmissionRule.capability`, `AgentGrant.capability`, `use:<key>` | `tests/test_admission.py`, `tests/test_access_matrix.py`, `tests/test_session_acl.py` |
| A2A card | skill `id` = capability key; public card lists only customised read mode + connections | `tests/test_a2a_agent_card.py` |
| Sensitive behaviour | confinement for everyone; gateway refuses `undeclared`; sensitive box holds only sensitive agents; sensitive caller token reaches only `SENSITIVE_CANOPY_TOOLS` | `tests/test_sensitive_capability.py`, `tests/test_sensitive_read_doors.py`, `tests/test_sensitive_data_box_is_dedicated.py`, `tests/test_sensitive_metadata_only.py` |
| MCP surface | a tool per route; `tests/test_mcp_api_tools.py:366-370` names `publish_interface` | updated in P3 to assert both the new and the deprecated names |

**New golden tests added first (P0)**, so the change is checked against today rather than against itself:
`tests/fixtures/live_interfaces_2026-11-04.json` (the 18 documents above, read-only fixtures),
`tests/legacy_interface.py` (a frozen copy of today's `profile`, `page_sites`, `site_ceiling`,
`own_login_of`, `handles_sensitive`, `site_capabilities`), and `tests/test_answering_settings_equivalence.py`
which for each fixture agent migrates and asserts, for every stamp (`""`, `read`, `ask`, each key), the
new `profile()` equals the legacy one; the site-agents `agent_entry` dict is equal; `execution.facts_for`
is equal; `page_turn` is equal; and `decide()` is equal over the matrix {owner, admin, `write` editor,
`read_only`, a contact with `use:<key>`} x {no site, each site}.

## 8. Phased, PR-sized plan

Each step is its own PR, in this order. "Verify" is what runs against the live agents before and after.

| # | PR | Risk | Verify |
|---|---|---|---|
| P0 | Characterization: fixtures, `legacy_interface`, equivalence test, `manage.py answering_snapshot --json` (prints per agent: `profile()` per stamp, `decide()` matrix, site-agents content per registered site, `facts_for` for any sensitive agent, A2A public and extended cards, topology edges). No behaviour change. | Low | Run the snapshot against production read-only and commit its hash to the PR. |
| P1 | Schema M1 + data M2 + the new service module (`legacy_interface`, `settings_from_interface`, accessors). Old routes also call `settings_from_interface` after saving, so the new tables stay current. Readers unchanged. | Medium (data migration on `Agent` rows; the migration's own five assertions and round trip) | Dry-run on a prod copy; after deploy, `answering_snapshot` diff is empty and `SELECT count(*) FROM agents_siteconnection` = 4 (ace x2, trellis, labsdata), `read_profile IS NOT NULL` = 6 (ace, echo, eva, hal, oolong, trellis), `sensitive_data` = 1. |
| P2a | Decision, envelope, caller tokens, holding, read-mode gate read the new model; new writes mirror to `interface`. | **Highest** (the decision path) | `answering_snapshot` diff empty; equivalence test green; then one real turn per posture on ACE: a `read_only` asker on the Connect Labs page (read mode), a `use:connect` contact (connect), an editor (full + `page_site`), plus a labsdata sensitive turn end to end on its sensitive box. |
| P2b | Gateway, execution, site-agents, mcp_apps_views, contact_api read the new model. | High (host-facing) | Site-agents JWS payload canonical-JSON equal to the P0 snapshot for `connect-labs` and `canopy-web`; `Canopy-Execution` for a labsdata call equal; the SDK conformance suite (`canopy_sdk/conformance`) green against it; live probe run from the Connected sites page. |
| P2c | A2A cards, topology, health, roster, Slack text. | Low | Snapshot diff empty. |
| P3 | New REST routes + MCP tools + dry-run; old routes kept as `deprecated=True` thin translators; `GET /api/agents/{slug}` gains the three fields; OpenAPI + `generated.ts`; `tests/test_mcp_api_tools.py`. | Medium (a published API surface) | `/api/openapi.json` lists both; old and new reads agree for all 18; the Trellis tool-list edit made through the new route. |
| P4 | Frontend: three cards, line-form wording, deep links, `#reach` anchor aliased. | Low | Visual check of ACE (two sites, an own login), labsdata (sensitive on), Trellis (customised, one site), a default agent. |
| P5 | Docs; canopy plugin release (new commands, hidden aliases); agent repos' text; SDK docstring wording (needs an SDK version bump, so batch it with the next SDK change rather than alone). | Low | `tests/test_docs_links_resolve.py`, `tests/test_claude_md_budget.py`. |
| P6 | After the alias window and 14+ days with no reader on the legacy columns: stop mirroring, delete `interface.py`/`parse`/YAML editor/`pages` code, old routes return 410 with a pointer, then M3 drops the columns in its own release. | Medium (irreversible once M3 runs) | MCP audit shows zero calls to the old tool names in 30 days; snapshot diff empty; backup dump attached. |

The ordering rule behind it: readers move to the new model one family at a time while writes keep the
legacy JSON current, so every step is rollbackable by redeploying the previous image, and the only
irreversible step (M3) is last and alone.

## 9. What I recommend NOT doing

1. **Do not rename any wire field a host or runner verifies** (section 7): `capability` and `declaration`
   in `Canopy-Execution`, `capabilities[]` in the site-agents answer, `granted_by: capability:<name>` and
   `no-interface`, the `use:<key>` permission. Hosts and the plugin would break with no way to roll the
   host side back. The word "capability" stays in the SDK; only canopy's UI loses it.
2. **Do not rewrite stored data**: `Turn.capability`, `AdmissionRule.capability`, access lines. The
   migration creates new rows beside them; nothing referencing a name is touched.
3. **Do not unify the `ask` and `read` stamps now.** The runner keys a confined session by
   `<thread>#<name>` (`session_naming.py:138`), so changing the stamp of ACE, Trellis, eva, echo, hal and
   oolong would start every live conversation over. The alias in `is_read_mode` already makes them one
   thing wherever it matters.
4. **Do not make sensitivity both agent-level and per-site.** One switch. An agent that needs sensitive and
   non-sensitive sites is two agents (open question 1).
5. **Do not invent a profile library or presets** ("email reader", "browser") to make customising easier.
   The default is the preset; customising is a list of tools, commands and folders. More modes is the
   anti-pattern the parent spec warned about.
6. **Do not keep YAML as the storage or editing format.** It is what nobody understood; the sole reason
   to keep it was comment preservation and no saved document has a comment. The CLI takes a file, but
   of the profile only.
7. **Do not carry `pages` over.** Zero users, and `sites` superseded it in 2026-09.
8. **Do not merge `SiteConnection` into `AppCredentialAgent`.** Two different people authorise those two
   facts, and the listing already reports their disagreement as a problem.
9. **Do not change the default read profile or `READ_PROFILE_VERSION` in this work.** A pinned default
   changing in the same release as its storage moving makes every diff ambiguous.
10. **Do not "fix" Trellis's `gh api` patterns or its stale tool lists inside the migration.** Migrate
    verbatim, fix as a normal audited edit afterwards.
11. **Do not touch the other meanings of capability** (`Runner.capabilities`, cloud capabilities,
    `Storyboard.capability`, workspace permissions) or the A2A card's "interfaces".
12. **Do not drop the legacy columns in the same release that moves the last reader.** That is the
    rollback window.

## 10. Deprecation of the renamed tools and routes (alias window)

| Old | New | During the window |
|---|---|---|
| `GET /api/agents/{slug}/interface` (`get_interface`) | `GET …/read-mode`, `GET …/connected-sites`, `GET /api/agents/{slug}` fields | kept, `deprecated=True`, returns `legacy_interface(agent)` (the same function as the rollback mirror) plus `deprecated_use` |
| `PUT …/interface` (`publish_interface`) | `PUT …/read-mode`, `PUT …/connected-sites/{site}`, `PUT …/sensitive-data` | kept; parses the old YAML as today (`pages` now a 422 pointing at connected sites), applies it through `settings_from_interface` in one transaction, audits each resulting setting change |
| `POST …/interface/validate` (`validate_interface`) | `dry_run: true` on each PUT | kept, same errors |
| `PUT …/interface/capabilities/{name}/sensitive` (`set_capability_sensitive`) | `PUT …/sensitive-data` | kept; `{name}` must be a connection of the agent; sets the agent-level switch |
| `DELETE …/interface` (`reset_interface`) | `DELETE …/read-mode` + delete each connection (a UI "reset all" button calls both) | kept |

Each deprecated route has `deprecated=True` (OpenAPI marks it), a `Deprecation: true` and `Sunset` header,
and a description starting "Deprecated: use X". MCP exposes every route as a tool, so the old tool names
keep working and say so in their descriptions. **The window ends on use, not on a date**: the MCP audit
(`apps/mcp/audit.py`, `list_mcp_calls`) is queried for the five old names; at zero calls from any caller
for 30 days the routes become `410 Gone` with a pointer (the precedent in CLAUDE.md for retired
`marketplace.json`/`plugins/…`), and the tools disappear in the next release. The canopy plugin keeps
`canopy agent interface get|set` as hidden aliases for one release, printing a deprecation line, then
removes them. The scoped confined-session token (`caller_tokens`) allows tools by the profile's patterns,
so while Trellis's stored tool list still names `get_interface` the alias must exist; that is why the
Trellis edit (section 6) gates the end of the window.

## 11. Open questions for Jonathan

1. **Is sensitive data an agent-level switch with no per-site exception?** Today it is per capability, so
   one agent could mix. Live: only labsdata uses it, with one site. *Recommend:* agent-level; an agent
   needing both is two agents.
2. **One connection per (agent, site)?** *Recommend:* yes. Live data has no agent with two on one site,
   and it removes the union rule for ceilings.
3. **Keep the internal names (`canopy`, `connect`, `data`) as wire forever?** Hosts (Connect Labs,
   at least for `data`) and runner sessions depend on them. *Recommend:* yes, hidden in the UI as
   "internal name"; never rename.
4. **Should `read_only` people reach a connected site's profile without a `use:<key>` line?** Today they
   get read mode on the site's page unless a line opens the connection (`roles.py:97`), which is why ACE
   carries lines 44-46. *Recommend:* no change in this work; afterwards consider one checkbox on a
   connection, "anyone who can read may use it here", instead of three lines.
5. **A default profile for a new connected site** (`Read`, `Skill`, `ToolSearch`, `who_is_asking`,
   `current_page`, plus the gateway): is that the right floor? *Recommend:* yes; it is sensitive-safe, and
   customising is for adding Drive or `Write`, as ACE `connect` does.
6. **Is `canopy-web` a "connected site" or built in?** ACE and Trellis connect the canopy app's own
   widget as a site named `canopy-web`. *Recommend:* show it as a normal row labelled "canopy (this app)";
   revisit only if a third agent wants it.
7. **`declared_by/at` meaning.** Today it is whoever last saved *anything* on the interface, so an
   unrelated edit re-dates the sensitive declaration to hosts. After this it changes only when the switch
   is flipped. Day-one values are copied, so nothing changes yet. *Recommend:* accept; tell the Connect
   Labs owners.
8. **Where does this live in Settings?** *Recommend:* the existing agent Settings tab renamed "Access",
   holding Who can use it, Read mode, Connected sites, Sensitive data in that order.
9. **Alias window.** *Recommend:* use-based (30 days with zero calls) rather than a date, and update
   Trellis's tool list first.
10. **Timing.** This touches the decision path Phase 1 just changed and that is not yet deployed.
    *Recommend:* start at P0 only after Phase 1 is deployed and `read_mode_gate` has been re-run; P0 is
    safe to write earlier.
