# Huddle outcomes as multi-player proposed projects

**Date:** 2026-10-16 · **Status:** approved — see 2026-10-16-work-group-huddles-design.md · **Owner:** Jonathan (decisions), Ada (spec)

## Why

Jonathan, 2026-10-16 (AI Learning Champions next steps, item 4): "Just like we are trying
to get multiple-player mode better working for projects, we will want to do that for
huddle outcomes (which are really proposed projects) so lets think through the design on
that." Same mail: for the next four weeks huddles serve the AI Learning Champions work
groups, and the summaries and proposals "are not really clicking for me yet, so expect a
ton of iteration."

## What a huddle outcome is today

- **Derived, not stored** (`apps/huddles/services.py`). A huddle is an anchor turn plus
  round turns; its outcomes are whatever `AgentTask`s have `source_url` ending in
  `/huddles/<id>`.
- **`canopy huddle file`** turns each surviving proposal into a `suggested` task on the
  **lead's** board (inside a project it finds or creates by name), plus a separate linked
  task on **each partner's** board. Nothing ties those cards together except the URL and
  the title.
- **Co-signs live in agent replies.** Who backed a proposal, who amended it, and how an
  agreement thread settled are in round-3 JSON and thread transcripts. The huddle page
  shows them; the task and the project do not.
- **One person decides.** Jonathan approves or declines the lead's card from the email.
  Nobody else (a work-group member, Shayoni, another agent's owner) can back it, push back
  on it, or comment on it in a place the lead reads.

## What "multi-player" already means for projects

The pieces exist on `AgentProject`, mostly for one agent and its people: `ProjectParticipant`
(the people a project involves, and how canopy learned it), the team view
(`GET …/projects/{ref}/team/`: what it is, where it stands, decisions waiting on people),
ideas (`kind="idea"` tasks quoting a person, which any workspace member can add), contributor
pages, and the five task actions (`approve / decline / reply / nudge / done`, where an
editor's reply wakes the agent). The design below reuses these pieces and adds only what a
huddle outcome needs.

## Proposed model

1. **A proposal becomes a project with status `proposed`** on the lead's agent (a new
   fourth status beside active / done / archived). Its `outcome` is the proposal's
   "done when", its `links` include the huddle page, and its tasks are the plan steps.
2. **One decision card per proposed project:** a review-ask task, "Start this project?".
   *Approve* sets the project to active and starts the lead's turn. Each partner's linked
   task (still on their own board, still pointing at the project) starts too.
   *Decline* archives the project and keeps the reason. These are the existing actions;
   no new verbs.
3. **Backing is a row, not a transcript.** A new `ProjectBacking` table holds
   (project, backer = agent or person, stance `backs` / `backs with changes` / `passes`,
   one line, at). `huddle file` writes the agents' round-3 answers into it. A person adds
   or changes their own from the project page. The page shows it as a short list:
   "Echo backs · Eva backs with changes: <line> · Shayoni passes: <line>".
4. **Comments are replies on the decision card.** Any viewer can reply, and an editor's
   reply wakes the lead. That is the comment thread already, so nothing new is built.
5. **People become participants.** Anyone who backs or replies is added as a
   `ProjectParticipant` (new source `backing`), so they appear in the team view and can be
   asked things later.
6. **Iteration is one object.** A later huddle that lands on the same open proposed project
   revises it (new outcome or plan, backings carried over and marked stale) instead of
   filing a second card. A declined one comes back only with `new_evidence`, which is
   today's rule.
7. **The huddle page and the email point at projects.** The page's outputs become the
   proposed projects, each with its backing list. Each line in the email links to its
   project page, not to a board.

Out of scope: realtime co-editing (SP3's draft/presence model is for chat, and proposals
are short), and Drive layout (each project keeps its own Drive folder under the lead's root).

## Decisions for Jonathan

Answer by number, e.g. "1 yes, 2 no".

1. **Proposed project, not loose tasks.** A huddle proposal is filed as an `AgentProject`
   with status `proposed`, plus one "Start this?" card. Recommend: yes.
2. **Projects stay per agent** (your 2026-09-19 rule). The lead owns the project; partners'
   work stays on their own boards as tasks linked to it. Recommend: yes. The alternative is
   letting partner agents' tasks live inside the lead's project, which would reverse that rule.
3. **Backing is recorded and shown** (`backs` / `backs with changes` / `passes`, plus one
   line) for agents and people on the project page. Recommend: yes.
4. **Who can start a proposed project.** (a) only you; (b) you, or the person you name as
   its owner; (c) automatically once N people back it. Recommend: (b). Backing is a signal,
   not a gate.
5. **Who can back and comment.** Any member of the lead's workspace. For the AI Learning
   Champions groups, that means inviting group members to Ada's workspace as viewers.
   Inviting people is outbound, so you would say who. Recommend: yes, and name the groups
   once Shayoni finalises them (after 2026-10-21).
6. **Comments use the existing reply action** on the decision card. No separate comment
   system. Recommend: yes.
7. **A later huddle revises an open proposed project instead of duplicating it.**
   Recommend: yes. This is where most of the "expect a ton of iteration" will happen.
8. **Unanswered proposals expire.** A proposed project nobody starts or declines within
   14 days is archived as "not taken up". Recommend: yes, so the Projects page does not
   fill up with stale proposals.

## If approved: the work, in order

1. canopy-web: `proposed` status, `ProjectBacking` model + routes (each also an MCP tool),
   the backing section on the project page, the huddle page's outputs as projects, the
   participant source `backing`, the expiry job.
2. canopy: `huddle file` creates or revises the proposed project and its decision card,
   and writes the round-3 backings.
3. ada: `bin/ada-huddle email` links each proposed project page (`tests/test_ada_huddle.py`
   pins the shape).
