# Approval requests — a typed ask for one person's yes, run by fixed code

An **approval request** asks a person to approve ONE specific action. It is an explicit record
(`ApprovalRequest`) with a `kind` from a closed registry; approving runs that kind's hard-coded
handler, as the approver. No model is involved in approving or declining. The one exception is
the kind that says so — `agent.run_turns` — which starts the listed agent turns.

Code: `apps/approvals` (framework tier, L2 features), `apps/slack/approvals.py` (the Slack DM and
its buttons). Page: `/w/:workspace/approvals/:id` (`frontend/src/pages/ApprovalPage.tsx`).

## Why it is built this way

- **The preview is the server's, not the requester's.** `describe(params)` renders the title, the
  plan ("If you approve, this happens") and the scopes from the same typed params the handler will
  run. The requester's own words (`reason`, `evidence`) are stored and shown apart, labelled as
  theirs. A request cannot say one thing and do another.
- **The authority is the approver's own.** Each kind's `may_approve(user, params)` is the same rule
  the real route enforces (`members.manage` and the role ceiling for adding a member; the agent's
  owner or a workspace owner for agent admins). The handler runs as the approver. The person the
  request is about never approves it. Nothing is filed that nobody could approve.
- **Hard-coded, not routed to a model.** A kind is Python (`kinds/*.py`): typed params, `describe`,
  `may_approve`, `execute`. A new kind is a new module with `@register`.
- **Decided once.** `services.decide` locks the request; a second decision is a 409 naming who got
  there first.

## The kinds (`GET /api/approvals/kinds`)

| Kind | Params | Who can approve | What runs |
|---|---|---|---|
| `workspace.member.add` | `workspace, email, role` | an admin or owner of the workspace (only an owner can add an admin or owner) | adds the person (directly if they already have an account in the organisation, else emails an invitation) |
| `workspace.member.set_role` | `workspace, email, role` | an admin or owner (only an owner can touch an admin or owner) | sets the member's role (the last owner cannot be demoted) |
| `agent.admin.add` | `agent, email` | the agent's owner, or an owner of its workspace | makes a workspace member an admin of the agent |
| `agent.run_turns` | `agent, prompts[1..3]` | anyone who can start turns for the agent | starts one turn per prompt, as the approver (the prompts are shown in full) |

## Filing

`POST /api/approvals/` `{kind, params, reason?, evidence?[{quote, who, source_url}], source_url?,
for_email?, expires_in_hours?, idempotency_key?}` — as an agent's own login or a person who can
work in the request's workspace. 422 says what is wrong with the kind or params, or that nobody can
approve it. Replaying `idempotency_key` returns the original. `for_email` sends it to one named
person (who must be able to approve it). Default expiry 7 days.

## Status and history

`pending → done | failed | declined | cancelled | expired`. `failed` means it was approved but the
handler refused (the person is already there, the target is gone, the last owner…): the reason is
on the card and nothing was changed. `ApprovalEvent` is the audit trail (created, delivered,
approved/declined, executed/failed, cancelled, expired), returned as `history`.

## Who is told, and how (`delivery.deliver`)

The people who could approve it (capped at 5), or the one named. Once per person per channel, on
commit of the filing; a 60-second backstop (`approvals.backstop`) delivers any pending request from
the last 6 hours nobody was told about, and expires the ones nobody decided.

| Channel | What | One tap |
|---|---|---|
| **Slack DM** | the plan, the requester's words, and **Approve / Decline** buttons | yes: the click decides |
| **Web Push** | what is asked and by whom; opens the request | opens it |
| **Email** | the same, with buttons that open the request with your choice made | opens it to confirm (a link never decides by itself) |

A person with a push or a Slack DM is not also emailed. A request in a workspace below another
(Connect under Dimagi) uses the organisation's Slack. Settings → Account → *Approval cards* turns
the sending off for a person (`NotificationPreference.approval_cards`).

A Slack click is accepted only from the DM canopy sent **that person for that request** (matched on
the message ts). After any decision every Slack copy is rewritten ("Approved by X via Slack and done").

## Routing like an alarm (`alerts_approval`)

A pending request is also an alert of kind `approval_request`, scoped by its kind
(`workspace:<slug>`, `agent:<slug>`), so it appears wherever alerts show for those scopes. The
buttons are the kind's Approve / Decline as server actions (`via: alert`); a person who could not
approve it has no actions and so does not see it. It resolves when the request is closed.

## API

`GET /api/approvals/kinds` · `GET /api/approvals/` (what you could approve; `status=all`,
`scope=…`) · `POST /api/approvals/` · `GET /api/approvals/{id}` · `POST /api/approvals/{id}/decide
{decision, comment}` · `POST /api/approvals/{id}/cancel`; each also an MCP tool.

## Not here (on purpose)

No passkey step-up on a decision yet (`act-as` and sign-offs have the pattern), and no reminders.
Vault access and runner approvals are not kinds yet; each is one more module in `kinds/`.
