import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'

import { getMember, WorkspaceApiError, type MemberProfileOut } from '@/api/workspaces'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { scopedPath } from '@/lib/scopedLinks'
import { relativeAge } from '@/lib/relativeAge'

const KIND_LABEL: Record<MemberProfileOut['kind'] & string, string> = {
  person: 'Person',
  agent: 'Agent',
}

/**
 * One member of a workspace, as the viewer may see them
 * (/w/:workspace/members/:userId). Every member sees who they are, their role,
 * the agents they operate here and the other workspaces you share; someone who
 * manages members also sees who invited them, when they last signed in and how
 * many chats they started here. The server decides what is filled in
 * (GET /api/workspaces/{slug}/members/{user_id}/); this page shows what it got.
 */
export function MemberPage(): JSX.Element {
  const { workspace: slug = '', userId = '' } = useParams()
  const { workspaces } = useWorkspace()
  const wsName = workspaces.find((w) => w.slug === slug)?.display_name ?? slug
  const id = Number(userId)

  const [member, setMember] = useState<MemberProfileOut | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!Number.isInteger(id) || id <= 0) {
      setError('There’s nothing at this address.')
      return
    }
    let cancelled = false
    setMember(null)
    setError(null)
    getMember(slug, id)
      .then((m) => {
        if (!cancelled) setMember(m)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(
          e instanceof WorkspaceApiError && e.status === 404
            ? `No one with this address is a member of ${wsName}.`
            : e instanceof Error ? e.message : 'Failed to load member',
        )
      })
    return () => {
      cancelled = true
    }
  }, [slug, id, wsName])

  const back = (
    <Link to={scopedPath(slug, '/settings/members')} className="text-[12px] text-muted-foreground hover:text-foreground">
      ← Members of {wsName}
    </Link>
  )

  if (error) {
    return (
      <div className="max-w-3xl space-y-3">
        {back}
        <p className="text-sm text-muted-foreground">{error}</p>
      </div>
    )
  }
  if (!member) return <div className="max-w-3xl"><div className="h-32 animate-pulse rounded-lg bg-muted" /></div>

  const name = member.name || member.email

  return (
    <div className="max-w-3xl space-y-6">
      {back}

      <header className="flex items-center gap-4">
        <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xl font-semibold text-primary">
          {name.charAt(0).toUpperCase()}
        </span>
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-foreground">{name}</h1>
          {member.name && <div className="text-[13px] text-muted-foreground">{member.email}</div>}
          <div className="mt-1 flex flex-wrap gap-2 text-[12px]">
            <span className="rounded bg-muted px-2 py-0.5 capitalize text-foreground">{member.role}</span>
            <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground">{KIND_LABEL[member.kind ?? 'person']}</span>
            {member.inherited && <span className="text-muted-foreground">via parent workspace</span>}
          </div>
        </div>
      </header>

      {member.is_you && (
        <div className="rounded-lg border border-primary/30 bg-primary/5 p-3 text-[13px] text-foreground">
          This is you, as members of {wsName} see you.{' '}
          <Link to="/settings/profile" className="text-primary hover:underline">Your profile →</Link>
        </div>
      )}

      <Section title="In this workspace">
        <Fact label="Role" value={<span className="capitalize">{member.role}</span>} />
        <Fact label="Member since" value={new Date(member.joined_at).toLocaleDateString()} />
        {member.agent && (
          <Fact
            label="Agent"
            value={<Link to={scopedPath(slug, `/agents/${member.agent.slug}`)} className="text-primary hover:underline">{member.agent.name}</Link>}
          />
        )}
      </Section>

      <Section title="Agents they operate">
        {(member.operates ?? []).length === 0 ? (
          <p className="text-[13px] text-muted-foreground">None in this workspace.</p>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {(member.operates ?? []).map((a) => (
              <li key={a.slug}>
                <Link to={scopedPath(slug, `/agents/${a.slug}`)} className="rounded border border-border px-2 py-1 text-[13px] text-foreground hover:border-primary/50">
                  {a.name}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>

      {(member.shared_workspaces ?? []).length > 0 && (
        <Section title={member.is_you ? 'Your other workspaces' : 'Other workspaces you share'}>
          <ul className="space-y-1 text-[13px]">
            {(member.shared_workspaces ?? []).map((w) => (
              <li key={w.slug}>
                <Link to={scopedPath(w.slug, `/members/${member.user_id}`)} className="text-primary hover:underline">{w.display_name}</Link>
                <span className="ml-2 capitalize text-muted-foreground">{w.role}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {member.admin && (
        <Section title="Admin detail" note="Shown to workspace admins and owners">
          <Fact label="Invited by" value={member.admin.invited_by_email ?? '—'} />
          <Fact label="Last signed in" value={member.admin.last_login ? relativeAge(member.admin.last_login) : 'Never'} />
          <Fact label="Chats started here" value={String(member.admin.chats_started ?? 0)} />
          <p className="pt-1 text-[12px]">
            <Link to={scopedPath(slug, '/settings/members')} className="text-primary hover:underline">Change role or remove in Members →</Link>
          </p>
        </Section>
      )}
    </div>
  )
}

function Section({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-5">
      <h2 className="mb-3 text-sm font-semibold text-foreground">
        {title}
        {note && <span className="ml-2 text-[11px] font-normal text-muted-foreground">{note}</span>}
      </h2>
      <div className="space-y-2">{children}</div>
    </section>
  )
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-3 text-[13px]">
      <span className="w-36 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 text-foreground">{value}</span>
    </div>
  )
}
