# Approval requests — a typed ask for one person's yes, run by fixed code

An **approval request** asks a person to approve ONE specific action. It is an explicit record
(`ApprovalRequest`, `apps/approvals/models.py`) with a `kind` from a closed registry; approving
runs that kind's hard-coded handler, as the approver. No model is involved in approving or
declining. The one exception is the kind that says so, `agent.run_turns`, which starts agent turns.

Code: `apps/approvals` (framework tier, L2 features), `apps/slack/approvals.py` (the Slack DM and
its buttons). Page: `/w/:workspace/approvals/:id` (`frontend/src/pages/ApprovalPage.tsx`).

## Invariants

- **The preview is the server's, not the requester's.** `Kind.describe(params)` renders the title,
  plan and scopes from the same typed params the handler will run. The requester's own words
  (`reason`, `evidence`) are stored and shown apart, labelled as theirs. A request cannot say one
  thing and do another.
- **The authority is the approver's own.** Each kind's `may_approve(user, params, described)` is the
  same rule the real route enforces, and `execute` runs as the approver. The person a request is
  about never approves it (`Described.about_email`). `services.create` refuses a request nobody could
  approve (422).
- **Hard-coded, not routed to a model.** A kind is Python (`apps/approvals/kinds/*.py`): typed
  params, `describe`, `may_approve`, `execute`, registered with `@register`. The registry is
  `GET /api/approvals/kinds`.
- **Decided once.** `services.decide` locks the request row; a second decision is a 409 naming who
  got there first. An approved request whose handler refuses ends `failed` with nothing changed
  (`kinds.ExecError`). Statuses: `ApprovalRequest.STATUSES`; `ApprovalEvent` is the audit trail.
- **A link never decides by itself.** The email's buttons open the request with the choice made, to
  confirm (mail scanners open every link: `channel_email.py`). A Slack click decides, but only from
  the DM canopy sent THAT person for THAT request, matched on the message ts
  (`slack/approvals.py::handle_click`). A decision carries no passkey step-up.

## Delivery (`delivery.deliver`)

Who is told is `Kind.recipients`: the people accountable, not everyone who could approve (by
default the workspace's own direct admins and owners, not the parent organisation's owners
reaching it by inheritance; an agent kind overrides it), capped at `delivery.MAX_RECIPIENTS`.
Anyone else with the authority still sees the request in `GET /api/approvals/` and can decide it.
Once per person per channel (push and Slack first, email only to someone neither reached);
`approvals.backstop` (`apps/approvals/apps.py`) delivers a pending request nobody was told about
and expires the undecided. A request in a workspace below another uses the organisation's Slack.
After any decision every Slack copy is rewritten.

A pending request is also an alert of kind `approval_request` (`alerts_approval.py`), scoped by its
kind, with the kind's Approve / Decline as server actions (`via: alert`); a person who could not
approve it has no actions and so does not see it.
