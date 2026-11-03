# Read mode and doors: simplifying what an agent's "interface" is for

**Status:** proposed 2026-11-03, for Jonathan's review. Builds on
`2026-11-02-agent-permissions-and-roles-design.md` and `docs/architecture/access.md` (the access
list, roles and `decide` stay exactly as they are). Written from an audit of the decision path
(`apps/agents/access.py`, `roles.py`, `interface.py`), the enforcement layers (caller tokens, the
runner's profile and native deny rules, the `profile_guard` hook) and the screens, API and live
agents. Findings are cited as file names; the audit did not read the 13k-line cloud runner end to
end.

## Why

The access list now says, clearly, who may do what with an agent: `read`, `write`, `agent_edit`,
`admin`, `own`. The agent's **interface** was built for a different job: to say *who* may reach
*which* capability. That job moved to the access list, and what is left of the interface is four
unrelated things in one YAML card:

1. a **confinement profile**: the allowlist of tools, exact bash commands and paths a confined
   session may use (the part canopy enforces in three layers, and the part that is worth keeping);
2. an **entry skill**: what a confined session runs first;
3. a **site door**: `sites`, `ceiling`, `own_login`, which let a visitor on a Connected site reach
   the agent as themselves;
4. a **data attestation**: `data: sensitive`, which forces confinement for everyone and is signed
   to host sites.

Because nothing publishes a profile by default, `read` does nothing until someone hand-writes an
`ask` capability. Live on 2026-11-03 there are 17 agents across 7 workspaces; six published an `ask`
(ACE, eva, hal, echo, Trellis, oolong), the other eleven offer `read` nothing. Four of the six are
close to the same profile with the agent's own slug in it (ACE, eva, hal, echo; Trellis and oolong
differ). The screen asks an admin to author a tool allowlist to turn on the commonest case.

A hand-written `ask` is also not maintained, and it shows on real work: the Trellis rollout audit found
it denying code search, `gh api --jq` patterns that miss, and Google docs reads. A default that is
canopy-web's to keep must therefore be *useful*, not minimal (see "Read mode").

Scope for now: **internal Dimagi use**, where most people hold `write` and agents are meant to dive
into code and propose fixes. How an agent should answer people *outside* Dimagi, and whether that
needs different behaviour at all, is deliberately not designed here (see "Not decided").

## The model

An agent has a **permission** per person (unchanged) and, for people who hold `read` but not
`write`, one confined **read mode**. Everything else is a **door**, and doors are rare.

| Word | Means | Set by |
|---|---|---|
| **read mode** | What a person with `read` and no `write` gets: the agent answers them, confined. One per agent. It exists on every agent from the start, with no setup. | platform default; an agent may override it |
| **door** | A further confined profile for a specific place (a Connected site, a page) or a specific list of people. Rare. | owner or agent admin |

`ask` stops being a name anyone sees or types. The word "capability" leaves the screens: it has five
meanings in the code (`interface.py`'s own docstring lists them) and the Settings "Capabilities" tab
shares a name with the cloud-machine "Capabilities" left-nav page.

### Read mode

* **`read` always resolves.** `decide`'s row 4 stops depending on a published capability: a person
  whose entries give them `read` gets read mode. Nothing is "off until published".
* **The default profile is defined in canopy-web code**, built from the agent, and shown on the
  agent's settings page so an admin can see exactly what it is:
  * tools: `Read`, `Grep`, `Glob`, `who_is_asking`, and `Write` to one scratch folder
    `{cwd}/.<slug>-read/*` (it drafts the reply there);
  * paths: read `{cwd}/**`; write only the scratch folder;
  * bash: only the email-reply rules (read the thread, the review receipt, and the agent's own
    `bin/<slug>-email --reply-all --thread-id {thread_id}`), and only when the agent has a mailbox;
  * entry: see "Default entry" below.
* **Default entry.** canopy-web knows an agent's published skills (`AgentSkill`, the catalog an agent
  PUTs, `agents.services.list_skills`; names are stored bare, without the plugin prefix). The default
  entry is `/<slug>:answer-caller --thread {thread_id}` exactly when the catalog has a skill named
  `answer-caller`. When it has none, the default has **no entry**: the session starts with the caller's
  message and no command, and the agent answers from its persona and `CLAUDE.md` within the read
  profile. It never falls back to another skill name (`ask`, `policy-qa`) by guessing; an agent that
  enters through a different skill overrides. The entry is decided at claim time from the catalog as
  it stands, so publishing or removing `answer-caller` changes the next turn, not a stored one. (ACE
  and Trellis both ship `skills/answer-caller`; eva, hal, echo and oolong do not.)
* **What "read" really lets an agent do** should be said plainly: the confined session cannot change
  anything except draft and send a reply on the thread it was asked on. Today's docs call it
  "read-only", which is not quite true for email.
* **The default profile must be useful.** Beyond the fields above, the default grants read-only lookup
  of the agent's own repo and docs (code search included, which the hand-written `ask` of the audited
  agents denied) and narrowly patterned `gh api` reads (a fixed list of read endpoints, each pattern
  written to match with and without `--jq`), and nothing that writes outside the scratch folder.
  Google docs reads are not in the default: they carry the agent's own Google login, so an agent that
  wants them overrides. The exact list is part of the pinned test (Phase 1, change 9).
* **Read mode is the team door; `write` is for people who may drive or change the agent.** Read mode
  answers support questions and coaches a person about themselves ("coach me"). It is not a thin
  version of working with the agent. A full-profile turn carries the agent's own login: Trellis's
  login is an editor in 7 workspaces and holds a delegated GitHub token, so granting `write`
  org-wide hands those powers to everyone who holds it. The full profile for the whole team is not the
  default posture (see "Rollout posture").
* **An agent may override it** with its own profile (Trellis adds its sessions CLI and a few canopy
  tools; eva, hal and echo add web search). An override is the same fields as today and
  replaces the default whole; there is no merging.
* **Stamp: the capability name actually run.** A turn on the default is stamped `read`; a turn on an
  agent's own published `ask` is stamped `ask`. Stored stamps are never rewritten. Both names mean read
  mode, and **one function** (`interface.is_read_mode(name)`, true for `read` and `ask`) is the only
  place that says so; the decision, `interface.opens`, `interface.profile`, session continuation and
  admission matching all call it. The enforcement layers never read the name (runner, `profile_guard`,
  native deny rules, caller token), so this is a decision-time change only.
* **Admission rules.** `AdmissionRule.capability` (a free-text name, `apps/admission/models.py`)
  is compared with `have != want` against `turn.capability` (`evaluate.facts`/`match`). A rule
  naming `ask` must keep matching turns stamped `read`, and a rule naming `read` must match turns
  stamped `ask`. Decision: the alias, not a data migration. `match` compares the `capability` condition
  through `is_read_mode` (both sides read mode means equal), `rules.py` summaries say "read mode" for
  either name, and the rule form offers `read` while still accepting `ask`. Stored rules are never
  rewritten, so a rule edited back to `ask` still works. The seeded rules' wording is already
  corrected.
* **Caller envelope** (`apps/harness/caller_context.py`; `permissions` and `manual_only` are the
  fields the access-list change added). For a confined read-mode turn `permissions` lists `read` and
  nothing named after the door: today `_held_permissions` adds `use:<stamp>` for any stamped capability,
  which would yield `use:read` (and yields `use:ask` for an override). Read mode is `read`, not a
  door, so for either stamp it adds nothing; a real door still adds `use:<door>`. `capability.name`
  and the stamp carry the name actually run (`read` or `ask`). The envelope contract test
  gains both cases. `granted_by`/`source` keep `capability:<name>` with the stamped name.

### Doors

A door is today's capability, minus the pieces that moved: it keeps a profile, an optional entry
skill, and, for a Connected site, `sites`, `ceiling` and `own_login`. A person is let through a door
by an access entry that names it (`use:<door>`), as now. `write` opens every door, as now. A line `use:ask` is a read-mode line under the alias, and the
migration below turns it into `read_only`.
Phase 2 below moves site connections and the sensitive-data setting out of the door card.

## What does not change

* `decide`'s order and rows 1, 2, 3 and 5, the sensitive-data override, `manual_only`, turn mode.
* The live-state property: the profile is computed by canopy-web at claim time and delivered in the
  envelope; the agent's repo cannot widen it.
* The three enforcement layers and the caller token: they read only `tools`, `bash`, `read_paths`,
  `write_paths` and `entry`, never the name.
* Site gateway behaviour, the signed `Canopy-Execution` statement and the SDK's `capabilities[]`
  listing: hosts verify them, so their shapes are kept and fed from the new settings.

## Rollout posture

How access is set once Phase 1 lands, per agent:

* **The team is `read_only`** (a workspace or domain default line): they get read mode.
* **Admins (and the people an owner names) are full**: `write` or the agent admin role.
* No agent is granted `write` to a whole workspace or domain as a way to make it useful. If an agent's
  read mode is not enough for the team, the answer is to improve its read profile or add a door.

Two server rules this posture depends on, each its own change, and each a precondition for putting a
whole team on `write` anywhere:

1. **Session reads by an agent turn bind to the asker** (a separate PR is in flight): an agent turn that
   reads sessions can read only what its asker may read.
2. **A full-profile asker cannot read the agent's own secrets** (a rail): holding `write` is not a
   way to ask the agent to read out its credentials or vault.

Neither is Phase 1 code; both are named so that Phase 1 does not become the reason someone widens
`write`.

## Phase 1 (this design's commitment): read mode replaces `ask`

### Gate, at deploy time

**List every agent that has a `read_only` or `use:ask` entry (agent, workspace or domain line) but no
published `ask`.** Today such entries are inert; after this change they go live, so this is the one way
the change can widen access and it is a gate, not a note. State of the check on 2026-11-03: all 17
live agents' access lists were read. Agents with `read_only` entries: ACE, labsdata, echo, eva, hal,
oolong (ACE also has a `use:ask` entry); Trellis had one `read_write` entry for everyone in the
dimagi workspace, now `read_only`. Of these, only labsdata had no published `ask` (its one door is the
sensitive `data` capability); its two `read_only` lines (ids 11 and 12) were removed on 2026-11-03.
**The result today is: no agent.** Access lists keep changing, so the check is re-run immediately
before deploying change 3 of the order below, and that deploy waits on a result of none (or on each
hit's owner decision: keep, remove, or leave the agent without the entry).

### Order

1. **Pin the default profile** in a test (change 9) and write the default-entry rule. Nothing reads it
   yet.
2. **Alias first.** Add `is_read_mode` and make admission matching, `interface.opens`/`profile`, the
   envelope and session continuation use it, still resolving `read` only through a published `ask`.
   Deploy this alone: it changes no behaviour and makes every later step safe to roll back.
3. **Resolve `read` without a published `ask`** (changes 1 and 2), behind the gate above. This is the
   step that changes who is let in; it is the last behaviour change to deploy, after a fresh gate run.
4. **Migration** (change 3), then **screens, OpenAPI and docs** (changes 5, 6 and 8).
5. Remove dead fields (change 7) only after one deploy has run.

### Changes

1. `roles.can_use` and `access._capability_for` resolve `read` to read mode; the free-form fallback
   no longer needs a published `ask`. The name it falls back to is the agent's published `ask` when
   it has one (stamp `ask`), else the default (stamp `read`). The duplicate constants
   (`roles.READ_CAPABILITY`, `interface.ASK`) become one.
2. `interface.profile` builds the default for read mode when the agent has no override. An unknown
   or removed *other* name still yields the empty deny-all profile, so a turn stamped with a retired
   door fails closed.
3. **Migration.** Every published `ask` stays the agent's read-mode override unchanged (it keeps its
   name), so on day one no live agent behaves differently. A `use:ask` access entry becomes
   `read_only` (ACE has one). Each agent's old interface goes to the settings audit, as the earlier
   migrations did. Admission rules need no migration (the alias).
4. The gate above.
5. Screens: the Settings tab becomes "Read mode and doors". It shows the default read profile in
   plain words, an "Override" action, and a short list of doors. The "Add an ask capability" button
   goes. The access-entry form's free-text capability name becomes a picker of existing doors.
6. **Roles text, OpenAPI and generated types.** `roles[].can` comes from `_CAN` in
   `apps/agents/api_access_list.py`; the `read` string ("ask, in a confined session, through the
   agent's ask capability (nothing, until the agent publishes one)") is rewritten to say read mode
   answers you in a confined session and exists on every agent. The same wording is in
   `docs/architecture/access.md`, and route docstrings are published as OpenAPI descriptions.
   Regenerate `frontend/src/api/generated.ts` (`npm run gen:api`) and commit it, or the required
   `Backend tests` check fails on a stale one.
7. Remove what nothing uses: `input` (validated and displayed, no reader), `callers_default` and
   `version` (accepted, ignored), the unused helpers `interface.capability_for` and `granted_by`.
   `pages:` is deprecated but live in three places; it stays until its remaining users are moved,
   and is called out in the UI as legacy.
8. The A2A agent card, roster and topology views stop reading "no capabilities" as "nothing offered".
   The public card must not start listing read mode for every agent that has a contact or domain
   `read_only` entry unless that is chosen deliberately.
9. A test pins the default profile exactly (tools, paths, bash, `gh api` patterns, entry rule), so a
   code change that widens every agent at once cannot pass unnoticed, and the envelope carries a
   default-profile version so a turn records which one it ran under.

### Verify

Before deploying change 3:

* the gate query returns no agent, or each hit has its owner's decision;
* the pinned default-profile test passes and a person reviewed any diff to it;
* `agent_roster`, `agent_topology` and the A2A card for an agent with no `ask` are unchanged;
* admission rules: a rule naming `ask` fires on a turn stamped `read`, and one naming `read` fires on a
  turn stamped `ask` (unit test on `evaluate.match`);
* the envelope contract test (`tests/test_caller_envelope.py`, the `permissions` / `manual_only`
  cases) has read-mode cases for both stamps with `permissions == ["read"]` in each.

After deploying:

* `/api/openapi.json` carries the new `roles[].can` text and `generated.ts` matches it;
* the gate query, run again, still returns none;
* end to end on a real agent, one asker per posture (the Sophie and ACE flow): a `read_only` asker is
  answered in a confined session, cannot write outside the scratch folder, and the turn's envelope
  shows `read` and the intended profile; an admin on the same agent still gets the full profile;
* a published-`ask` agent (Trellis) and an agent with no `ask` each answer a read-mode question.

## Phase 2 (direction, to be designed with the compatibility constraints)

* **Site connections** become an agent setting ("this agent may be reached from connect-labs,
  with this ceiling and this login"), separate from any profile. The compatibility constraint is the
  site gateway: it refuses a confined turn whose capability does not list the site
  (`host_gateway.py`), and `page_sites`, `site_ceiling`, `own_login_of` and the site-agents listing
  all derive "set up for a site" from the capability. All of them move together.
* **Sensitive data** becomes an agent-level setting with its own history. The structural rule that a
  sensitive profile has no bash, no writes and read-only tools stays, which means a sensitive
  conversation cannot use the email-reply read mode. It needs its own profile (the gateway one), and
  that choice has to be made explicitly. The signed `Canopy-Execution.declaration` keeps its shape and
  `declared_by`/`declared_at` come from the setting rather than from "who last saved the interface".
* The word "interface" then leaves the product (the MCP tools `get_interface`, `publish_interface`,
  `validate_interface`, `set_capability_sensitive`, `reset_interface` are renamed with a deprecation
  window, because Trellis lists `get_interface` in its own tool allowlist). The A2A agent card's use of
  "interface" is a separate sense and is untouched.

## Not decided

* **How agents should behave for people outside Dimagi.** Internally the default is to read code and
  propose fixes; externally an agent would never do that. Whether that needs different agents, a
  different profile, or is better avoided (more modes may be an anti-pattern) is open. Read mode here
  is deliberately one mode, so this question cannot multiply it.
* Whether read mode keeps web search by default (eva, hal and echo have it; ACE does not).
* Whether the caller-skill convention (`answer-caller`) should be required. Decided for Phase 1:
  used when the agent's catalog has it, otherwise no entry (see "Default entry"). ACE and Trellis have
  the folder; hal, eva, echo and oolong enter through `/<slug>:ask` or `policy-qa` and would be
  overrides until renamed.

## Doc drift found while auditing (separate fix)

`access.md` says `Decision` has a `role` field (the code has `relationship`, `agent_role`,
`permissions`, `basis`); its turn-mode ladder omits the trigger posture and the manual-session rung;
"Where it is asked" omits several callers of `decide`; `turn_mode.py` still says a workspace editor's
turns are always manual; `api-surface.md` says the sensitive switch is under "Settings → Interface";
and the `canopy agent interface` CLI help still describes the removed `full:` model.
