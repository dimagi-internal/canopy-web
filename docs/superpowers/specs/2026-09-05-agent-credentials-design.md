# Agent credentials in canopy-web — creating an agent without 1Password or the box

**Date:** 2026-09-05
**Status:** Design — approved in direction, not yet built
**Builds on:** `2026-07-20-agent-runtime-registry-design.md` (the registry + reconciler;
its **secret store** half is what this replaces for the agent layer)
**Depends on:** #663 (the box reads its roster from canopy-web), `echo/runtime.yaml`,
`ace/runtime.yaml` (dimagi-internal/ace#1978) — the declarations this resolves against

## Problem

> *"My goal is that this could be fully configured on canopy-web and that someone
> could plausibly create a completely new agent without direct access to the cloud
> box or 1Password."* — Jonathan, 2026-09-05

Today, standing up an agent needs **three** kinds of access most people don't have:

1. **CloudFormation** — the roster was pinned in the `AgentSlugs` stack parameter.
   *Closed by #663:* the box reads the roster from canopy-web.
2. **1Password** — every secret VALUE lives in a per-agent vault (`Agent-Ace`,
   `Agent-Echo`, …), resolved on the box by a service-account token. Creating an
   agent means creating a vault and populating ~45 items by hand.
3. **The box** — interactive OAuth (`gog login`, `claude setup-token`) is run by a
   human in a terminal, and the resulting token is pasted into 1Password.

(2) and (3) are what remain. This spec closes (2) and moves (3) into the web UI.

**The motivating failure is not hypothetical.** On 2026-09-05 a from-scratch rebuild
found ACE's mailbox dead: its token had been minted 2026-05-01 under an OAuth client
ACE's own config warns against, and nothing surfaced that until a drill ran three
weeks later. Nobody could see the state of a credential without SSH-ing to a box and
running `gog auth list`. **Credentials that only exist in a vault and a keyring are
credentials nobody is watching.**

## Decisions

- **canopy-web becomes the agent secret store**, mirroring `RunnerCredential`
  (Fernet at rest, `apps/common/encryption.py`). 1Password becomes an **import
  source and a fallback**, not the required path. This is the decision the Runtime
  Registry spec deferred ("1Password is the single source of truth for laptop AND
  cloud"); it is reversed deliberately, and the reason is the goal above — a vault
  you must be granted is exactly the access barrier being removed.
- **Values are write-only over the API.** Set and rotate; never read back as
  plaintext by a human or a UI. The only reader is a **runner**, authenticated by
  the same PAT gate `RunnerCredential` already uses. `GET .../status` returns
  which slots are set and when — booleans and timestamps, never values — which is
  the existing `runner_credential_status` shape.
- **The declaration is `runtime.yaml`; the values are canopy-web's.** The UI renders
  one row per `secrets:` entry the agent declares, so "what does this agent need"
  and "what is actually set" are the same screen. An agent with no `runtime.yaml`
  gets an empty form rather than a guess — which is itself the nudge to write one.
- **Named slots, not fixed columns.** `RunnerCredential` has five columns because a
  runner has five credentials. ACE declares 45 refs and echo 12, so this is a
  `(agent, name) -> ciphertext` table.
- **OAuth is minted in the browser, not the terminal.** See below; this is the half
  that cannot be solved by moving values around.
- **Resolution order on the box: canopy-web → 1Password → skip.** Existing agents
  keep working with nothing set; a new agent works with nothing in 1Password. The
  migration is therefore per-secret and reversible, never a cutover.

## Model

```python
class AgentCredential(models.Model):
    """One named secret for one agent, encrypted at rest. The value is write-only
    over the API: set/rotate freely, never read back except BY A RUNNER."""
    agent = models.ForeignKey("agents.Agent", related_name="credentials", ...)
    name = models.CharField(max_length=120)      # matches a runtime.yaml `secrets[].name`
    value_enc = models.TextField()               # Fernet ciphertext
    updated_at, updated_by = ...
    class Meta:
        constraints = [UniqueConstraint(fields=["agent", "name"], name="one_value_per_agent_secret")]
```

No `optional` / `env` / `path` columns: those are the **declaration's** job and live
in `runtime.yaml`. Duplicating them here is the two-writers mistake that produced
the `gog_client` drift (three copies, two wrong). canopy-web stores values; the repo
declares shape.

## API

| Route | Purpose |
|---|---|
| `PUT /api/agents/{slug}/credentials` | Upsert `{name: value}`. Write-only. Membership-gated like the rest of the agents surface. |
| `DELETE /api/agents/{slug}/credentials/{name}` | Remove a slot. |
| `GET /api/agents/{slug}/credentials/status` | Per declared ref: `name`, `required`, `set`, `updated_at`, `updated_by_email`, `source` (`canopy-web` \| `1password` \| `unset`). **Never values.** |
| `GET /api/agents/{slug}/credentials/resolve` | **Runner-only.** Returns the plaintext map. Gated exactly as `RunnerCredential`'s fetch is — a paired runner's PAT, owner-scoped. |

`status` is the screen that would have caught the ACE incident: it names every ref
the agent declares, and says which are unset — a question that today requires SSH.

**`resolve` is the one route that returns plaintext,** so it carries the same
guarantees as the existing runner credential fetch and no others: TLS only, PAT
auth, no browser session, never logged, and an event row per fetch so a credential
read is visible in the fleet log rather than silent.

## The OAuth half — mint in the browser

Moving values into canopy-web does not help with `gog login`: a Google refresh token
can only be produced by a human consenting **as that mailbox**, and today that
happens in a terminal on someone's laptop and is pasted into a vault. That is both
the worst step to ask a newcomer to perform and the one whose result nobody can see.

```
POST /api/agents/{slug}/oauth/google/start   -> {authorize_url}
GET  /api/agents/{slug}/oauth/google/callback -> stores refresh token as `gog-token`
```

- canopy-web holds the **shared `canopy` OAuth client** and requests the scopes the
  agent declares (`config/agent.json.gog_services`; ACE needs `slides` + `calendar`
  beyond the fleet default).
- The operator clicks **Mint**, signs in as `ace@dimagi-ai.com`, consents, and the
  refresh token lands encrypted in `AgentCredential` — **no terminal, no vault, no
  copy-paste of a secret.**
- The token is stamped with the client it was minted under. This is the fix for the
  2026-09-05 failure at its root: *a token is minted FOR a client and works only
  with that client*, so recording which client produced it turns an invisible
  mismatch into a rendered fact.

**Claude credentials stay out of scope.** `claude setup-token` is Anthropic's flow
and not ours to embed, and the Claude login is **runner-level, not agent-level** —
the agent brings its identity, not its AI subscription (both `runtime.yaml` files
say so). It stays on `RunnerCredential`.

## UI — one screen per agent

`/agents/{slug}/credentials`, rendering the agent's declared refs:

```
ACE — credentials                          declared by runtime.yaml (45 refs)

  IDENTITY
  ● canopy-pat            set   2026-07-17  by jjackson@         [rotate]
  ⚠ gog-token             SET, BUT STALE — minted 2026-05-01 under client `ace`;
                          this agent declares `canopy`             [mint again]
  ● gws-sa-key            set   2026-04-09  by jjackson@         [rotate]

  COMMCARE HQ            5 of 5 set
  CONNECT + LABS         2 of 2 set
  OPEN CHAT STUDIO       10 of 10 set
  NOVA                   ○ nova-api-key    UNSET — required      [set]
```

Grouping follows the comment headers in `runtime.yaml`, so the screen is generated
from the declaration rather than hand-maintained. Optional refs collapse; **unset
required refs sort to the top**, because "what is stopping this agent from running"
is the question the page exists to answer.

## Reconciler changes (`bootstrap_agents.sh`)

Today it hardcodes `op inject .env.tpl` per agent and a per-agent vault name. It
becomes: read `runtime.yaml` → `GET /credentials/resolve` → materialise each ref to
its declared `env:` or `path:` → **fall back to 1Password for anything unset** →
preflight.

The fallback is what makes this shippable in one PR without a migration: every agent
keeps working with zero credentials in canopy-web, and each secret moves when
somebody sets it.

## Testing

- **Write-only** — no route returns a value except `resolve`; asserted by walking
  the router, not by inspection, so a future route cannot quietly leak one.
- **`resolve` is runner-gated** — a browser session, a non-paired runner, and
  another tenant's runner all 403; a paired runner gets the map.
- **Encryption at rest** — the column holds ciphertext; the plaintext never appears
  in a `values()` dump or in a log line.
- **Status is derived from the declaration** — an agent with no `runtime.yaml` shows
  zero refs, not a guess; a ref declared and unset reads `required` correctly.
- **Resolution order** — canopy-web wins; 1Password fills gaps; neither means skip
  (and skip is not an error for an `optional:` ref).
- **OAuth round trip** — start returns an authorize URL for the declared scopes;
  the callback stores a token stamped with its client; a mismatch between that
  stamp and the agent's declared client renders as the ⚠ row above.
- **The regression that motivated this** — a token stamped `ace` on an agent
  declaring `canopy` must surface on `status` as stale, not as `set`.

## Trial: ACE first

ACE is the right first agent — it has the most refs (45), the most surface, and the
credential that is currently broken. Order:

1. Ship the model + API + status screen; nothing depends on it yet.
2. Mint ACE's `gog-token` through the browser flow. **This also fixes the live
   outage** — ACE's mailbox has been dead since 2026-05-01 — so the trial and the
   repair are the same action.
3. Move ACE's remaining refs from 1Password, watching `status` go green.
4. Rebuild the cloud box. Success is: ACE provisions with **no `Agent-Ace` vault
   access at all**, which is the goal stated in one sentence.
5. Then echo (12 refs, already declared), then the rest.

## Out of scope

- **Per-user secrets.** `chrome-sales` acts on behalf of the dispatching human, not
  the agent; echo's `runtime.yaml` already flags this as a separate concern. An
  agent vault is the wrong home and this does not become one.
- **Claude credentials** — runner-level, see above.
- **Rotation policy / expiry alerting.** `status` renders `updated_at`, which is
  what makes a policy possible later; the policy itself is not this.
- **Capability-aware routing.** Noted 2026-09-05: a laptop is fast at DDD video
  capture and the cloud box is not, so some work must route by what a box CAN DO,
  not only by who asked. That is a routing spec (an extension of actor rules), and
  is unrelated to credentials beyond sharing the word "runtime".
