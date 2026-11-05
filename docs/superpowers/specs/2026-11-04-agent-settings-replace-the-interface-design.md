# Agent settings replace the interface: read mode and connected sites

**Status:** P0 is built and deployed. P1 and P2 are being built. P3 waits on 30 days with no calls to the old
routes. Follows `2026-11-03-agent-read-mode-and-doors-design.md`, whose Phase 1 is deployed.

Checked against production on `221d920be`, read-only: 21 agents and 3 sites, every list in `shape` empty, and
`read_mode_gate` found nothing. The snapshot's hash is
`aec39912a35ad8750a3067ce56e8ee28f40ae60ea4e11441069f01fcf96b788f`. An empty `shape` is what says every agent,
including the three section 4 does not describe, fits the plain forms.

The design decisions here were made by the implementer under the owner's delegation and have not been
personally reviewed by the owner (section 9).

Section 4's per-agent detail comes from a read through the canopy API on 2026-11-04 by an earlier draft of this
spec, and `tests/fixtures/answering_interfaces.json` is reconstructed from it, not captured. Neither is relied
on for what production holds: the new routes refuse any document they cannot hold.

## 1. In plain words

Today every agent has an "interface": a block of YAML nobody on the team was comfortable editing. Who may
ask an agent for what moved to the **access list** long ago. What the YAML still holds is two things, and an
admin has to understand "capability", "door", "ask", "entry", "ceiling" and "own_login" to touch either.

After this change an agent has **two settings beside its access list**, each a plain form:

| What the admin sees | What it means | Stored as, unchanged |
|---|---|---|
| **Read mode** | What someone who may only read gets. Every agent has one. Leave it on canopy's default, or press **Customise** for this agent's own list of what it may do while answering. **Go back to the default** undoes it. | the `ask` entry of `Agent.interface` |
| **Connected sites** | "This agent can be reached from Connect Labs." One row per site: which of the site's tools it may use (default: whatever the site allows), its own login on that site (kept out of the conversation), what it may do while answering there, and a switch, **Handles sensitive data**. | every other entry of `Agent.interface` |

**Handles sensitive data** means: a conversation on this site runs only in this row's confined profile, for
everyone including the owner, and a host's sensitive data reaches the agent only through such a row, on a
box set aside for it. It says nothing about the same agent's other conversations: its owner in an ordinary
chat still has the whole agent, today and after this (`tests/test_answering_snapshot.py` pins it).

Words that leave the product (UI, docs, new routes and tools, CLI): *interface*, *capability* (in the agent
sense only), *door*, *ask*, *entry*, *ceiling*, *own_login*, *pages*. They stay where they are wire or
storage (section 5). The word leaves the product, not the database.

## 2. What the code does today

`Agent.interface` is one JSON document, `{capabilities: {<name>: {description, entry, tools, bash,
read_paths, write_paths, pages, sites, ceiling, own_login, data}}}`, validated by `interface.parse`. Exactly
one function writes it: `apps/agents/api.py::_save_interface`, behind `publish_interface`,
`set_capability_sensitive` and `reset_interface`.

A name means one of four things, by what the entry holds:

* `ask` or `read`: read mode's override (`interface.read_mode_override`, `interface_default.is_read_mode`).
  None means canopy's `default_read_profile`, stamped `read`.
* an entry with **`sites`**: selected when the conversation's `embed_app` is in it
  (`interface._site_capability`) and the asker's permissions open it (`roles.can_use`). `interface.profile`
  appends canopy's gateway and page tools.
* an entry with **`pages`**: selected by what a page declares (`interface._page_capability`). Superseded by
  `sites`; no live agent uses it.
* an entry with **`data: sensitive`**: must pass `sensitive_data.structural_refusals`. `access._sensitive_rule`
  confines everyone in it, and refuses a sensitive conversation anywhere else. It is per entry, and that is
  what the wire states: `declaration.capability` in `Canopy-Execution`, `capabilities[].handles_sensitive`
  in the site-agents answer.

Two rules a reader might miss, both from the read-mode hardening: naming `read` cannot dodge a sensitive
`ask` (`access._sensitive_capability`), and a sensitive turn stamped with a read-mode name whose capability
has gone gets the deny-all profile, not the default (`caller_context._profile`).

Who reads the document (each stays exactly as it is, because the document does): `access.py`,
`interface.py`, `sensitive_data.py`, `apps/agents/{agent_card,topology,health,holding,read_mode_gate,
api_access_list}.py`, `apps/host_grants/{host_gateway,site_agents}.py`, `apps/host_grants/execution.py` (via
`as_claim`), `apps/mcp_apps_host/mcp_apps_views.py::_ceiling`, `apps/contacts/contact_api.py`,
`apps/harness/{caller_context,caller_tokens}.py`, `apps/harness/schemas/turns.py`, and through
`is_read_mode` `apps/admission/{evaluate,rules}.py`.

**The runner never reads the interface.** It reads the envelope, and uses the capability **name** in two
places: `session_naming.restricted_key` continues the session `<thread>#<name>`, and
`cloud_runner._kept_identity` resumes a kept sensitive session only under the same name. So a stamp's name
is wire: changing `ask` to `read`, or `connect` to anything else, would split every live conversation.

The name also lives in `Turn.capability`, `AdmissionRule.capability`, `AgentGrant.capability`, and, since
roles became configurable, in a custom role's stored permissions (`AgentRole.permissions`, `use:<name>`,
`roles.validate_permissions`).

## 3. The design: one document, one writer, new forms

**Nothing about storage moves.** The new routes and forms edit the same document through the same writer.
No new table, no new column, no data migration, no mirror, and no change to any reader.

The two settings are views of the document:

| Setting | Read | Write |
|---|---|---|
| Read mode | the `ask` (else `read`) entry, else canopy's default: `{source: default\|custom, profile}` | sets the entry, under the name it already has (a new one is `ask`, so that person's next read-mode turn starts a new runner session, as publishing an `ask` always has), or deletes both names |
| Connected sites | each entry naming a site: `{site, key, description, profile, ceiling, own_login, sensitive}` | sets or deletes the entry for that site |

**Why not the separate columns and `SiteConnection` table an earlier draft proposed.** They existed to give the
settings their own storage, and that storage was the whole cost: a two-way mapping (`settings_from_interface`,
an inverse "legacy mirror"), a mirror written in every transaction, every reader across the decision path moved
family by family, a final irreversible column drop, and a 14-day window around it. What it bought was a database
uniqueness constraint and a queryable table, and nothing needs either: every "which agents serve this site"
reader already has the agent in hand, and the one writer can enforce the rule. The decision path, the riskiest
code here, now is not touched at all, and any phase rolls back by redeploying the previous image, because the
previous image reads the same column.

Costs accepted: a blob has no database constraint (the writer enforces the rules; the snapshot's `shape`
reports any row that breaks them), and two people editing different rows at once could lose one (the writer
takes `select_for_update` on the agent). The column keeps its name; no person sees it.

### 3.1 The rules the new routes enforce

Checked in the writer for the new routes only. The old `PUT …/interface` keeps today's parser until it is
retired, so it stays the unchanged way back.

* One site per row, and one row per (agent, site). Live data has none otherwise.
* A read-mode entry holds no `sites`, `ceiling`, `own_login`, `pages` or `data`.
* No `pages`.
* Every entry but read mode names a site.
* Sensitive on: the row must pass `structural_refusals`, as today.

The rules are asked of the whole stored document before the edit, so an edit cannot tidy away what it cannot hold,
and a document that fits still fits after any edit these routes can make. An existing document that breaks one keeps working as it does. It is refused only when someone saves it
through a new route, with a message saying why. An edit validates only the entry it changes and leaves the others
as saved. Saving clears `interface_source`, which would otherwise be stale YAML that the old sensitive switch
edits and saves back.

### 3.2 A new row's key and defaults

`key` is the entry's name, generated once from the site: lower-cased, every other character `_`, because a
name must match `^[a-z][a-z0-9_]{0,40}$`, so `connect-labs` becomes `connect_labs`. It avoids `read`, `ask`, `session_review` (canopy's own, which nobody can publish) and
any name the agent already has (a suffix). It is never renamed. The UI shows it only as a small "internal name",
which an access line can pick.

A new row's form **starts filled in** with `Read`, `Skill`, `ToolSearch`, `who_is_asking`, `current_page`, read
paths `{cwd}/**`, no shell and no writes, and saves those values explicitly. There is no stored "use the
default" state, so no second pinned default, no new envelope field, and a later change to the form's starting
values widens nobody. The values pass `structural_refusals`, so "add a site, switch sensitive on" needs nothing
else.

### 3.3 Vocabulary and routes

| Old | New | Route | MCP tool | CLI |
|---|---|---|---|---|
| `ask` / `read` | Read mode | `GET/PUT/DELETE /api/agents/{slug}/read-mode` | `get_read_mode`, `put_read_mode`, `reset_read_mode` | `canopy agent read-mode get\|set --file\|reset` |
| capability with `sites` | Connected site | `GET …/connected-sites`; `PUT/DELETE …/connected-sites/{site}` | `list_connected_sites`, `put_connected_site`, `delete_connected_site` | `canopy agent sites list\|set\|remove` |
| `data: sensitive` | Handles sensitive data | field `sensitive` on the row | as above | as above |
| `ceiling` | Site tools it may use | field `ceiling` | | |
| `own_login` | Its own login on this site | field `own_login`: a list of names | | |
| `entry` | Starts with | field `entry` | | |
| `use:<name>` line | "Only from <site>" | unchanged | unchanged | unchanged |
| `validate_interface` | `dry_run: true` on each PUT | | | |

Each write route carries the gate `publish_interface` carries today, `@gate(..., setting=True)`, with
`audit.note_change` scopes `agent:<slug>/read-mode` and `agent:<slug>/site/<site>`. `GET /api/agents/{slug}` gains
`read_mode` and `connected_sites`, so Trellis can explain another agent with a tool it already holds.

## 4. Live agents (21 in production; 18 described here, read 2026-11-04)

| Agents | Stored | They see |
|---|---|---|
| ada, budgy, fizzy, gshubdesk, jarvis, lex, muse, neo, otto, sift, bailey | nothing | default read mode, no sites |
| echo, eva, hal, oolong | one `ask` | customised read mode |
| trellis | `ask` + `canopy` (site `canopy-web`, no ceiling) | customised read mode, one site |
| ace | `ask` + `canopy` (`canopy-web`, 4 ceiling globs) + `connect` (`connect-labs`, ceiling, own login `LABS_MCP_TOKEN`) | customised read mode, two sites |
| labsdata | `data` (`connect-labs`, sensitive, ceiling, entry) | default read mode, one sensitive site |

alafia-test, tempo and vera arrived after that read, so their documents are not described here. `hal`'s entry has no `sites`/`ceiling` keys at all (an older save); readers already tolerate that. The read
default is now version 3: labsdata's read mode has no `curl` reads.

## 5. Wire and stored strings that must not change

| Contract | Stays | Pinned by |
|---|---|---|
| `Canopy-Execution` | `agent`, `capability`, `declaration {data, capability, declared_by, declared_at}`, `runner`, `runner_posture`, `requirements`, `turn`, `session` | `tests/test_execution_statement.py`, `sdk/python/tests/test_host_execution.py` |
| Site-agents JWS | `v`, `site`, `runner_flags`, `agents[]`, `capabilities[] {name, description, handles_sensitive, sensitive_set_by, sensitive_set_at}`, `ready`, `problems`, `problem_links`, `runners`. Only the `problems` sentence changes (P2), which moves `version` once: hosts show it, none parses it, and the SDK treats it as text | `tests/test_site_agents.py`, `sdk/python/tests/test_site_agents.py` |
| Caller envelope | `profile`, `capability {...}`, `permissions`, `granted_by` values (`owner`, `admin`, `system`, `session:<role>`, `grant:<line>`, `capability:<name>`, `refused`, `no-interface`) | `tests/test_caller_envelope.py`, `tests/test_default_read_profile.py`, `tests/test_read_mode_alias.py` |
| Claim to the runner | `page_site {site, withheld}`, `caller_context`, `mcp_token` | `tests/test_site_gateway.py`, runner tests |
| Stored strings | `Turn.capability`, `AdmissionRule.capability`, `AgentGrant.capability`, `AgentRole.permissions` | `tests/test_admission.py`, `tests/test_access_matrix.py` |
| A2A card | skill `id` = the entry's name | `tests/test_a2a_agent_card.py` |
| Topology | basis strings, including `no-interface` | the snapshot (`topology`); nothing else pins `no-interface` here |

`declared_by/at` keep meaning "who last saved the document": no host checks them (`sensitive_clearance` reads only
`declaration.data`), so re-dating on an unrelated save is cosmetic, and fixing it would be the one reason to add a
column.

## 6. Checks

* **`tests/legacy_interface.py`**: a frozen copy of how the document answers (`profile`, `decide`'s
  interface-reading parts, the site helpers, `as_claim`, `facts_for`, `agent_entry`). Since the document stays, it
  stays valid in every phase.
* **`tests/test_answering_settings_equivalence.py`**: the real code equals the frozen copy over 18 fixture agents and
  9 corner cases (pages, both read names, two sites on a row, two rows on a site, a mixed agent, a sensitive `ask`),
  over every stamp, and `decide` over {owner, admin, editor, read_only, unlisted, a contact holding each
  `use:<key>`} x {no turn, chat, sensitive chat, each site, each sensitive site, a site nobody serves, a page} x
  {free-form, `read`, `ask`, each key, a retired name}. A digest of the frozen copy's answers catches both being edited
  together.
* **`manage.py answering_snapshot [--json]`**: per agent `profile()` per stamp, the `decide()` matrix, its lines and
  roster, `facts_for` when sensitive, A2A public and extended cards; site-agents content per registered site without
  what a heartbeat moves (`ready`, `ready_with`, problems that link `#running`); topology edges; and `shape`, every
  stored fact sections 3.1 and 5 assume, checked rather than assumed. It reads only: a read-only transaction on
  PostgreSQL, a wrapper refusing any non-read statement, and a rollback. Personas in its matrix live in memory. It
  refuses to print anything shaped like a token, and two runs on the same data print the same bytes.

## 7. Phases

Each is one PR. Every phase rolls back by redeploying the previous image.

| # | PR | Risk | Verify |
|---|---|---|---|
| P0 | Characterization (section 6). No behaviour change. | Low | Done, deployed and run read-only against production. |
| P1 | New routes and MCP tools (3.3), the rules (3.1), `dry_run`, `select_for_update` in the writer, the two `GET /api/agents/{slug}` fields, `generated.ts`, the plugin's new CLI commands. Old routes stay as they are, marked `deprecated=True`. | Medium: a published surface and writes | Before: `read_mode_gate` and `shape`, both clean on `221d920be`. After: the snapshot's hash is unchanged across the deploy. Round-trip test: reading a setting and PUTting it back changes no answer and leaves every other entry as saved, for every fixture agent that satisfies 3.1. It is parse-equal, not byte-equal: the entry saved is normalised by `parse`, as any save is. |
| P2 | Frontend: on the Channels tab, "Read mode and doors" becomes Read mode and Connected sites cards, replacing `AgentInterfaceView`/`InterfaceEditor`. Line form, role form and admission-rule wording (door to connected site). The site-agents `problems` sentence. Docs, CLAUDE.md, agent repos' text. | Low | Render check of ACE, labsdata, Trellis and a default agent. Snapshot diff is only that sentence. |
| P3 | Retire. Once `list_mcp_calls` shows no call to the five old tool names for 30 days and Trellis's tool lists name `get_agent` (edited through the new route, then its repo test): old routes return 410 with a pointer; delete YAML parsing and `with_sensitive`. If `shape` is empty, delete `pages` and the site-ceiling union, and move those corner cases out of the frozen copy. Optional and alone: drop `interface_source`. | Medium for the deletion; the column drop is the only irreversible step | Equivalence test green; snapshot hash unchanged. |

## 8. What not to do

1. **Do not rename any wire field or stored string in section 5.** Hosts and the plugin would break with no way to roll the host side back.
2. **Do not unify the `ask` and `read` stamps.** It starts every live conversation with ACE, Trellis, eva, echo, hal and oolong over. `is_read_mode` already makes them one wherever it matters.
3. **Do not move the storage, now or "later for tidiness".** Reopen it only when something needs a query or constraint the blob cannot give.
4. **Do not rename the `Agent.interface` column or `apps/agents/interface.py`.** No person sees them; the churn touches the decision path for nothing.
5. **Do not change the default read profile or `READ_PROFILE_VERSION` in this work.** It makes every diff ambiguous.
6. **Do not fix Trellis's `gh api` patterns as part of anything here.** A normal audited edit, afterwards.
7. **Do not add presets or a profile library.** The default is the preset.
8. **Do not touch the other meanings of capability**: `Runner.capabilities`, cloud capabilities, `Storyboard.capability`, workspace permissions.

## 9. Decisions

Decided by the implementer under the owner's delegation; not personally reviewed by the owner.

1. **Sensitive data is a switch on a connected site, not an agent-level setting.** The code, the wire and the existing switch are all per entry, and an agent-level switch would have described something the code does not do (section 1).
2. **One row per (agent, site)**, enforced on new writes. The union rule goes in P3 if `shape` allows.
3. **Internal names stay wire forever**, shown small, never renamed.
4. **A `read_only` person does not reach a connected site's profile without a `use:<key>` line**: no change. A later checkbox could replace ACE's lines; it is not needed for this.
5. **A new row starts with a fixed, saved list of tools** (3.2), not a stored default.
6. **`canopy-web` is a normal row**, labelled "canopy (this app)".
7. **`declared_by/at` keep their meaning** (section 5).
8. **The cards stay on the Channels tab, and the access list stays on the Roles tab.** Channels also holds Slack, email and Google, so it cannot be renamed "Access", and moving the list buys nothing.
9. **The alias window is use-based** (30 days with no calls), and needs no translators, because old and new routes write the same document.
10. **Timing:** Phase 1 is deployed. P1 waits only for the gate re-run and `shape` (section 7).
