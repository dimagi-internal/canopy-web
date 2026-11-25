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
  (`slack/approvals.py::handle_click`). A decision carries no passkey step-up, except for a kind with `Kind.step_up` (`vault.attach_agent`, which stands in for a route that needs one; `agent.definition.publish`, so a turn acting as an admin never approves an agent's change it was asked for; `agent.aws_role`, because it grants AWS account rights): `services.decide` approves such a kind only when its caller verified the passkey, which only the web page's ceremony does.
- **A passkey kind is approved with a touch made for THAT request, never a recent one.** The page begins
  a WebAuthn assertion for the request and approver (`apps/approvals/api.py::begin_approve_approval`,
  slot `approval:<id>:<user>`) and approves only on its verification (`finish_approve_approval`), in a
  browser, by a person who may approve it. The five-minute step-up window that other passkey routes
  accept (`stepup.require_recent_step_up`) does not approve one: `decide_approval` refuses it with 403
  `passkey_for_this_request`, so a passkey touched for something else a minute ago never carries an
  AWS role or a vault attachment through. This is the same ceremony an act-as-you approval uses
  (`apps/harness/api_act_as.py`).
- **Superseded, not left pending.** When someone with the authority to take a request's action
  takes it directly, the request is closed by `services.supersede`: `cancelled`, result
  `{"superseded": true}`, a comment naming who acted, and no authority asked of them (the route
  already allowed the act). `agent.definition.publish` is the one kind that uses it: publishing
  a draft directly (a person, an act-as agent, or another agent that administers the target)
  closes the draft's open request. That kind is filed only for a draft no agent may publish: an
  agent's draft of itself, or one by an agent that does not administer the target
  ([stored-agents.md](stored-agents.md)).
- **A fenced AWS role is a grant a person approves, never a standing list.** `agent.aws_role` is filed
  only by `POST /api/agents/{slug}/aws-role-requests` (`apps/approvals/api_aws_roles.py`) for a role in
  `settings.CANOPY_AWS_ROLE_AGENTS`; its one param is the server-made `AgentAwsRoleRequest`, so the
  preview (agent, role and ARN, `settings.CANOPY_AWS_ROLE_SUMMARIES`, duration) is the server's. Only an
  owner of the agent's workspace or a canopy superuser may approve it, never an agent's login and never
  an agent's admin as such (`kinds/agent_aws_role.py::AgentAwsRole.may_approve`). Approving makes a grant
  that `aws-credentials` vends under until it ends or is revoked; the rules are
  `apps/agents/api_aws.py`'s module docstring.
- **A passkey kind offers no Approve anywhere but its page.** The card says `step_up` and carries
  `page_url` (`cards.py::build`); in Approve's place the Slack DM, the alert and the email offer a
  link to the request's page (`cards.STEP_UP_APPROVE`), the push opens that page, and the page asks
  for the passkey. Decline needs none and stays on every channel.

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
