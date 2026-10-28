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
    canopy.agentTask(id) -> Promise<task>  re-read a task this view started (status, error, ...)
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
- Tailwind utility classes that canopy itself uses, and canopy's semantic colours:
  bg-card, bg-muted, text-foreground, text-muted-foreground, border, text-success,
  text-warning, text-destructive. Never raw palette colours (stone-*, red-*).

RULES (the frame enforces them; breaking one fails, it is not "best effort")
- No network: fetch, XHR, WebSocket and remote images are blocked by the frame's CSP.
  Everything comes through props.data, canopy.read or canopy.runAgentTask.
- No writes, except starting an agent task the view declares: the catalog is read-only, and
  canopy.runAgentTask is refused for any {agent, name} not in the view's agent_tasks.
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
