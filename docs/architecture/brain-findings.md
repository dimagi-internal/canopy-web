# Canopy Brain: findings

What happens to an output after a run writes it: who sets its status, how a repeat folds in, how dismissals are
remembered, when it fades, and how far it reaches. Part of [brain.md](brain.md).

## A finding's lifecycle, and repeats (`apps/brain/findings.py`)

An output's `state` is the cascade's (active, withdrawn, stale) and nobody sets it. Its `status` is the people's:
`open` when made, then `accepted`, `dismissed` or `done` (`POST /api/w/<ws>/brain/outputs/{id}/status`, by the management
rule, each change an event in its workspace naming who made it). Only someone who sees it AND acts for its audience sets it
(`findings.may_set`; an admin agent only a `system` or `project` one, `findings.actor_may_set`): a `system` finding's workspace owners and admins (or an ancestor's) or a canopy admin, a `project`
output's members who may read the project, an `individual` or `memory` output's own person.

A run names what an output is with a `key`. An emit whose kind and key match an output of the same learner, subject and
workspace that is still active and `open` or `accepted` is a repeat and folds into it (`findings.match`, `fold`): `seen`
counts it, the newer words replace the old, and this run's reads join the lineage, so a removal of any session either
run read still reaches it. A `dismissed` one takes a repeat the same way while it is within its target's cooldown
(`dismiss_cooldown_days`, 60 unless set, from when it was dismissed; `budget.dismiss_cooldown_days`) and **stays
dismissed**: a sighting, not news; after the cooldown a repeat is a new output. A `done` one never takes a repeat: the
repeat is a new output naming it (`BrainOutput.recurs_of`, `findings.came_back_of`; `came_back` on the page), a fix that
did not hold. A repeat spends no output of the run's budget. Every emit that makes or folds into an output adds a
sighting (`BrainOutput.sightings`, `findings.sighted`). A finding's reach (`findings.breadth`) is its distinct cited
sessions and the distinct people in them across every fold (`OutputLineage.participant_scope`): a `system` finding in
one session is a `single session`, in two or more a `pattern` (`findings.breadth_label`); a one-off is labelled, never
refused.

A run is told the open findings its learner already holds for its subject and workspace (`findings.open_for`, in the
run context's `open_findings`: id, key, title, sightings, most recently seen first, capped) and told to reuse a key for
the same problem; it may also name the finding outright (`brain_emit`'s `same_as`, `findings.match_id`). It is told
the ones dismissed within the cooldown too (`findings.dismissed_for`, `dismissed_findings`, most recently dismissed
first, capped), as "don't re-raise unless something changed". The page's copy of a run's context leaves both lists out
(`state.context_shown`): who sees a run is not who sees its findings. Two outputs
that are one problem are merged (`POST /api/w/<ws>/brain/outputs/{id}/merge`, by the rule that sets a status, on both;
`findings.merge`): the kept one takes the other's sightings, lineage and a `merged_from` note, the other becomes
`duplicate` with `merged_into`, a repeat of its key folds into the kept one, and an `output.merged` event records it.
An open or accepted finding no run re-saw within the fade window of its type is **faded** (`findings.faded`,
computed, nothing deleted). The window is its body's `type`, else its `signal`, looked up in `budget.FADE_BY_TYPE`
(friction and provision fade in weeks, a skill gap or over-claim in six, design in a quarter) under the target's own
`fade_days_by_type`; a finding of no known type takes the target's `fade_days` (21 unless set) (`budget.fade_days_for`).
A faded finding is left out of runs' context, listed last on the page, fresh again when a
repeat folds in or someone sets it open or accepted (`set_status` moves its `last_seen_at`).
