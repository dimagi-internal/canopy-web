# Runner fleets: cloud capacity that scales itself

**Status:** design, 2026-10-24. Phase 1 is built with this spec. Jonathan asked for it the same day, after an elastic design that started and stopped pre-paired named boxes: *"if this is core infrastructure, shouldn't we have a safe way to say this runner was linked because the trusted infra spun it up? … we do NOT want to rely on anything pre-existing on the box that is stateful or a secret aside from making sure existing sessions are routed to the right runner but if the runner goes down mid conversation, we would just try to transfer it elsewhere. … I want to create runner credential sets that can apply to a fleet of runners as they scale up scale down. It still may be the right choice to leave instances around to startup … but building from scratch should also be seamless and automatable."*

## Intent

A **fleet** is a set of interchangeable cloud runners that canopy-web grows and shrinks with demand. A person approves the fleet once, with a passkey. After that, any instance that AWS proves belongs to the fleet's auto-scaling group joins on its own. Every member uses the fleet's **credential set**. A member keeps nothing on disk that the fleet needs. When a member goes away, its conversations move to another member.

## Today

- **Identity.** A cloud box proves its EC2 instance role with a signed, unsent `sts:GetCallerIdentity` (`apps/harness/aws_iam_proof.py`, the HashiCorp Vault AWS-auth pattern). Its device key is the role (`jkt` = sha256 of the role ARN). A person's passkey approves a one-hour invitation for that role and runner name (`api_runner_invitations.py`). STS returns the instance id as the assumed role's session name; canopy-web discards it.
- **Secrets.** The box's ECDH key is a per-stack KMS key that only its role can use. Claude tokens and API keys are sealed in a workspace vault, wrapped to that key (`docs/architecture/vault.md`). A box keeps no secret on disk except a full `claude auth login`, whose refresh token rotates.
- **Credentials.** Each runner has its own ordered list (`RunnerLogin`). Several runners may share a vault, but each still has its own list (spec `2026-10-23-runner-claude-credentials-in-the-vault-design.md`).
- **Sessions.** A chat is bound to one runner (`RunnerBinding`), and its turns go only there. A transfer (`canopy_sessions/placement.transfer_session`) carries the history and, when the source answers, its worktree and Claude session (`handoff.py`). A person starts it; nothing moves a session automatically (`routing.runner_target_q`).
- **Building a box.** `up.sh` makes one stack per box from stock Ubuntu. The box installs everything at boot, and the first bootstrap takes up to ~40 minutes.

## Decisions

1. **Infrastructure is the attestation; a person approves the fleet, not each box.** The approval is still a passkey touch by its creator, in a browser; no token can give it. What it approves is a fleet: a workspace, an owner, an IAM role, a KMS key and an auto-scaling group. A member joins only when all of these hold:
   - STS says the caller is `assumed-role/<the fleet's role>/<i-…>`. The role trusts only `ec2.amazonaws.com`, so the session name is the instance id, set by EC2.
   - EC2 says that instance is pending or running and carries the tag `aws:autoscaling:groupName = <the fleet's group>`. Tags starting with `aws:` are reserved, so only Auto Scaling can set one.
   - The fleet is active, and its owner may still run work in its workspace.

   This **changes** the invariant "a runner pairs only by passkey-approved invitation". It becomes "a runner pairs by a passkey-approved invitation, or as a member of a passkey-approved fleet". Nothing a token or a box sends can approve either.
2. **One identity per instance.** A member's device key is its instance: `jkt` = sha256(role ARN + "/" + instance id). A member cannot sign in as another member, and the role on its own signs in as nobody. A role that has a fleet can never be one box's identity, and the reverse holds too.
3. **One KMS key per fleet.** All members open the fleet's vault copies with the fleet's ECDH key, which only the fleet's role can use. When a member joins, canopy-web copies the fleet's wrap of each vault key to the new member's device key. canopy-web can do this because the wrap is ciphertext for a public key the member also holds. canopy-web still cannot open any of it. This **changes** decision 3 of the credentials-in-the-vault spec ("device keys are never shared"), and only within a fleet. A member that is gone loses access with its instance role. A rotated vault key reaches only live members, through the person who rotates it.
4. **A credential set is the fleet's one list.** `RunnerCredentialSet` holds an ordered list of tokens and API keys (`RunnerLogin` rows pointing at the set) and the vault their values live in. Every member reads the set's list as its own. Editing the list from any member's Credentials tab edits the set, and every member re-reads it. A full login is refused: its refresh token rotates and cannot be shared, and it would be state on one box. As before, quota comes from accounts, not copies: N members on one token share one cap, so a set should hold tokens from several accounts.
5. **canopy-web drives the group's size; AWS keeps the machines.** The fleet is an EC2 Auto Scaling group with a **warm pool** of stopped, already-bootstrapped instances (`ReuseOnScaleIn`). These are the "instances left around to start up", and AWS manages them. Building from scratch is the same group with an empty warm pool. canopy-web's scaler sets the desired capacity from queued turns, not from CPU. Members are protected from scale-in. To remove one, the scaler drains an idle member, lifts its protection, and lowers the desired capacity by one. The member goes back to the warm pool, and AWS never stops a busy member.
6. **A member is disposable, so its sessions fail over.** A chat bound to a member that is gone (retired, terminated, in the warm pool, or offline past `RUNNER_FAILOVER_AFTER_S`) and that has a turn waiting is transferred to a live member with room. The transfer uses the existing path (`send.move_queued_turns`): with the work when the source still answers, otherwise from the conversation's history. A turn that was executing on a member that died is closed as LOST and asked again on the new member. This **changes** "no automatic failover" for fleet members only. A single named box keeps today's behaviour.

## The model

- `RunnerFleet`: `name` (a slug, and the prefix of every member's name), `workspace`, `owner`, `role_arn`/`role_jkt`, `ecdh_public_jwk` (the fleet's KMS public key), `region`, `group_name`, `min_members`/`max_members`, `member_turns` (turns one member runs at once), `credential_set`, `status` (`pending` → `active` → `retired`), `approved_at/by`, `vault_wraps` (the fleet's copies, `{vault_id: {kv, wrap}}`).
- `RunnerCredentialSet`: `name`, `workspace`, `owner`, `vault`.
- `Runner.fleet`, `Runner.instance_id`, `Runner.credential_set`. A member's name is `<fleet>-<instance id without "i-">`.
- `RunnerInvitation.fleet`: a fleet invitation. Approving it on the existing approve page activates the fleet.
- `RunnerLogin.credential_set`: an entry belongs to exactly one of a runner or a set.

### Joining (`POST /api/harness/runners/pair-by-role`)

The box sends the same request as today, with `name = <fleet>-<instance>`. When the proven role belongs to a fleet, `fleets.join` runs the checks in Decision 1. It then:
- creates or re-attaches the member (a warm-pool instance comes back with the same id, so it is the same runner);
- creates its device key and approves the fleet's ECDH key, approved by the fleet's approver;
- attaches the set's vault and copies the fleet's wraps.

Any refusal is the same uniform 401 as today.

### The scaler (`apps/harness/fleets.py`, a tick on `apps/common/ticks.py`)

Every `RUNNER_SCALER_TICK_S` (30 s), for each active fleet:
1. Read the group (`DescribeAutoScalingGroups`). Each member's `power` follows its instance's lifecycle state: `InService` → on, `Pending` → waking, `Warmed:*` → stopped. A member whose instance has left the group is retired, and its sessions fail over.
2. **Grow** when turns the fleet could take have waited past `RUNNER_WAKE_AFTER_S` (60 s). The "could take" test is the claim's own coverage and tenant gate, asked of a live member. A turn waiting only behind its own agent's or session's current turn does not count. The new desired capacity is `in service + coming up + ceil(starving / member_turns)`, capped at `max_members`.
3. **Shrink** one member per tick. The member must have nothing executing or queued and must have been idle for `RUNNER_IDLE_STOP_S`. The fleet must be above `min_members`. One tick drains the member (the claim refuses a draining runner). The next tick, if it is still idle, lifts its scale-in protection and lowers the desired capacity by one. Work that arrives in between undrains it.
4. **Fail over** the sessions of members that are gone (Decision 6).
5. Refresh the fleet's copy of each vault wrap from any member that holds the current key version.

### Infrastructure (`runner/ec2/fleet.cfn.yaml`, `fleet.sh`)

One stack per fleet holds:
- the role, which trusts only EC2;
- the KMS ECDH key, which only the role can use;
- a launch template, with the same cloud-init as a single box, and `RUNNER_NAME` derived from the instance id at boot;
- the group, with new instances protected from scale-in;
- the warm pool, with `PoolState: Stopped` and `ReuseOnScaleIn: true`;
- a launch lifecycle hook, which the member completes once its first bootstrap is done, so a warming instance is stopped only once it is ready;
- a policy on canopy-web's task role that lets it set the desired capacity and the instance protection of **this group only** (`aws:ResourceTag/canopy:fleet`).

`fleet.sh up` deploys the stack, creates the fleet with your token, prints the approve link, and waits. `fleet.sh down` sets the group to zero and retires the fleet.

## Phases

1. **This PR:** fleets, joining with infrastructure attestation, identity per instance, credential sets, the scaler (group plus warm pool), failover from history or a live handoff, the stack, and docs. It replaces the elastic start/stop of named boxes from the first version of this branch.
2. **Lossless failover from a crash:** after each turn a member saves a checkpoint (the handoff archive format) of the session's worktree and Claude session, and failover restores the latest one. A crashed member then loses only the turn it was running, which is asked again.
3. **A baked image:** an image pipeline builds the AMI with tooling, Claude Code, the canopy CLI and plugins, so a from-scratch member is ready in minutes. The warm pool then only saves the boot.
4. **The approve page wraps the vault key** to the fleet's ECDH key in the same passkey touch. Then even the first member of a new fleet needs no second visit.

## Out of scope

- Sensitive-data boxes: a sealed conversation's key is wrapped to people, not boxes.
- Machines running several runners.
- Laptops.

A fleet serves one workspace tree, and its members are refused sensitive-data mode.
