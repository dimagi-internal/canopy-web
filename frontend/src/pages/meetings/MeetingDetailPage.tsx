// /w/:workspace/meetings/:id — one meeting: everything saved on it, whichever tool made it, the
// project it's in, and the conversation with the agent helping with it.
import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { deleteMeeting, getMeeting, KIND_LABELS, startMeetingFollowUp, updateMeeting, type Meeting } from '@/api/meetings'
import { errorText } from '@/api/problem'
import { listMyAgents } from '@/api/agents'
import { listProjects } from '@/api/work'
import { useScopedPath } from '@/lib/scopedLinks'

export default function MeetingDetailPage(): JSX.Element {
  const { workspace = '', id = '' } = useParams()
  const navigate = useNavigate()
  const scoped = useScopedPath()
  const [meeting, setMeeting] = useState<Meeting | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [projects, setProjects] = useState<{ address: string; name: string }[]>([])
  const [agents, setAgents] = useState<{ slug: string; name: string }[]>([])
  const [agent, setAgent] = useState('')
  const [ask, setAsk] = useState('')

  const load = useCallback(() => {
    getMeeting(id)
      .then(setMeeting)
      .catch((err: unknown) => setError(errorText(err, 'Could not load this meeting')))
  }, [id])
  useEffect(() => load(), [load])

  useEffect(() => {
    listProjects('open', workspace)
      .then((list) => setProjects(list.map((p) => ({ address: `${p.workspace}/${p.handle}`, name: p.name }))))
      .catch(() => setProjects([]))
    listMyAgents()
      .then((page) => setAgents(page.items.map((a) => ({ slug: a.slug, name: a.name || a.slug }))))
      .catch(() => setAgents([]))
  }, [workspace])

  if (error) return <p role="alert" className="text-[13px] text-destructive">{error}</p>
  if (!meeting) return <p className="text-[13px] text-muted-foreground">Loading…</p>

  const move = (project: string) => {
    updateMeeting(meeting.id, { project })
      .then(setMeeting)
      .catch((err: unknown) => setActionError(errorText(err, 'Could not move this meeting')))
  }
  const remove = () => {
    if (!window.confirm('Delete this meeting and everything saved on it? Anything its agent already read stays in that conversation.')) return
    deleteMeeting(meeting.id)
      .then(() => navigate(scoped('/meetings')))
      .catch((err: unknown) => setActionError(errorText(err, 'Could not delete this meeting')))
  }
  const help = () => {
    startMeetingFollowUp(meeting.id, { agent, ask })
      .then((res) => {
        setAsk('')
        load()
        window.location.assign(res.session_url)
      })
      .catch((err: unknown) => setActionError(errorText(err, 'Could not ask for help')))
  }

  return (
    <div className="max-w-4xl space-y-4 pb-28" data-testid="meeting-detail">
      <Link to={scoped('/meetings')} className="text-[12px] text-muted-foreground hover:underline">
        ← Meetings
      </Link>
      <div>
        <h1 className="text-xl font-semibold">{meeting.title || 'Untitled meeting'}</h1>
        <p className="text-[13px] text-muted-foreground">
          {new Date(meeting.started_at ?? meeting.created_at).toLocaleString()}
          {meeting.attendees.length > 0 && ` · ${meeting.attendees.map((a) => a.name || a.email).join(', ')}`}
          {!meeting.is_mine && ` · saved by ${meeting.owner}`}
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-lg border border-border bg-card p-4 text-[13px]">
        {meeting.may_change ? (
          <label className="block">
            Who can see it
            <select
              value={meeting.project?.address ?? ''}
              onChange={(e) => move(e.target.value)}
              className="mt-1 block rounded border border-border bg-background px-2 py-1"
            >
              <option value="">Just me</option>
              {meeting.project && !projects.some((p) => p.address === meeting.project?.address) && (
                <option value={meeting.project.address}>Everyone on {meeting.project.name}</option>
              )}
              {projects.map((p) => (
                <option key={p.address} value={p.address}>
                  Everyone on {p.name}
                </option>
              ))}
            </select>
          </label>
        ) : (
          meeting.project && <p>In {meeting.project.name}</p>
        )}
        <label className="block">
          Agent
          <select value={agent} onChange={(e) => setAgent(e.target.value)} className="mt-1 block rounded border border-border bg-background px-2 py-1">
            <option value="">{meeting.agent || 'Project lead'}</option>
            {agents.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block min-w-48 flex-1">
          What do you need?
          <input value={ask} onChange={(e) => setAsk(e.target.value)} className="mt-1 block w-full rounded border border-border bg-background px-2 py-1" />
        </label>
        <Button onClick={help}>Get help</Button>
        {meeting.session_url && (
          <a href={meeting.session_url} className="self-center hover:underline">
            Open the conversation
          </a>
        )}
        {meeting.may_change && (
          <Button variant="outline" onClick={remove}>
            Delete
          </Button>
        )}
      </div>

      {actionError && <p role="alert" className="text-[13px] text-destructive">{actionError}</p>}

      {meeting.meeting_instructions && (
        <div className="rounded-lg border border-border bg-card p-4 text-[13px]">
          <h2 className="font-medium">What this project does with notes</h2>
          <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{meeting.meeting_instructions}</p>
        </div>
      )}

      {meeting.artifacts.map((artifact) => (
        <section key={artifact.id} className="rounded-lg border border-border bg-card p-4 text-[13px]">
          <h2 className="font-medium">
            {KIND_LABELS[artifact.kind] ?? artifact.kind}
            {artifact.source && <span className="font-normal text-muted-foreground"> · {artifact.source}</span>}
          </h2>
          {artifact.source_url && (
            <a href={artifact.source_url} target="_blank" rel="noreferrer" className="text-[12px] hover:underline">
              {artifact.source_url}
            </a>
          )}
          {artifact.text ? (
            <pre className="mt-2 max-h-[32rem] overflow-auto whitespace-pre-wrap font-sans">{artifact.text}</pre>
          ) : (
            <p className="mt-2 text-muted-foreground">Nobody has read this link yet.</p>
          )}
        </section>
      ))}
    </div>
  )
}
