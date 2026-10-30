// Who else a project is open to: the workspaces of its own tree it is shared with
// (and the ones below them). Their members can read it and their agents can join it;
// they cannot lead it, and it is never shared across organisations
// (`docs/architecture/work.md` § Sharing a project across its workspace tree).
import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'

import {
  addProjectShare,
  listProjectShares,
  removeProjectShare,
  type ProjectDetail,
  type ProjectShare,
} from '@/api/work'
import { listWorkspaces, type WorkspaceOut } from '@/api/workspaces'
import { Section } from '@/components/objectPage/settingsLayout'

const FIELD = 'rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder'

export function ProjectShares({
  detail,
  workspace,
  handle,
  onChanged,
}: {
  detail: ProjectDetail
  workspace: string
  handle: string
  onChanged: () => void
}): JSX.Element | null {
  const [shares, setShares] = useState<ProjectShare[]>([])
  const [tree, setTree] = useState<WorkspaceOut[]>([])
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const load = useCallback(() => {
    listProjectShares(workspace, handle)
      .then(setShares)
      .catch(() => setShares([]))
  }, [workspace, handle])
  useEffect(load, [load])
  useEffect(() => {
    if (!detail.may_edit) return
    listWorkspaces()
      .then(setTree)
      .catch(() => setTree([]))
  }, [detail.may_edit])

  if (!shares.length && !detail.may_edit) return null

  // Only the project's own tree, and not where it already reaches: its workspace, or one above it.
  const own = tree.find((w) => w.slug === workspace)
  const taken = new Set(shares.map((s) => s.workspace))
  const above = new Set<string>()
  for (let cur = own; cur; cur = tree.find((w) => w.slug === cur?.parent)) above.add(cur.slug)
  const options = own ? tree.filter((w) => w.root === own.root && !above.has(w.slug) && !taken.has(w.slug)) : []

  const add = async (e: FormEvent) => {
    e.preventDefault()
    if (!pick) return
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await addProjectShare(workspace, handle, pick)
      setPick('')
      load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not share it')
    } finally {
      setBusy(false)
    }
  }

  const remove = async (slug: string) => {
    setError(null)
    setNote(null)
    try {
      const out = await removeProjectShare(workspace, handle, slug)
      const removed = out.removed_agents ?? []
      if (removed.length) setNote(`Removed from the project: ${removed.join(', ')}`)
      load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not stop sharing')
    }
  }

  return (
    <Section
      id="project-shares"
      title="Shared with"
      description="Other workspaces of this organisation that can read this project, and whose agents can join it. A share covers the workspaces below it too."
    >
      {shares.length > 0 && (
        <ul className="mb-3 divide-y divide-border rounded-lg border border-border bg-card text-[13px]" data-testid="project-shares">
          {shares.map((s) => (
            <li key={s.workspace} className="flex flex-wrap items-center gap-3 px-3 py-2">
              <span className="font-medium text-foreground">{s.workspace}</span>
              <span className="text-[12px] text-muted-foreground">
                {(s.member_agents ?? []).length ? `Agents on it: ${(s.member_agents ?? []).join(', ')}` : 'No agents from here yet'}
              </span>
              {detail.may_edit && (
                <Button size="sm" variant="ghost" className="ml-auto" onClick={() => void remove(s.workspace)}>
                  Stop sharing
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {detail.may_edit && (
        <form onSubmit={add} className="flex flex-wrap items-center gap-2" data-testid="add-share">
          <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="Workspace" className={FIELD}>
            <option value="">Share with a workspace…</option>
            {options.map((w) => (
              <option key={w.slug} value={w.slug}>
                {w.display_name} ({w.slug})
              </option>
            ))}
          </select>
          <Button size="sm" type="submit" disabled={busy || !pick}>
            Share
          </Button>
        </form>
      )}
      {note && <p className="mt-2 text-[12px] text-muted-foreground">{note}</p>}
      {error && <p role="alert" className="mt-2 text-[12px] text-destructive">{error}</p>}
    </Section>
  )
}
