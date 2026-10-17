import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import {
  deleteHuddleGroup,
  getHuddleGroup,
  listHuddleGroupRuns,
  type HuddleGroup,
  type HuddleGroupRun,
} from '@/api/huddleGroups'
import { WorkGroupForm } from './WorkGroups'
import { readWindow } from './workGroupModel'

/**
 * One work group: who leads it, which channel, which agents, and every run —
 * its status, what was read, the Slack post and the result.
 */

const STATUS: Record<string, string> = {
  awaiting_confirm: 'waiting for the lead',
  reading: 'reading the channel',
  running: 'working',
  done: 'done',
  skipped: 'skipped',
  expired: 'lapsed',
  failed: 'failed',
}

export function HuddleGroupPage() {
  const { workspace = '', slug = '' } = useParams()
  const navigate = useNavigate()
  const [group, setGroup] = useState<HuddleGroup | null>(null)
  const [runs, setRuns] = useState<HuddleGroupRun[]>([])
  const [error, setError] = useState('')
  const [editing, setEditing] = useState(false)

  const load = useCallback(
    () =>
      Promise.all([getHuddleGroup(slug), listHuddleGroupRuns(slug)])
        .then(([g, r]) => {
          setGroup(g)
          setRuns(r)
        })
        .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e))),
    [slug],
  )
  useEffect(() => {
    void load()
  }, [load])

  const back = <Link to={`/w/${workspace}/huddles`} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>
  if (!group) {
    return (
      <div className="mx-auto max-w-5xl p-6">
        {back}
        {error ? <p className="mt-4 text-sm text-destructive">Couldn’t load this work group: {error}</p> : <div className="mt-4"><WorkbenchSkeleton /></div>}
      </div>
    )
  }
  const remove = async () => {
    if (!window.confirm(`Delete the ${group.name} work group? Its channel comes off ${group.leader_agent}'s reading list.`)) return
    try {
      await deleteHuddleGroup(group.slug)
      navigate(`/w/${workspace}/huddles`)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <div className="mx-auto max-w-5xl p-6">
      {back}
      <header className="mt-3 mb-6 rounded-2xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold">{group.name} <span className="text-[13px] font-normal text-muted-foreground">work group</span></h1>
            {group.topic && <p className="mt-1.5 max-w-[640px] text-[14px] text-foreground-secondary">{group.topic}</p>}
            <p className="mt-2 text-[13px] text-foreground-secondary">
              #{group.channel_name || group.channel_id} · lead {group.lead_email || '—'} · {group.agents.join(', ')}
              {group.mode === 'multi' ? ` · huddle led by ${group.leader_agent}` : ''}
              {' · '}<code>{group.cron}</code> {group.timezone}{group.enabled ? '' : ' · paused'}
            </p>
            {group.project && (
              <p className="mt-1 text-[13px]">
                Project: <Link className="text-primary hover:underline" to={group.project.url}>{group.project.name}</Link>
              </p>
            )}
            <p className="mt-2 text-[12px] text-muted-foreground">
              When it comes due, canopy asks {group.lead_email || 'the lead'} in the channel. The channel is read only
              when they press Confirm there.
            </p>
          </div>
          {group.can_edit && !editing && (
            <div className="flex gap-3 text-[12px]">
              <button type="button" onClick={() => setEditing(true)} className="text-muted-foreground hover:text-primary">Edit</button>
              <button type="button" onClick={remove} className="text-muted-foreground hover:text-destructive">Delete</button>
            </div>
          )}
        </div>
      </header>
      {editing && (
        <WorkGroupForm workspace={workspace} group={group} onDone={(saved) => {
          setEditing(false)
          if (saved) void load()
        }} />
      )}
      {error && <p className="mb-3 text-sm text-destructive">{error}</p>}
      <h2 className="mb-3 text-lg font-semibold">Runs</h2>
      {runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">No runs yet. The first comes when the cadence next fires.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {runs.map((r) => (
            <li key={r.id} className="rounded-xl border border-border bg-card p-3 text-[13px]">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">
                  {new Date(r.slot).toLocaleString(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}
                  {r.manual ? ' (run now)' : ''}
                </span>
                <span className="text-muted-foreground">{STATUS[r.status] ?? r.status}</span>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">
                {readWindow(r)}
                {r.confirmed_by_email ? ` · confirmed by ${r.confirmed_by_email}` : ''}
                {r.note ? ` · ${r.note}` : ''}
              </p>
              <p className="mt-1 flex flex-wrap gap-3 text-[12px]">
                {r.slack_url && <a className="text-primary hover:underline" href={r.slack_url} target="_blank" rel="noreferrer">Slack post</a>}
                {r.huddle_id && <Link className="text-primary hover:underline" to={`/w/${workspace}/huddles/${encodeURIComponent(r.huddle_id)}`}>The huddle</Link>}
                {r.result_url && <a className="text-primary hover:underline" href={r.result_url}>Result</a>}
              </p>
              {r.summary && <p className="mt-1 text-[12px] text-foreground-secondary">{r.summary}</p>}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default HuddleGroupPage
