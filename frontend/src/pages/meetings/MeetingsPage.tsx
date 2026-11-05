// /w/:workspace/meetings — every meeting you can read: your private ones and those of the projects
// you're on. Search across them, and add one.
import { useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { listMeetings, type MeetingRow } from '@/api/meetings'
import { errorText } from '@/api/problem'
import { listProjects } from '@/api/work'
import { AddMeeting } from '@/components/meetings/AddMeeting'
import { MeetingList } from '@/components/meetings/MeetingList'

export default function MeetingsPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [rows, setRows] = useState<MeetingRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [q, setQ] = useState('')
  const [adding, setAdding] = useState(false)
  const [projects, setProjects] = useState<{ address: string; name: string }[]>([])

  useEffect(() => {
    let live = true
    listMeetings({ workspace, q })
      .then((res) => {
        if (live) {
          setRows(res.meetings)
          setError(null)
        }
      })
      .catch((err: unknown) => live && setError(errorText(err, 'Could not load meetings')))
    return () => {
      live = false
    }
  }, [workspace, q])

  useEffect(() => {
    let live = true
    listProjects('open', workspace)
      .then((list) => {
        if (live) setProjects(list.map((p) => ({ address: `${p.workspace}/${p.handle}`, name: p.name })))
      })
      .catch(() => live && setProjects([]))
    return () => {
      live = false
    }
  }, [workspace])

  const search = (event: FormEvent) => {
    event.preventDefault()
    setQ(draft.trim())
  }

  return (
    <div className="max-w-4xl space-y-4 pb-28" data-testid="meetings-page">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Meetings</h1>
          <p className="text-[13px] text-muted-foreground">
            Yours stay private until you put them in a project. Then everyone on that project can read them.
          </p>
        </div>
        <Button onClick={() => setAdding((on) => !on)}>{adding ? 'Close' : 'Add a meeting'}</Button>
      </div>
      {adding && <AddMeeting workspace={workspace} projects={projects} />}
      <form onSubmit={search} className="flex gap-2">
        <input
          aria-label="Search meetings"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Search titles, people and what was said"
          className="w-full rounded border border-border bg-background px-3 py-1.5 text-[13px]"
        />
        <Button type="submit" variant="outline">
          Search
        </Button>
      </form>
      {error && <p role="alert" className="text-[13px] text-destructive">{error}</p>}
      {rows !== null && rows.length === 0 && !error && (
        <p className="text-[13px] text-muted-foreground">{q ? `Nothing matches "${q}".` : 'No meetings yet.'}</p>
      )}
      {rows !== null && rows.length > 0 && <MeetingList rows={rows} />}
    </div>
  )
}
