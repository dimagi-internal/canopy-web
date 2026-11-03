# Read mode and doors: simplifying what an agent's "interface" is for

**Status:** Phase 1 is built and merged (the six PRs below); nothing in it is deployed yet. Phase 2 is
direction only. Builds on `2026-11-02-agent-permissions-and-roles-design.md` and
`docs/architecture/access.md` (the access list, roles and `decide` stay exactly as they are). Written
from an audit of the decision path (`apps/agents/access.py`, `roles.py`, `interface.py`), the
enforcement layers (caller tokens, the runner's profile and native deny rules, the `profile_guard`
hook) and the screens, API and live agents. The audit did not read the 13k-line cloud runner end to
end.

| Phase 1 step | PR |
|---|---|
| Pin the default profile (`interface.default_read_profile`) | #2460 |
| `interface.is_read_mode` alias (admission, `roles.can_use`/`opens`, `profile`, the decision's capability lookup) | #2463 |
| `read` resolves to read mode on every agent; envelope `permissions == ["read"]`; `manage.py read_mode_gate` | #2465 |
| Migration `agents/0119`: `use:ask` / `use:read` lines become `read_only` | #2467 |
| Screens ("Read mode and doors"), OpenAPI, generated types, docs | #2469 |
| Dead interface fields removed (tolerantly) | #2471 |

The same rollout depends on two server rules that landed separately (see "Rollout posture"): an agent
turn's session reads bind to its verified asker (#2457) and an agent's own login can re-read its own
running turn's caller envelope (#2462).

## Why

The access list says, clearly, who may do what with an agent: `read`, `write`, `agent_edit`, `admin`,
`own`. The agent's **interface** was built for a different job: to say *who* may reach *which*
capability. That job moved to the access list, and what is left of the interface is four unrelated
things in one YAML card:

1. a **confinement profile**: the allowlist of tools, exact bash commands and paths a confined
   session may use (the part canopy enforces in three layers, and the part that is worth keeping);
2. an **entry skill**: what a confined session runs first;
3. a **site door**: `sites`, `ceiling`, `own_login`, which let a visitor on a Connected site reach
   the agent as themselves;
4. a **data attestation**: `data: sensitive`, which forces confinement for everyone and is signed
   to host sites.

Before read mode nothing published a profile by default, so `read` did nothing until someone
hand-wrote an `ask` capability. Of the 18 agents live on 2026-11-03 (17 in `dimagi`, `bailey` in
`marshellis`), six had published an `ask` (ACE, eva, hal, echo, Trellis, oolong); the other twelve
offered `read` nothing. A hand-written `ask` is also not maintained: the Trellis rollout audit found
it denying code search, `gh api --jq` patterns that miss, and Google docs reads. A default that is
canopy-web's to keep must therefore be *useful*, not minimal.

Scope: **internal Dimagi use**, where most people hold `write` and agents are meant to dive into code
and propose fixes. How an agent should answer people *outside* Dimagi is deliberately not designed here
(see "Not decided").

## The model

An agent has a **permission** per person (unchanged) and, for people who hold `read` but not `write`,
one confined **read mode**. Everything else is a **door**, and doors are rare.

| Word | Means | Set by |
|---|---|---|
| **read mode** | What a person with `read` and no `write` gets: the agent answers them, confined. One per agent. It exists on every agent from the start, with no setup. | platform default; an agent may override it |
| **door** | A further confined profile for a specific place (a Connected site, a page) or a specific list of people. Rare. | owner or agent admin |

`ask` is no longer a name anyone has to type: the access-line form picks a door from the agent's
`doors` list, the Settings tab says "Read mode and doors", and `ask` survives only as the name of an
agent's own override and of turns stamped before read mode existed. The word "capability" has left
the screens (it has five meanings in the code; `interface.py`'s docstring lists them).

### Read mode

* **`read` always resolves.** `access._capability_for` and `roles.can_use` give a person whose lines
  give `read` (and not `write`) read mode, or whose line is `use:ask` / `use:read`, with no published
  capability. A sensitive conversation never reaches read mode (the sensitive-data rule refuses it).
* **The default profile is defined in canopy-web code** (`apps/agents/interface_default.py`,
  `default_read_profile(agent)`), a pure function of the agent, pinned exactly by
  `tests/test_default_read_profile.py`, and shown on the agent's Settings → Channels tab:
  * tools: `Read`, `who_is_asking`, and `Write` to one scratch folder `{cwd}/.<slug>-read/*` (it
    drafts the reply there). **No `Grep` or `Glob`**: the cloud runner's Claude Code build has neither
    (a live probe answered "No such tool available"), and a listed tool that does not exist only sends
    the model reaching for it. File search in a confined session needs an explicit bash pattern, which
    the default does not grant; an override may;
  * paths: read `{cwd}/**`; write only the scratch folder;
  * bash: when the agent has a mailbox, the email-reply rules (read the thread, the review receipt,
    and the agent's own `bin/<slug>-email --reply-all --thread-id {thread_id}`); when its
    `repo_url` is on GitHub, four **unauthenticated `curl`** read patterns against its own repo
    (`api.github.com/repos/<o>/<r>/contents/*` with and without the raw `Accept` header,
    `…/git/trees/<ref>?recursive=1`, and `raw.githubusercontent.com/<o>/<r>/<ref>/*`). Never `gh`:
    `gh` is not logged in inside a confined session, and no other host is ever listed;
  * entry: `/<slug>:answer-caller --thread {thread_id}` exactly when the agent's published catalog
    (`AgentSkill`, `agents.services.list_skills`; names stored bare) has a skill named
    `answer-caller`; otherwise **no entry**: the session starts with the caller's message and no
    command, and the agent answers from its persona and `CLAUDE.md` within the profile. It never falls
    back to another skill name (`ask`, `policy-qa`) by guessing. The entry is decided at claim time
    from the catalog as it stands. (ACE and Trellis ship `skills/answer-caller`; eva, hal, echo and
    oolong do not.)
  * `READ_PROFILE_VERSION` is bumped with any change to the profile; the envelope's `capability`
    carries it as `default_profile_version`, so a turn records which default it ran under.
* **The profile is useful, not minimal**, within one rule: nothing writes outside the scratch folder,
  and nothing that carries the agent's own login is in it. Google docs reads are not in the default
  (they use the agent's Google login), so an agent that wants them overrides.
* **Read mode is the team door; `write` is for people who may drive or change the agent.** Read mode
  answers support questions and coaches a person about themselves ("coach me"). It is not a thin
  version of working with the agent. A full-profile turn carries the agent's own login: Trellis's
  login is an editor in 7 workspaces and holds a delegated GitHub token, so granting `write`
  org-wide hands those powers to everyone who holds it. In the envelope, `profile == "full"` exactly
  when `write` is among `permissions`.
* **What read mode really lets an agent do**: the confined session cannot change anything except draft
  and send a reply on the thread it was asked on. The docs no longer call it "read-only".
* **An agent may override it** by publishing an `ask` capability (Trellis adds its sessions CLI and a
  few canopy tools; eva, hal and echo add web search). An override is the same fields as any
  capability and replaces the default whole; there is no merging. Settings → "Override" opens the
  editor on a copy of the default as an `ask`; "Go back to the default read mode" removes it.
* **Stamp: the capability name actually run.** A turn on the default is stamped `read`; a turn on an
  agent's published `ask` is stamped `ask`. Stored stamps are never rewritten. **One function**,
  `interface.is_read_mode(name)` (true for `read` and `ask`, defined in `interface_default.py`), says
  the two are the same; admission matching, `roles.can_use` (so `interface.opens`, the roster, the
  topology and the A2A views), `interface.profile` and the decision's capability lookup call it. The
  enforcement layers never read the name (runner, `profile_guard`, native deny rules, caller token),
  so this is a decision-time alias only. No code in canopy-web compares capability names for session
  continuation, so there was nothing to change there. An unknown or removed *other* name still yields
  the empty deny-all profile, so a turn stamped with a retired door fails closed.
* **Admission rules.** `AdmissionRule.capability` is compared with `turn.capability`
  (`evaluate.facts`/`match`). `match` compares through `is_read_mode`: a rule naming `ask` fires on a
  turn stamped `read` and the reverse (`tests/test_read_mode_alias.py`). `rules.summary` says "read
  mode" for either name; the rule form offers `read` and still accepts `ask`. Stored rules are never
  rewritten.
* **Caller envelope** (`apps/harness/caller_context.py`). For a confined read-mode turn `permissions`
  is exactly `["read"]` under either stamp: read mode adds no `use:<name>`. A real door still adds
  `use:<door>`. `capability.name` and the stamp carry the name actually run. The contract test
  (`tests/test_caller_envelope.py`) covers both stamps, and `profile == "full"` iff `write` is in
  `permissions`, across lines that include `use:read`.
* **Views.** The roster's `capabilities`, the topology edges and the signed-in (extended) A2A card
  list `read` for a confined read-mode person (`interface.reachable`). The PUBLIC A2A card is
  unchanged: it lists published capabilities only, so a contact or domain `read_only` entry does not
  advertise read mode. Health's `has_interface` keeps meaning "published an override".

### Doors

A door is today's capability, minus the pieces that moved: it keeps a profile, an optional entry
skill, and, for a Connected site, `sites`, `ceiling` and `own_login`. A person is let through a door by
an access entry that names it (`use:<door>`), as now. `write` opens every door, as now. A `use:ask` or
`use:read` line is a read-mode line, and migration `agents/0119` turned the stored ones into
`read_only`. The access-line form takes the door from the agent's `doors` (`GET …/access-list`); a
workspace's default lines, which span agents, still take a typed name. `pages:` is deprecated but live
in three places; it stays until its users are moved. Phase 2 moves site connections and the
sensitive-data setting out of the door card.

## What does not change

* `decide`'s order and rows 1, 2, 3 and 5, the sensitive-data override, `manual_only`, turn mode.
* The live-state property: the profile is computed by canopy-web at claim time and delivered in the
  envelope; the agent's repo cannot widen it.
* The three enforcement layers and the caller token: they read only `tools`, `bash`, `read_paths`,
  `write_paths` and `entry`, never the name.
* Site gateway behaviour, the signed `Canopy-Execution` statement and the SDK's `capabilities[]`
  listing: hosts verify them, so their shapes are kept.

## Rollout posture

How access is set once Phase 1 is live, per agent:

* **The team is `read_only`** (a workspace or domain default line): they get read mode. ACE's
  "Everyone in connect" line, which was `use:ask`, is now `read_only`.
* **Admins (and the people an owner names) are full**: `write` or the agent admin role.
* No agent is granted `write` to a whole workspace or domain as a way to make it useful. If an agent's
  read mode is not enough for the team, improve its read profile or add a door.

The server rules that posture depends on:

1. **Session reads by an agent turn bind to the asker** (built, #2457): an agent's own login reads
   sessions by grant only inside a running turn of that agent, and only those of the turn's verified
   asker ("coach me" is me only); a turn nobody asked for (a schedule, a trigger) or one asked by an
   administrator of the agent reads every grantor's (the coaching sweep); a stored token, a session,
   a socket, a finished, confined or unverified-asker turn reads nobody's
   (`canopy_sessions.access._review_grantors`, `tests/test_agent_session_reads_bind_to_the_asker.py`).
2. **An agent's own login re-reads its running turn's caller envelope** (built, #2462):
   `who_is_asking` and `GET /api/harness/turns/{id}/caller-context` answer for the turn that login was
   issued for, while it runs and unless its conversation is sensitive
   (`turn_access.can_read_caller_context`), so a confined turn can always learn whom it answers.
3. **A full-profile asker cannot read the agent's own secrets** (a rail): holding `write` is not a way
   to ask the agent to read out its credentials or vault. **Not built.** It is the remaining
   precondition for putting a whole team on `write` anywhere.

## Phase 1: read mode replaces `ask` (DONE)

### The gate

`manage.py read_mode_gate [--json]` (`apps/agents/read_mode_gate.py`) lists every agent with a
`read_only` or `use:ask` line (its own or a workspace default, not `write`) and no published `ask`.
Such lines were inert before read mode resolved on its own and are live after, so this is the one way
the change widens access. It exits 1 on any hit. On 2026-11-03 it returned none: all 18 agents'
interfaces and access lists were read, twice (published `ask`: ace, echo, eva, hal, oolong, trellis;
the rest hold only editor-or-above lines, jarvis's person editor line and labsdata's two `use:data`
door lines). Access lists keep changing, so **re-run it immediately before the deploy that includes
#2465**; that deploy waits on a result of none, or on each hit's owner decision (keep, remove, or
leave the agent without the entry).

### What was built

* **Resolution** (#2465): `roles.can_use` and `access._capability_for` resolve `read` to read mode; the
  name is the agent's published `ask` when it has one (stamp `ask`), else the default (stamp `read`).
  The duplicate constants (`roles.READ_CAPABILITY`, `interface.ASK`) became one set in
  `interface_default`.
* **Migration** (#2467): `agents/0119` rewrites a `use:ask` / `use:read` access line (agent or
  workspace default) to `read_only`; if the subject already holds a role line the capability line is
  dropped. Only the old *access lines* are audited (`SettingChange`, operation `read_mode_migration`);
  nothing changes an interface, so no interface is audited. Every published `ask` stays the agent's
  override under its own name, so on day one no live agent behaves differently. Its test
  (`tests/test_read_mode_migration.py`) is deleted once a deploy includes `0119`.
* **Screens, OpenAPI and docs** (#2469): `GET /api/agents/{slug}/interface` gains
  `read_mode {source: default|override, override, default_profile, default_profile_version}` and
  `GET …/access-list` gains `doors`; `_CAN["read"]` says read mode exists on every agent;
  `generated.ts` regenerated; `access.md`, `api-surface.md`, `frontend.md` updated. The "Add an ask
  capability" button is gone; the admission rule form offers `read`.
* **Dead fields** (#2471): `input`, `version` and `callers_default: none` are accepted, ignored with a
  logged warning and not written back (every stored interface carries `version: 1`, seven carry
  `input: {}`, so a strict removal would 422 a republish); `callers_default` set to anything else is
  still refused. `interface.capability_for` and `interface.granted_by` are removed.

### Verified

* The default profile is pinned exactly (`tests/test_default_read_profile.py`), including "no `gh`, no
  other host" and the entry rule.
* Admission rules fire across the two stamps; the envelope has `permissions == ["read"]` for both.
* The public A2A card for an agent with no `ask` is unchanged (a test).

### After the deploy (NOT yet done: nothing is deployed)

* `/api/openapi.json` carries the new `roles[].can` text and `generated.ts` matches it.
* The gate, run again, still returns none.
* End to end on a real agent, one asker per posture (the Sophie and ACE flow): a `read_only` asker is
  answered in a confined session, cannot write outside the scratch folder, and the turn's envelope
  shows `read` and the intended profile; an admin on the same agent still gets the full profile.
* A published-`ask` agent (Trellis) and an agent with no `ask` each answer a read-mode question.

## Phase 2 (direction only, to be designed with the compatibility constraints)

* **Site connections** become an agent setting ("this agent may be reached from connect-labs, with
  this ceiling and this login"), separate from any profile. The compatibility constraint is the site
  gateway: it refuses a confined turn whose capability does not list the site (`host_gateway.py`), and
  `page_sites`, `site_ceiling`, `own_login_of` and the site-agents listing all derive "set up for a
  site" from the capability. All of them move together.
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
  different profile, or is better avoided (more modes may be an anti-pattern) is open. Read mode is
  deliberately one mode, so this question cannot multiply it.
* Whether read mode keeps web search by default (eva, hal and echo have it; ACE does not).
* Whether the caller-skill convention (`answer-caller`) should be required. Phase 1 uses it when the
  agent's catalog has it, otherwise no entry. hal, eva, echo and oolong enter through `/<slug>:ask` or
  `policy-qa` and are overrides until renamed.
* Whether the default should grant file search, which needs an explicit bash pattern on a cloud
  runner (no `Grep`/`Glob` there).

## Doc drift found while auditing (separate fix)

`access.md` says `Decision` has a `role` field (the code has `relationship`, `agent_role`,
`permissions`, `basis`); its turn-mode ladder omits the trigger posture and the manual-session rung;
"Where it is asked" omits several callers of `decide`; `turn_mode.py` still says a workspace editor's
turns are always manual; `api-surface.md` says the sensitive switch is under "Settings → Interface";
and the `canopy agent interface` CLI help still describes the removed `full:` model.
