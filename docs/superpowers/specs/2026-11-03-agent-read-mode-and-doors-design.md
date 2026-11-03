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
`ask` capability. Of the nine live agents, six wrote one, and four of those six are close to
the same profile with the agent's own slug in it (ACE, eva, hal, echo; Trellis and oolong differ). The
screen asks an admin to author a tool allowlist to turn on the commonest case.

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
  * entry: the agent's own caller skill, `answer-caller`, by convention (both ACE and Trellis
    already ship that skill folder).
* **What "read" really lets an agent do** should be said plainly: the confined session cannot change
  anything except draft and send a reply on the thread it was asked on. Today's docs call it
  "read-only", which is not quite true for email.
* **An agent may override it** with its own profile (Trellis adds `gh api`, its sessions CLI and a
  few canopy tools; eva, hal and echo add web search). An override is the same fields as today and
  replaces the default whole; there is no merging.
* **Stamp.** A read-mode turn is stamped `read`. `ask` stays accepted as an alias on stored turns,
  access entries and sessions, so history and in-flight turns resolve. The enforcement layers never
  read the name (runner, `profile_guard`, native deny rules, caller token), so this is a decision-time
  change only.

### Doors

A door is today's capability, minus the pieces that moved: it keeps a profile, an optional entry
skill, and, for a Connected site, `sites`, `ceiling` and `own_login`. A person is let through a door
by an access entry that names it (`use:<door>`), as now. `write` opens every door, as now.
Phase 2 below moves site connections and the sensitive-data setting out of the door card.

## What does not change

* `decide`'s order and rows 1, 2, 3 and 5, the sensitive-data override, `manual_only`, turn mode.
* The live-state property: the profile is computed by canopy-web at claim time and delivered in the
  envelope; the agent's repo cannot widen it.
* The three enforcement layers and the caller token: they read only `tools`, `bash`, `read_paths`,
  `write_paths` and `entry`, never the name.
* Site gateway behaviour, the signed `Canopy-Execution` statement and the SDK's `capabilities[]`
  listing: hosts verify them, so their shapes are kept and fed from the new settings.

## Phase 1 (this design's commitment): read mode replaces `ask`

1. `roles.can_use` and `access._capability_for` resolve `read` to read mode; the free-form fallback
   no longer needs a published `ask`. The duplicate constants (`roles.READ_CAPABILITY`,
   `interface.ASK`) become one.
2. `interface.profile` builds the default for read mode when the agent has no override. An unknown
   or removed *other* name still yields the empty deny-all profile, so a turn stamped with a retired
   door fails closed.
3. **Migration.** Every published `ask` becomes the agent's read-mode override unchanged, so on
   day one no live agent behaves differently. A `use:ask` access entry becomes `read_only` (ACE has
   one). Each agent's old interface goes to the settings audit, as the earlier migrations did.
4. **Before it deploys, list every agent that has a `read_only` or `use:ask` entry but no published
   `ask`.** Today those entries are inert; after this change they go live. Each one needs a decision
   (keep, remove, or leave the agent without the entry), made by the agent's owner. This is the one
   way the change can widen access, so it is a gate, not a note.
5. Screens: the Settings tab becomes "Read mode and doors". It shows the default read profile in
   plain words, an "Override" action, and a short list of doors. The "Add an ask capability" button
   goes. The access-entry form's free-text capability name becomes a picker of existing doors.
6. Remove what nothing uses: `input` (validated and displayed, no reader), `callers_default` and
   `version` (accepted, ignored), the unused helpers `interface.capability_for` and `granted_by`.
   `pages:` is deprecated but live in three places; it stays until its remaining users are moved,
   and is called out in the UI as legacy.
7. The A2A agent card and topology views stop reading "no capabilities" as "nothing offered". The
   public card must not start listing read mode for every agent that has a contact or domain
   `read_only` entry unless that is chosen deliberately.
8. A test pins the default profile exactly, so a code change that widens every agent at once cannot
   pass unnoticed, and the envelope carries a default-profile version so a turn records which one it
   ran under.

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
* Whether the caller-skill convention (`answer-caller`) should be required or overridable per agent.
  ACE and Trellis have the folder; hal, eva, echo and oolong enter through `/<slug>:ask` or `policy-qa`
  and would be overrides until renamed.

## Doc drift found while auditing (separate fix)

`access.md` says `Decision` has a `role` field (the code has `relationship`, `agent_role`,
`permissions`, `basis`); its turn-mode ladder omits the trigger posture and the manual-session rung;
"Where it is asked" omits several callers of `decide`; `turn_mode.py` still says a workspace editor's
turns are always manual; `api-surface.md` says the sensitive switch is under "Settings → Interface";
and the `canopy agent interface` CLI help still describes the removed `full:` model.
