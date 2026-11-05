// A project's meetings: the history of what its people talked about, and what they want done with
// the notes. Those instructions are what an agent works from when it follows up.
import { useCallback, useEffect, useState, type JSX } from 'react'
import { Button, Textarea } from 'canopy-ui/ui'
import { listMeetings, type MeetingRow } from '@/api/meetings'
import { errorText } from '@/api/problem'
import { type ProjectDetail } from '@/api/work'
import { Section } from '@/components/objectPage/settingsLayout'
import { AddMeeting } from '@/components/meetings/AddMeeting'
import { MeetingList } from '@/components/meetings/MeetingList'

export function ProjectMeetings({
  detail,
  workspace,
  handle,
  onSaveInstructions,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onSaveInstructions: (text: string) => void
}): JSX.Element {
  const [rows, setRows] = useState<MeetingRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(detail.meeting_instructions ?? '')
  const address = `${workspace}/${handle}`

  const load = useCallback(() => {
    listMeetings({ project: address })
      .then((res) => setRows(res.meetings))
      .catch((err: unknown) => setError(errorText(err, 'Could not load meetings')))
  }, [address])
  useEffect(() => load(), [load])

  return (
    <Section id="project-meetings" title="Meetings" description="What this project's people have talked about, and what to do with the notes.">
      <div className="space-y-3" data-testid="project-meetings">
        <div className="rounded-lg border border-border bg-card p-4 text-[13px]">
          <div className="flex items-center justify-between gap-2">
            <h3 className="font-medium">What to do with meeting notes</h3>
            {detail.may_edit && !editing && (
              <Button variant="outline" onClick={() => setEditing(true)}>
                Edit
              </Button>
            )}
          </div>
          {editing ? (
            <>
              <Textarea
                aria-label="What to do with meeting notes"
                className="mt-2 min-h-24"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder="For example: file action items as tasks, and draft a recap for the partner."
              />
              <div className="mt-2 flex gap-2">
                <Button
                  onClick={() => {
                    onSaveInstructions(draft)
                    setEditing(false)
                  }}
                >
                  Save
                </Button>
                <Button variant="outline" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            </>
          ) : (
            <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
              {detail.meeting_instructions || 'Nothing yet. An agent following up will just ask what you need.'}
            </p>
          )}
        </div>
        <div className="flex justify-end">
          <Button variant="outline" onClick={() => setAdding((on) => !on)}>
            {adding ? 'Close' : 'Add a meeting'}
          </Button>
        </div>
        {adding && (
          <AddMeeting workspace={workspace} project={address} projectAgent={detail.agent_slug ?? ''} onSaved={load} />
        )}
        {error && <p role="alert" className="text-[12px] text-destructive">{error}</p>}
        {rows.length > 0 ? (
          <MeetingList rows={rows} showProject={false} />
        ) : (
          <p className="text-[13px] text-muted-foreground">No meetings in this project yet.</p>
        )}
      </div>
    </Section>
  )
}
