// Paste the notes, drop a file, or paste a link: the one way in for "here's a meeting, help me
// with it". Text that is only a link is left for the agent to read.
import { useEffect, useState, type ChangeEvent, type FormEvent, type JSX } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button, Textarea } from 'canopy-ui/ui'
import { listMyAgents } from '@/api/agents'
import { createMeeting, linkOnly, linkSource } from '@/api/meetings'
import { errorText } from '@/api/problem'
import { useScopedPath } from '@/lib/scopedLinks'

const TEXT_FILE = /\.(txt|md|vtt|srt|text)$/i

export function AddMeeting({
  workspace,
  project,
  projectAgent,
  projects,
  onSaved,
}: {
  workspace: string
  /** Set when this is drawn on a project's page: the meeting goes there. */
  project?: string
  projectAgent?: string
  /** The projects to choose from when it isn't. */
  projects?: readonly { address: string; name: string }[]
  onSaved?: () => void
}): JSX.Element {
  const navigate = useNavigate()
  const scoped = useScopedPath()
  const [text, setText] = useState('')
  const [title, setTitle] = useState('')
  const [kind, setKind] = useState('transcript')
  const [ask, setAsk] = useState('')
  const [target, setTarget] = useState(project ?? '')
  const [agents, setAgents] = useState<{ slug: string; name: string }[]>([])
  const [agent, setAgent] = useState(projectAgent ?? '')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    listMyAgents()
      .then((page) => {
        if (live) setAgents(page.items.map((a) => ({ slug: a.slug, name: a.name || a.slug })))
      })
      .catch(() => {
        if (live) setAgents([])
      })
    return () => {
      live = false
    }
  }, [])

  const onFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    if (!TEXT_FILE.test(file.name)) {
      setError('That file type can\'t be read here. Use a text file, or paste the text or a link to the Doc.')
      return
    }
    setError(null)
    file
      .text()
      .then((content) => {
        setText(content)
        if (!title) setTitle(file.name.replace(/\.[^.]+$/, ''))
      })
      .catch((err: unknown) => setError(errorText(err, 'Could not read that file')))
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    const link = linkOnly(text)
    setBusy(true)
    setError(null)
    createMeeting({
      workspace,
      project: target,
      title,
      agent,
      ask,
      follow_up: Boolean(agent),
      artifact: link
        ? { kind, source: linkSource(link), source_url: link, text: '' }
        : { kind, source: 'pasted', source_url: '', text },
    })
      .then((meeting) => {
        onSaved?.()
        navigate(scoped(`/meetings/${meeting.id}`))
      })
      .catch((err: unknown) => setError(errorText(err, 'Could not save the meeting')))
      .finally(() => setBusy(false))
  }

  const isLink = linkOnly(text) !== null
  return (
    <form onSubmit={submit} className="space-y-3 rounded-lg border border-border bg-card p-4" data-testid="add-meeting">
      <label className="block text-[13px] font-medium">
        The transcript or notes, or a link to them
        <Textarea
          className="mt-1 min-h-28"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste the text, or a link to the Google Doc"
          required
        />
      </label>
      {isLink && (
        <p className="text-[12px] text-muted-foreground">
          Your agent will read this link. If it can&apos;t, it will ask you to share the Doc with it or paste the text.
        </p>
      )}
      <div className="flex flex-wrap items-end gap-3 text-[13px]">
        <label className="block">
          Or a text file
          <input type="file" accept=".txt,.md,.vtt,.srt,.text" onChange={onFile} className="mt-1 block text-[12px]" />
        </label>
        <label className="block">
          This is
          <select value={kind} onChange={(e) => setKind(e.target.value)} className="mt-1 block rounded border border-border bg-background px-2 py-1">
            <option value="transcript">A transcript</option>
            <option value="notes">Notes</option>
            <option value="summary">A summary</option>
          </select>
        </label>
        <label className="block min-w-48 flex-1">
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Optional" className="mt-1 block w-full rounded border border-border bg-background px-2 py-1" />
        </label>
      </div>
      <div className="flex flex-wrap items-end gap-3 text-[13px]">
        {project === undefined && (
          <label className="block">
            Who can see it
            <select value={target} onChange={(e) => setTarget(e.target.value)} className="mt-1 block rounded border border-border bg-background px-2 py-1">
              <option value="">Just me</option>
              {(projects ?? []).map((p) => (
                <option key={p.address} value={p.address}>
                  Everyone on {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="block">
          Agent to help
          <select value={agent} onChange={(e) => setAgent(e.target.value)} className="mt-1 block rounded border border-border bg-background px-2 py-1">
            <option value="">Just save it</option>
            {agents.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        {agent && (
          <label className="block min-w-48 flex-1">
            What do you need?
            <input value={ask} onChange={(e) => setAsk(e.target.value)} placeholder="Optional: draft the follow-up email" className="mt-1 block w-full rounded border border-border bg-background px-2 py-1" />
          </label>
        )}
      </div>
      {project !== undefined && (
        <p className="text-[12px] text-muted-foreground">Everyone who can see this project can read what you save here.</p>
      )}
      {error && <p role="alert" className="text-[12px] text-destructive">{error}</p>}
      <Button type="submit" disabled={busy || !text.trim()}>
        {agent ? 'Save and get help' : 'Save'}
      </Button>
    </form>
  )
}
