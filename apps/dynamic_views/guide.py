"""What an author (usually an agent, over MCP) needs to write a view cold.

Served by `GET /api/dynamic-views/guide/` together with the catalog. Keep it in
step with the frame runtime (`frontend/src/dynamicViews/frame/`) — the props,
the `canopy` API and the kit named here are exactly what the frame provides.
"""
from __future__ import annotations

CONTRACT = """\
A dynamic view is JSX that defines `function View(props)`. canopy compiles it and runs it in a
sandboxed frame inside the workspace (or project) page, and draws what it returns.

PROPS
- props.data[alias]   the result of each declared source, read before the first draw.
                      A source that failed arrives as {"error": "<why>"} under its alias.
- props.scope         {workspace: {slug, name}, project: {handle, name} | null,
                       view: {id, slug, title, version}, viewer: {email, name}}
- props.canopy        the only way out of the frame:
    canopy.read(model, {params?, path?}) -> Promise<json>   any catalog model, on demand
    canopy.refresh()                     re-read every declared source (props.data updates)
    canopy.navigate(path)                open a canopy page: "/tasks/CON-12", "/projects/<handle>"
                                         (relative to the workspace) or "/w/<ws>/..."
    canopy.runAgentTask({agent, name, input, output_schema, instructions?, timeout_s?, routing?,
                         onStatus?}) -> Promise<result>
                                         give an agent a task the view DECLARES (AGENT TASKS below)
                                         and resolve with its result; rejects with the reason when
                                         it fails, is cancelled, times out or the viewer may not
                                         give that agent tasks
    canopy.connection.request({provider, method, path, query?, body?, as?, agent?}) -> Promise<{status, body}>
                                         a Salesforce call the view declares (see SALESFORCE CALLS)
    canopy.connection.begin() -> Promise<{expires_at, hard_limit_at, writes}>
                                         open this click's window before any write (see SALESFORCE CALLS)
    canopy.agentTask(id) -> Promise<task>  re-read a task this view started (status, error, ...)
    canopy.googleAccess.start({agent, account?, services?: ["gmail"], minutes?: 60, purpose?})
                                         -> Promise<{ok, account, until}>  the VIEWER lends an agent
                                         the view declares tasks for their own Google account for
                                         1-60 minutes, without leaving the page: canopy explains it
                                         in its own dialog and opens Google in a small window. No
                                         refresh token: it ends by itself (read `my_connected_accounts`
                                         for `session_until`). Rejects when they cancel.
    canopy.googleAccess.end({agent, account?})  end it now
    canopy.connections.connect({agent, provider, minutes?, ownerTurnsOnly?})
                                         the VIEWER connects their own account and lends it to an agent
                                         the view declares tasks for: this page leaves for the provider's
                                         sign-in and canopy's share screen (agent ticked, limits shown,
                                         their passkey) and comes back here. `minutes` 5-10080 ends it by
                                         itself and revokes it; `ownerTurnsOnly` keeps it to turns they
                                         started. Read `my_connections` for its state and `expires_at`.
    canopy.connections.connect({agent, provider, temporary: true})
                                         the same, for about an hour and with no passkey or share screen: the
                                         viewer signs in at the provider and comes back here, and the agent
                                         gets the token only on turns they start. It can't be renewed, so they
                                         sign in again when it runs out. Google only. Read `temporary` in
                                         `my_connections` (`{id, provider, workspace, agent, account_label,
                                         expires_at}`); disconnect ends it with the same call below.
    canopy.connections.outcome() -> Promise<{provider, status, reason} | null>  how the connect that
                                         brought them back here went (a failure's `reason` is a code; for
                                         `no_vault_key`, canopy.navigate("/settings#vault-key") sets one up)
    canopy.connections.disconnect({id, agent}) -> Promise<{outcome}>  end one the viewer lent `agent`:
                                         a machine, or canopy-web, revokes it at the provider
- Plain <a href="/tasks/CON-12"> links work too: the frame routes them through navigate.

SOURCES (declared on the view, read for it as the viewer before it draws; `example_sources` beside
this contract are the example's)
  [{"alias": "open", "model": "tasks", "params": {"category": "started,unstarted", "limit": 200}},
   {"alias": "proj", "model": "project", "path": {"handle": "unga-2026"}}]
- `model` is a catalog name; `params` only the parameters the catalog lists for it.
- `path` fills the model's path parameters. On a PROJECT view, `handle` may be omitted: it is the
  view's own project. `{workspace}` is always the view's own workspace and is never yours to set.
- Each model returns exactly what its REST route returns (see /api/docs/ for the schema): most lists
  are a plain JSON array (tasks, projects, agents, members); a few are a page ({"items": [...]}).
  Read one with canopy.read first if unsure of its shape.

AGENT TASKS (the one thing a view may make happen; docs/architecture/agent-tasks.md)
- Declare each task the code may start on the view: "agent_tasks": [{"agent": "bailey",
  "name": "gmail-triage.review"}]. The agent must be in the view's workspace and one its author may
  give tasks to. canopy refuses any runAgentTask whose {agent, name} is not declared.
- The task runs AS THE VIEWER: canopy starts it with the viewer's own session, so the server's own
  gate decides — a workspace viewer (or anyone the agent does not take work from) gets the refusal
  as a rejected promise. Show its message.
- `output_schema` is the JSON Schema of the result; the agent's answer is validated against it, so
  the promise resolves with JSON of exactly that shape. `input` is any JSON (at most 1 MiB).
  `timeout_s` 30-3600 (default 900). `name` names the agent's skill (`gmail-triage.review`).
- `onStatus(task)` is called as the task moves: {id, status: "queued"|"running"|"done"|"failed"|
  "cancelled", error, ...}; keep task.id to show progress. Leaving the page cancels a running task.
- Example:
    const [rows, setRows] = useState(null), [status, setStatus] = useState("")
    async function review() {
      try {
        const out = await canopy.runAgentTask({
          agent: "bailey", name: "gmail-triage.review", input: { count: 50 },
          output_schema: { type: "object", required: ["rows"], properties: { rows: { type: "array" } } },
          onStatus: (t) => setStatus(t.status),
        })
        setRows(out.rows)
      } catch (e) { setStatus(e.message) }
    }

GLOBALS (no imports — the code is not a module)
- React, and its hooks as bare names: useState, useEffect, useMemo, useCallback, useRef.
- The kit, styled in canopy's look:
    <Card title? actions?>…</Card>            a bordered panel
    <Stat label value hint? tone?/>          a big number; tone: "default"|"success"|"warning"|"destructive"
    <Table rows columns={[{key, label, render?(row), align?}]} empty?/>   dense, sortable by header
    <Badge tone?>text</Badge>
    <BarChart data={[{label, value}]} height?/>   a simple horizontal bar chart
    <Link to="/tasks/CON-1">text</Link>
    <Empty>nothing here</Empty>, <Loading/>
    Grid({cols, children}) and Stack({gap, children}) for layout.
    Form controls (a view rarely needs them; a workspace app's pages write with them):
    <Form onSubmit>…</Form>                  NOT <form>: the sandbox has no allow-forms, so a <form>
                                             never submits. Checks fields' own validity, then onSubmit().
    <Button tone? size? type?="submit" onClick? confirm?/>   tone: "default"|"primary"|"destructive";
                                             confirm="Send it?" asks inline first. NEVER window.confirm/alert/prompt:
                                             the sandbox has no allow-modals, so confirm() is silently false.
    <Input label? value onChange={(v) => …} type? required?/>, <TextArea …/>,
    <Select label? value onChange options={["a", {value, label}]}/>   onChange gets the VALUE.
    Only on a workspace app's page (they read the app; a view has none): FileInput, FieldInput,
    FieldInputs, FieldValues, Slot — see the app guide (GET /api/workspace-apps/{ws}/{key}/guide).
- Tailwind utility classes that canopy itself uses, and canopy's semantic colours:
  bg-card, bg-muted, text-foreground, text-muted-foreground, border, text-success,
  text-warning, text-destructive. Never raw palette colours (stone-*, red-*).

SALESFORCE CALLS
- Declare the calls the code may make on the view: "connections": [{"provider": "salesforce",
  "as": "viewer", "reads": [{"method": "GET", "path": "query"}], "writes": [{"method": "PATCH",
  "path": "sobjects/Lead/*"}]}]. `as` is "viewer" (the viewer's own connection) or "shared" (an
  agent's shared connection, with "agent": "<slug>" naming an agent in the workspace).
- A path is relative to the Salesforce API root, in plain form: no dots, queries or encoding. `*`
  stands for exactly one segment. A write names its object exactly (sobjects/Lead/*, never
  sobjects/*/*), and a flow or Apex write names the exact flow or class. People, permissions and
  code objects (User, Profile, PermissionSet, ApexClass, ...) are never writable. Running a report
  (POST analytics/reports/*) is a read.
- Every line is checked on save against the allowlist and refused with a sentence naming it; at most
  5 entries and 50 lines each.
- Call: canopy.connection.request({provider: "salesforce", method, path, query?, body?, as?, agent?})
  -> Promise<{status, body}>. `status` is Salesforce's own, also when it refused the call (a 400 is a
  resolved answer; look at it). canopy refuses in a sentence (not connected, not declared, over the
  limit, ...) as a rejected promise: show its message. Query values are text. Not connected, the
  message says where ("Connect Salesforce in Settings → Connections.") and the rejection also has
  e.connect_url. A frame can't go there itself: offer a button that calls canopy.navigate(e.connect_url),
  which asks the viewer and opens it in a new tab.
- Calls wait their turn: at most 8 at once from a page, the rest first come, first served. Fire as many
  reads in parallel as you like; a call that waits a full minute rejects with a sentence.
- A READ can run any time. A WRITE must start from a click on this screen: call it straight from an
  onClick handler, never on load or on a timer. The page checks for the click itself and rejects
  "This needs a click." otherwise. The click only counts for about 5 seconds, so the write must be
  the first thing the handler does: a write that comes after several seconds of awaits in the same
  handler is refused. One click opens up to 10 minutes and 500 writes within what the screen declared;
  later writes in that window need no new click. The window closes 120 seconds after the last write
  (reads don't count), so keep writing or click again. To read and dedup before the first write, call
  canopy.connection.begin() first thing in the onClick instead: it opens the window and resolves
  {expires_at, hard_limit_at, writes} (what can be written now), and the first write must then come
  within 120 seconds. A view that has not been saved yet cannot call ("Save this view to use
  connections.").
- The answer is {status, body, text?}: `body` is Salesforce's JSON; when its answer was not JSON,
  `body` is null and `text` holds the start of it.
- Example, a read and a click-driven write:
    const [leads, setLeads] = useState(null);
    useEffect(() => {
      canopy.connection.request({provider: "salesforce", method: "GET", path: "query",
        query: {q: "SELECT Id, Name, Status FROM Lead WHERE OwnerId = '005...' LIMIT 20"}})
        .then((r) => setLeads(r.body.records)).catch((e) => setError(e.message));
    }, []);
    <Button onClick={() => canopy.connection.request({provider: "salesforce", method: "PATCH",
      path: "sobjects/Lead/" + lead.Id, body: {Status: "Contacted"}})
        .then(() => canopy.refresh()).catch((e) => setError(e.message))}>Mark contacted</Button>

RULES (the frame enforces them; breaking one fails, it is not "best effort")
- No network: fetch, XHR, WebSocket and remote images are blocked by the frame's CSP.
  Everything comes through props.data, canopy.read, canopy.runAgentTask or canopy.connection.request.
- No writes, except starting an agent task the view declares and the Salesforce writes it declares
  (and only from a click): the catalog is read-only, canopy.runAgentTask is refused for any {agent,
  name} not in the view's agent_tasks, and canopy.connection.request for any call not in its
  connections.
- No storage, cookies or access to the page around the frame.
- What a view shows, its viewers may see — and so may its author. Read only what the view needs.

STYLE
- Dense and readable: tables, not cards of cards. Lead with the number or the list that matters.
- Handle empty and error states for every source.
"""

EXAMPLE = """\
function View({ data, scope, canopy }) {
  if (data.open?.error) return <Card title="Open work"><Empty>{data.open.error}</Empty></Card>;
  const tasks = Array.isArray(data.open) ? data.open : [];
  const byAssignee = {};
  for (const t of tasks) {
    const who = t.assigned || t.assignee_email || "Unassigned";
    byAssignee[who] = (byAssignee[who] || 0) + 1;
  }
  const bars = Object.entries(byAssignee)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);
  return (
    <Stack gap={4}>
      <Grid cols={3}>
        <Stat label="Open tasks" value={tasks.length} />
        <Stat label="People with work" value={bars.length} />
        <Stat label="Scope" value={scope.project ? scope.project.name : scope.workspace.name} />
      </Grid>
      <Card title="Open tasks by assignee"><BarChart data={bars} /></Card>
      <Card title="Open tasks" actions={<button className="text-sm underline" onClick={canopy.refresh}>Refresh</button>}>
        <Table
          rows={tasks}
          columns={[
            { key: "key", label: "Key", render: (t) => <Link to={`/tasks/${t.key}`}>{t.key}</Link> },
            { key: "title", label: "Title" },
            { key: "status", label: "Status", render: (t) => <Badge>{t.status}</Badge> },
          ]}
          empty="No open tasks."
        />
      </Card>
    </Stack>
  );
}
"""

EXAMPLE_SOURCES = [
    {"alias": "open", "model": "tasks", "params": {"category": "started,unstarted", "limit": 200}},
]
