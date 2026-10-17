import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listAgents } from '@/api/agents'
import {
  createHuddleGroup,
  listHuddleGroups,
  updateHuddleGroup,
  type HuddleGroup,
} from '@/api/huddleGroups'
import { listMembers } from '@/api/workspaces'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'
import { isAdmin } from './workGroupModel'

/**
 * Work groups on the Huddles page: a lead, a Slack channel, the agents that work
 * it and a cadence. Each time it comes due canopy asks the lead in the channel;
 * the channel is read only when the lead presses Confirm there.
 */

export function WorkGroups({ workspace }: { workspace: string }) {
  const role = useWorkspaceRole(workspace)
  const [rows, setRows] = useState<HuddleGroup[] | null>(null)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<HuddleGroup | 'new' | null>(null)

  const load = useCallback(
    () =>
      listHuddleGroups()
        .then(setRows)
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e))),
    [],
  )
  useEffect(() => {
    void load()
  }, [load, workspace])

  return (
    <section aria-label="Work groups" className="mb-10">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">Work groups</h2>
        {isAdmin(role) && editing === null && (
          <button
            type="button"
            onClick={() => setEditing('new')}
            className="rounded-md border border-border px-3 py-1 text-[13px] hover:border-primary/50 hover:text-primary"
          >
            New work group
          </button>
        )}
      </div>
      <p className="mb-3 text-[13px] text-muted-foreground">
        A lead, a Slack channel and the agents that work it. When one comes due, canopy asks the lead in the
        channel; nothing there is read until they press Confirm.
      </p>
      {error && <p className="text-sm text-destructive">Couldn’t load work groups: {error}</p>}
      {editing !== null && (
        <WorkGroupForm
          workspace={workspace}
          group={editing === 'new' ? null : editing}
          onDone={(saved) => {
            setEditing(null)
            if (saved) void load()
          }}
        />
      )}
      {rows !== null && rows.length === 0 && editing === null && (
        <p className="text-sm text-muted-foreground">No work groups yet.</p>
      )}
      {rows !== null && rows.length > 0 && (
        <ul className="flex flex-col gap-2">
          {rows.map((g) => (
            <li key={`${g.workspace}/${g.slug}`} className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card p-3">
              <div className="min-w-0">
                <Link to={`/w/${workspace}/huddles/groups/${g.slug}`} className="font-medium text-foreground hover:text-primary">
                  {g.name}
                </Link>
                {!g.enabled && <span className="ml-2 text-[11px] text-muted-foreground">(paused)</span>}
                <p className="text-[12px] text-muted-foreground">
                  #{g.channel_name || g.channel_id} · lead {g.lead_email || '—'} · {g.agents.join(', ')}
                  {g.mode === 'multi' ? ` · led by ${g.leader_agent}` : ''} · <code>{g.cron}</code> {g.timezone}
                  {g.last_run_status ? ` · last run ${g.last_run_status.replace('_', ' ')}` : ''}
                </p>
              </div>
              {g.can_edit && editing === null && (
                <button type="button" onClick={() => setEditing(g)} className="text-[12px] text-muted-foreground hover:text-primary">
                  Edit
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <label className="flex flex-col gap-1 text-[13px]">
      <span className="font-medium text-foreground">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  )
}

const input = 'rounded-md border border-border bg-background px-2 py-1 text-[13px]'

export function WorkGroupForm({ workspace, group, onDone }: {
  workspace: string
  group: HuddleGroup | null
  onDone: (saved: boolean) => void
}) {
  const [slug, setSlug] = useState(group?.slug ?? '')
  const [name, setName] = useState(group?.name ?? '')
  const [topic, setTopic] = useState(group?.topic ?? '')
  const [channel, setChannel] = useState(group ? `#${group.channel_name || group.channel_id}` : '')
  const [lead, setLead] = useState(group?.lead_email ?? '')
  const [agents, setAgents] = useState<string[]>(group ? [...group.agents] : [])
  const [leader, setLeader] = useState(group?.leader_agent ?? '')
  const [cron, setCron] = useState(group?.cron ?? '0 9 * * 1')
  const [tz, setTz] = useState(group?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? 'UTC')
  const [enabled, setEnabled] = useState(group?.enabled ?? true)
  const [allAgents, setAllAgents] = useState<string[]>([])
  const [emails, setEmails] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    listAgents({ limit: 200 }).then((p) => setAllAgents(p.items.map((a) => a.slug))).catch(() => undefined)
    listMembers(workspace).then((m) => setEmails(m.map((x) => x.email))).catch(() => undefined)
  }, [workspace])

  const toggle = (s: string) => setAgents((a) => (a.includes(s) ? a.filter((x) => x !== s) : [...a, s]))
  const multi = agents.length > 1

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    const body = {
      name, topic, channel, lead_email: lead, agents, leader_agent: multi ? leader : agents[0] ?? '',
      cron, timezone: tz, enabled,
    }
    try {
      if (group) await updateHuddleGroup(group.slug, body)
      else await createHuddleGroup({ ...body, slug })
      onDone(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={save} aria-label={group ? 'Edit work group' : 'New work group'}
      className="mb-4 grid gap-3 rounded-xl border border-border bg-card p-4 sm:grid-cols-2">
      <Field label="Name"><input className={input} value={name} onChange={(e) => setName(e.target.value)} required /></Field>
      <Field label="Slug" hint="Its address: /huddles/groups/<slug>">
        <input className={input} value={slug} onChange={(e) => setSlug(e.target.value)} disabled={!!group} required
          pattern="[a-z0-9][a-z0-9\-_]*" />
      </Field>
      <div className="sm:col-span-2">
        <Field label="Topic" hint="The work group's purpose, in one paragraph.">
          <textarea className={input} rows={3} value={topic} onChange={(e) => setTopic(e.target.value)} />
        </Field>
      </div>
      <Field label="Slack channel" hint="#channel-name or its id (C…). The canopy app must be in it.">
        <input className={input} value={channel} onChange={(e) => setChannel(e.target.value)} required />
      </Field>
      <Field label="Lead" hint="Must be in this workspace and in the channel. Only they can confirm a run.">
        <input className={input} type="email" list="work-group-members" value={lead}
          onChange={(e) => setLead(e.target.value)} required />
        <datalist id="work-group-members">{emails.map((m) => <option key={m} value={m} />)}</datalist>
      </Field>
      <div className="sm:col-span-2">
        <Field label="Agents" hint="One agent works the group's project; several hold a huddle.">
          <div className="flex flex-wrap gap-2">
            {allAgents.map((s) => (
              <label key={s} className="inline-flex items-center gap-1 text-[12px]">
                <input type="checkbox" checked={agents.includes(s)} onChange={() => toggle(s)} /> {s}
              </label>
            ))}
          </div>
        </Field>
      </div>
      {multi && (
        <Field label="Leader" hint="Runs the huddle; its login must be an owner or admin of every other agent.">
          <select className={input} value={leader} onChange={(e) => setLeader(e.target.value)} required>
            <option value="">Choose…</option>
            {allAgents.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
      )}
      <Field label="Cadence (cron)" hint="e.g. 0 9 * * 1 — Mondays at 9">
        <input className={input} value={cron} onChange={(e) => setCron(e.target.value)} required />
      </Field>
      <Field label="Timezone"><input className={input} value={tz} onChange={(e) => setTz(e.target.value)} required /></Field>
      <label className="inline-flex items-center gap-2 text-[13px]">
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Enabled
      </label>
      {error && <p role="alert" className="text-[13px] text-destructive sm:col-span-2">{error}</p>}
      <div className="flex gap-2 sm:col-span-2">
        <button type="submit" disabled={busy || agents.length === 0}
          className="rounded-md bg-primary px-3 py-1 text-[13px] text-primary-foreground disabled:opacity-50">
          {busy ? 'Saving…' : group ? 'Save' : 'Create'}
        </button>
        <button type="button" onClick={() => onDone(false)} className="rounded-md px-3 py-1 text-[13px] text-muted-foreground">
          Cancel
        </button>
      </div>
    </form>
  )
}
