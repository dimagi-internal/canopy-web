# Two axes, three kinds of credential: one model for runner and direct use

**Status:** built 2026-10-30 (items 1–5 below). Item 2 was narrowed: `_classify_read`'s owner-at-own-laptop branch only labels an admin's hand read for the alerts, so it stays; the change is that a request naming the person's own local turn is a `license` read before `runner`/`admin` are asked. Jonathan, after act-as-you settled into a clear pattern: the code has grown three overlapping "modes" because it was shaped around a power user who is a runner owner, a direct user and an overseer at once. A normal person has two choices only: where the session runs, and whether it is reviewed. He also retracted two earlier rules: that direct mode must never use the agent's credentials, and that direct mode must always step up before acting as the person. With step-up now painless, the power user needs no special path and steps up like everyone else.

## The model

**Two axes, both chosen by the person, neither implied by the other:**

| Axis | Values | Where it lives today |
|---|---|---|
| **Placement** — where the session runs | `runner` (a box claims it) · `direct` (the person's own Claude Code) | `Turn.origin` (`local` = direct) |
| **Oversight** — who reviews what it does | `manual` (reviewed) · `auto` (unreviewed) | `Turn.turn_mode`; `auto` only for an agent admin who asks (`harness/local.py::mode_for`) |

**Three kinds of credential, each with one fixed meaning:**

| Kind | What it is | Runner | Direct |
|---|---|---|---|
| **Agent-only** | The agent's canopy login, its 1Password service-account key | The agent's | **Never leaves canopy** to a person's machine (`authority`) |
| **Agent's vault secrets** | Values in the agent's own vault | The agent's | The agent's, as far as the person's role may read the vault. No extra step. |
| **Delegated** | A person's account lent to the agent (their GitHub) | The agent's, sealed to the box | **The person's own.** The lent copy never travels; the person is already themselves, and canopy only mediates where it is asked to (below). |

So direct mode is not "agent credentials never". The agent's own secrets work in both placements; the only things that change with placement are the two kinds above that hold the agent's *authority over a person* — its login and a lent account.

## Step-up has one meaning

**A step-up is crossing from the agent's identity to the person's.** It is the passkey touch that already exists for an act-as-you permission (`ActAsGrant` on canopy-web, `HostGrant` on a connected site): 30 minutes from the touch, never extended, dead with the conversation. Nothing else in a direct session asks for one.

- Using the agent's own credentials, in either placement, needs none.
- Acting as the person where **canopy mediates** (canopy-web itself, a connected site's tools) needs the grant.
- Acting as the person where **canopy does not mediate** (their own `gh`, their own browser login on their own machine) needs nothing, because it is simply them; canopy cannot add a gate there and does not pretend to.
- The power user is not a case. A runner owner using the agent directly, or an admin reading on their own laptop, gets the same answer as anyone: the agent's identity until they step up.

## What changes in the code

1. **One answer per (placement, kind), in one place.** Today the answer is spread over `credential_resolve.resolve` (`via` branches), `LICENSE_WITHHELD_*`, `accounts.withheld_payload` and `_classify_read`. Add `apps/agents/credentials/authority.py` with `stance(placement, kind) -> Stance(hands_out, why)`; `resolve` asks it, and the tests assert the whole table through it.
2. **Retire the owner-at-own-laptop special case.** `_classify_read` reads an owner's own EMDASH laptop as `admin` rather than `runner`, and `credential_access` ranks `runner` > `admin` > `license`. In a local turn every person, admins included, is a `license` read. Admin-only management reads stay in the browser behind the existing passkey step-up.
3. **Make the crossing visible.** A direct session says which identity each kind of call is using ("as <agent>", or "as you until 14:32"), from the caller envelope, rather than leaving it to the person to infer from the withheld list. `delegated_withheld` becomes an instruction ("use your own GitHub"), not a bare list.
4. **Auto and act-as-you.** An act-as-you grant may not be requested by an `auto` turn at `write` scope: an unreviewed turn holding a person's write authority is the one combination with no human anywhere in it. `read` is allowed. A manual turn is unaffected, since each action is already the person's call. Enforced in `act_as` (the request is refused, saying why) and pinned in `tests/test_act_as_you_on_canopy.py`.
5. **UI.** Settings → People → Local use and `AgentHowItRuns` describe the two axes and the three kinds in these words; the word "license" stays as the record of who uses an agent directly, and is not part of the vocabulary a person needs.

## Not changing

`access.decide` as the one rule for what a person may make an agent do; editors' always-`manual`; `acts_as` / `CANOPY_ACTS_AS` (a test tool for running the non-admin path, not a mode); the delegated-credential sealing rules (`accounts.delegated_devices`); the 30-minute, never-extended grant.

## Decisions I made without asking, and their cost if wrong

- **Agent's vault secrets stay readable in direct mode by role, with no step.** If this should instead need the touch, the cost is a passkey prompt at the start of every direct session; it would be one more `authority` row, not a redesign.
- **Auto + write act-as-you is refused outright**, not made admin-only. Loosening it later is easy; tightening it after people rely on it is not.

## Order of work

(2) and (1) first, together, since (2) is what the table makes obvious. (4) is independent and small. (3) and (5) follow once the envelope field exists. Each is its own PR; none needs a migration.
