// The people on a project: its lead and those added by name. They may change the project and
// they open its vaults, so only its lead, an admin on it or a workspace owner may change the list
// (`docs/architecture/work.md` § A project's people). Who merely took part is `participants`.
import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'

import {
  addProjectMember,
  listProjectMembers,
  removeProjectMember,
  type ProjectDetail,
  type ProjectMemberRole,
  type ProjectPeople,
} from '@/api/work'
import { Section } from '@/components/objectPage/settingsLayout'

const FIELD = 'rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder'
const ROLE: Record<string, string> = { owner: 'Lead', admin: 'Admin', member: 'Member' }

export function ProjectMembers({
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
  const [people, setPeople] = useState<ProjectPeople | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState<ProjectMemberRole>('member')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listProjectMembers(workspace, handle)
      .then(setPeople)
      .catch(() => setPeople(null))
  }, [workspace, handle])

  const change = useCallback(
    async (act: () => Promise<ProjectPeople>, fallback: string) => {
      setBusy(true)
      setError(null)
      try {
        setPeople(await act())
        onChanged()
        return true
      } catch (err) {
        setError(err instanceof Error ? err.message : fallback)
        return false
      } finally {
        setBusy(false)
      }
    },
    [onChanged],
  )

  if (!people) return null
  const members = people.members ?? []
  const missing = people.box_owners_missing ?? []
  const manages = people.may_manage ?? false
  if (!members.length && !manages) return null

  const add = async (e: FormEvent) => {
    e.preventDefault()
    const address = email.trim()
    if (!address) return
    if (await change(() => addProjectMember(workspace, handle, address, role), 'Could not add them')) setEmail('')
  }
  const hasVaults = (detail.vaults ?? []).length > 0

  return (
    <Section
      id="project-members"
      title="People"
      description={
        hasVaults
          ? 'Who is on this project. They can change it and open its vaults. Having access to one of its agents is not enough.'
          : 'Who is on this project. They can change it, and they will open any vault attached to it.'
      }
    >
      {members.length > 0 && (
        <ul className="mb-3 divide-y divide-border rounded-lg border border-border bg-card text-[13px]" data-testid="project-members">
          {members.map((m) => {
            const named = (m.sources ?? []).includes('direct')
            return (
              <li key={m.user_id} className="flex flex-wrap items-center gap-3 px-3 py-2">
                <span className="font-medium text-foreground">{m.email}</span>
                <span className="text-[12px] text-muted-foreground">{ROLE[m.role] ?? m.role}</span>
                {manages && named && (
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-auto"
                    disabled={busy}
                    onClick={() => void change(() => removeProjectMember(workspace, handle, m.user_id), 'Could not remove them')}
                  >
                    Remove
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {missing.length > 0 && (
        <div className="mb-3 rounded-lg border border-border bg-card px-3 py-2 text-[13px]" data-testid="box-owners-missing">
          <p className="text-[12px] text-muted-foreground">
            These agents run on machines whose owners are not on this project, so turns there can&apos;t read its vaults:
          </p>
          <ul className="mt-1">
            {missing.map((o) => (
              <li key={o.email} className="flex flex-wrap items-center gap-3 py-1">
                <span className="text-foreground">{o.email}</span>
                <span className="text-[12px] text-muted-foreground">{(o.agents ?? []).join(', ')}</span>
                <Button
                  size="sm"
                  variant="ghost"
                  className="ml-auto"
                  disabled={busy}
                  onClick={() => void change(() => addProjectMember(workspace, handle, o.email, 'member'), 'Could not add them')}
                >
                  Add
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {manages && (
        <form onSubmit={add} className="flex flex-wrap items-center gap-2" data-testid="add-member">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-label="Email"
            placeholder="name@example.org"
            className={FIELD}
          />
          <select value={role} onChange={(e) => setRole(e.target.value as ProjectMemberRole)} aria-label="Role" className={FIELD}>
            <option value="member">Member</option>
            <option value="admin">Admin</option>
          </select>
          <Button size="sm" type="submit" disabled={busy || !email.trim()}>
            Add
          </Button>
        </form>
      )}
      {error && <p role="alert" className="mt-2 text-[12px] text-destructive">{error}</p>}
    </Section>
  )
}
