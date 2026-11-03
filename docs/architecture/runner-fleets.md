# Runners: fleets, credential sets and readiness

Runner fleets (`apps/harness/fleets.py`), credential sets (`apps/harness/credential_sets.py`), runner readiness (the not-ready marker) and "Why is my turn still queued?".

**See also:** [runners.md](runners.md), [runner-routing.md](runner-routing.md), [runner-credentials.md](runner-credentials.md), [runner-flags-and-schedules.md](runner-flags-and-schedules.md).

## Runner fleets (`apps/harness/fleets.py`)

Cloud capacity grows with a **fleet**: one EC2 Auto Scaling group of interchangeable
cloud runners, whose size canopy-web sets from the work waiting for it. Spec:
`docs/superpowers/specs/2026-10-24-runner-fleets-design.md`. Building one: `runner/ec2/fleet.sh`
(README § Runner fleets).

- **One approval, then AWS vouches.** A fleet (`RunnerFleet`) is approved once, by its
  creator, from a browser behind a passkey: its `RunnerInvitation` on the usual approve
  page, used up by the approval. No token can approve it. After that, an instance
  joins on its own at `pair-by-role` when STS says it signs as
  `assumed-role/<the fleet's role>/<i-…>` and EC2 says that instance is pending or
  running with the reserved tag `aws:autoscaling:groupName = <the fleet's group>`. The
  role trusts only EC2, so the session name is an instance id EC2 set, and no caller
  can set an `aws:` tag. The fleet must be active and its owner must still have
  `agent.work` there. Any refusal is the uniform 401.
- **A member is its own runner.** Its name is `<fleet>-<instance id>`. Its device key
  is role + instance (`aws_iam_proof.member_jkt`; sign-in reads the instance from
  `verify_caller`). Its `machine_id` is `<role>/<instance>`. One member cannot sign in
  as another, and the role on its own signs in as nobody. A fleet's role is never one
  box's identity: an invitation, a fleet and a device-key enrollment each refuse a role
  the other uses.
- **One KMS key per fleet, so new members need no person.** Every member's ECDH key is
  the fleet's KMS key (`ecdh-kms.cfn.yaml`), which only the role can derive with. The
  first member is granted the credential set's vault key by its owner (Make its key /
  grant, once per fleet). From then on canopy-web keeps the fleet's copy
  (`RunnerFleet.vault_wraps`, refreshed each tick) and gives it to each new member
  (`_give_wraps`). The copy is ciphertext for the same public key; a wrap binds vault,
  key version and the ECDH key thumbprint, so it opens on any member and canopy-web
  opens none of it.
- **A member is routed as its fleet** (`fleets.sync_routing`). Agents' runner lists and
  workspaces' runner orders name runners, and a member the scaler adds is a new runner,
  so the fleet keeps its routing (`RunnerFleet.routing`): every row its members hold. A
  member joins with those rows (a live member's, else the fleet's copy), and each tick
  first carries a change made to one member's rows (added to an agent, taken off one,
  switched off) to every member. Route a fleet by routing any one member. A member whose
  instance left the group is retired with its rows deleted, as the retire route does;
  the fleet's copy outlives every member.
- **A credential set is the fleet's one list** (`RunnerCredentialSet`, § Credential
  sets below). `fleet.sh up --credential-set <name>` makes the fleet read an existing
  set; otherwise it gets a new one.
- **The scaler** (`tick`, the off-request clock, `RUNNER_SCALER_TICK_S` 30 s, 0 = off).
  Each tick, per fleet:
  1. Read the group and its warm pool. `Runner.power` follows each instance's state:
     `InService` → `on`, `Pending` → `waking`, `Warmed:*` → `stopped`. A member whose
     instance left the group is retired, and a turn it was running is queued again as
     itself (`requeue_lost`, at most twice).
  2. **Fail over** before measuring demand. A chat bound to a member that is gone
     (retired, or unreachable and out of service or silent `RUNNER_FAILOVER_AFTER_S`,
     3 min) with a turn waiting moves to the live member with the most room
     (`send.move_queued_turns`, as the fleet's owner). A member that still answers is
     never moved off, because its handoff would still be packing.
  3. **Grow** for turns the fleet could claim (the claim's `_coverage` plus the tenant
     gate) that have waited past `RUNNER_WAKE_AFTER_S` (60 s) and are not behind their
     agent's or session's current turn. It adds `ceil(starving / member_turns)` members,
     less what members coming up already absorb, up to `max_members`.
  4. **Shrink** by one member per tick, never while anything waits, never below
     `min_members`. A member idle `RUNNER_IDLE_STOP_S` (30 min) goes `draining`; **the
     claim refuses a member that is not `on`**. Next tick, if it is still idle, its
     scale-in protection is lifted and the desired capacity lowered by one, so AWS
     scales in that one instance, back into the warm pool. Every member is otherwise
     protected, so AWS never picks a busy one.
  5. Each size change is a `runner.fleet.scaled` fact.

  A retired fleet is still ticked while it has members: they are retired and their
  chats move as their instances go.
- **Readers.** A member in the warm pool or coming up, not paused or signed out, of an
  active fleet, is `Runner.wakeable`: `turn_reach` counts it as a LIVE taker and the
  unclaimable report as reachable.
- **Keep `min_members` ≥ 1**: the Gmail doorbell and inbox pollers run on a box (every
  member polls; the server dedupes mail turns per thread). **Capacity per member** is
  `MAX_CONCURRENT_TURNS` = RAM / 1.5 GiB (an m7i.xlarge runs 10); `fleet.sh` tells
  canopy-web the same number (`member_turns`).
- **Out of scope:** sensitive-data boxes and machines running several runners are single named
  boxes (`up.sh`). A fleet member is one runner for one workspace tree.


## Credential sets (`apps/harness/credential_sets.py`)

A **credential set** (`RunnerCredentialSet`) is one ordered list of Claude tokens and API
keys that several cloud runners read as their own (`runners.list_owner`): every member
of a fleet, and any other cloud runner of the set's owner that joins it. An edit from
any of their Credentials tabs (add, rename, reorder, remove) edits the set, and every
reader's stamp moves. Tokens and API keys only: a full login is refused (409), because
its refresh token rotates and cannot be shared. Usage caps are per Claude account, so
N runners on one token share one cap.

- **The list is shared across workspace trees; the values are not.** A vault never
  serves two isolation boundaries (`docs/architecture/vault.md` § A runner's own
  vault), so a set keeps **one vault per workspace tree** (`RunnerCredentialSet.vaults`,
  matched by the tree root of the vault's workspace, `credential_sets.vault_in_tree`).
  The set's first runner in a tree brings its own vault or gets a new one named for the
  set; every later runner in that tree is attached to it. A runner on a set is served
  by that vault only. Each tree's vault holds its own copy of each value, sealed by a
  person of that vault in their browser ("Add its key") or by one of its boxes after a
  sign-in ("Get a token", which fills the existing entry). An entry with no value in a
  runner's tree is one that runner skips; its Credentials tab says how many it lacks,
  and how each other tree's vault stands.
- **Coming onto a set.** A fleet member joins its fleet's set as it pairs. `POST
  /api/harness/runner-credential-sets/` `{runner, name}` shares a runner's own list:
  its entries and vault become the set's, so nothing it reads changes (409 while the
  list holds a login). `PUT …/runner-credential-sets/runners/{id}` `{credential_set}`
  makes a runner read a set (409 while it has entries of its own, or for a fleet
  member; 422 `vault_crosses_isolation` when its tree's vault already serves runners of
  another boundary, such as an agent's runner beside a tree's runner), or `null` to read its own
  list again. The set's vault then stops serving it, and its copy of the key is handed
  out to nobody (`workspace_vaults.device_may_hold`).
- **Making a set is separate from choosing who reads it.** The Credentials tab offers
  "Make a credential set from this list" (the POST), then shows the set's list, the
  runners reading it, and an "Add a runner…" picker of the owner's other cloud runners
  (`credential_sets.candidates`: each with why it may not join yet, and whether it is in
  a tree the set has no vault for). Adding one is the PUT on that runner.
- **One owner.** A set, and every runner on it, have the same owner. Only that person
  puts a runner on a set, and only they change a set's list (`api_runners._list_admin_or_404`,
  403): a runner administrator administers one runner, and the list is every runner's on
  the set. They still read it.
- **Removing an entry** deletes its secret from every tree's vault of the set, unless a
  runner outside the set that a vault also serves still lists it.
## Runner readiness (the not-ready marker)

A failed turn writes `~/.canopy/not-ready` (`runner/canopy_runner/canopy_runner/readiness.py`,
`mark_failed`); `readiness.compute` reports it on the heartbeat as `ready=False`, a
not-ready runner is not `Runner.is_available`, and lower ranks in every cascade
take its work. `mark_ok` clears it only after a turn executes, so the marker **expires after
`MARKER_TTL_SECONDS` (15 min)** — otherwise one transient failure exiles a box that
keeps heartbeating ONLINE. Expiry is retry-with-backoff: a genuinely broken box
fails its next turn and re-marks itself. The CDP probe gates independently
(`compute`), so expiry cannot mask emdash being down.

**Signed out is the one not-ready note that stops claiming.** A laptop turn finishes
at delivery, so without this a box with no Claude login would stay ready and answer
every message with the CLI's "Not logged in". The hook listener reads the transcript
tail on `Stop`/`Notification` (`canopy_transcript.signed_out`, the API-error record
only — never agent text): on that record the box writes the marker with
`canopy_transcript.SIGNED_OUT_NOTE`, raises `claude_auth_required` once
(`hooks.report_signed_out`, mailed by `credential_notify`), and skips its claim; on
the next real reply from any session on the box it clears it (`note_signed_in`). The
server reads the note as `Runner.signed_out`: `claim_next_turn` refuses the box
(above the pin), `turn_reach` returns `signed_out` for a turn only signed-out boxes
could take, admission rules treat it as offline, and the session list carries
`runner_signed_out` for `/ide`. Other not-ready notes (no Claude desktop CLI, which
falls back to emdash) still claim.

## Why is my turn still queued?

A QUEUED turn explains itself. `Runner.last_claim_poll_at` records when a runner last ASKED FOR
WORK (distinct from its heartbeat — a laptop's daemon can heartbeat for hours while the app that
takes turns is asleep); `claim._note_skips` records on the turn why a box passed it over; and the
`harness.note_waiting_turns` tick (`apps/harness/waiting.py`) writes one plain sentence per turn
queued past two minutes (a hold; the turn ahead in its conversation; "online but has not asked for
work for 5h16m"; offline / paused / signed-out runners; unroutable). They are `waiting` events on
the turn — `GET /api/harness/turns/{id}/events`, or `canopy agent turns --slug <agent>`, which
prints the newest under each queued turn. They are their own event kind so Slack and chat status
reactions to `status` rows are not triggered.
