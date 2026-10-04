import { useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import {
  listAccessRequests,
  setAccessSettings,
  WorkspaceApiError,
  type AccessRequestOut,
  type AutoApproveRole,
} from '@/api/workspaces'
import { roleAllows } from '@/lib/workspaceRoles'
import { notifyProblem, requesterLabel } from './accessRequests'

/**
 * Access requests — people asking to be invited into this workspace
 * (docs/architecture/access.md, "Getting into a workspace").
 *
 * Someone whose login email is at one of the workspace's request domains asks
 * from the first-run screen; every admin and owner is emailed a link straight
 * to that request's page (`access-requests/:id`), where they approve at a role
 * or deny. This is the list of them, newest first, plus — for owners — the
 * workspace's auto-approve setting.
 */
export function WorkspaceAccessRequestsPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const { workspaces, refresh } = useWorkspace()
  const me = workspaces.find((w) => w.slug === slug)
  const canManage = roleAllows(me?.role, 'members.manage')
  const isOwner = roleAllows(me?.role, 'own')

  const [rows, setRows] = useState<AccessRequestOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [settingError, setSettingError] = useState<string | null>(null)

  useEffect(() => {
    if (!slug || !canManage) return
    let cancelled = false
    setRows(null)
    setError(null)
    listAccessRequests(slug)
      .then((r) => {
        if (!cancelled) setRows(r)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load access requests')
      })
    return () => {
      cancelled = true
    }
  }, [slug, canManage])

  const pendingCount = useMemo(() => (rows ?? []).filter((r) => r.status === 'pending').length, [rows])

  async function changeAutoApprove(next: AutoApproveRole) {
    if (!slug) return
    setSaving(true)
    setSettingError(null)
    try {
      await setAccessSettings(slug, next)
      await refresh()
    } catch (e) {
      setSettingError(e instanceof WorkspaceApiError ? e.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  if (!slug) return null
  if (!canManage) {
    return (
      <p className="max-w-4xl text-[13px] text-muted-foreground">
        Only this workspace&apos;s admins and owners see and decide access requests.
      </p>
    )
  }

  const domains = me?.access_request_domains ?? []
  const autoRole = (me?.auto_approve_role ?? '') as AutoApproveRole

  return (
    <div className="max-w-4xl">
      <div className="mb-6 rounded-lg border border-border bg-card p-5" data-testid="access-request-settings">
        <h2 className="text-sm font-semibold text-foreground">Who may ask</h2>
        <p className="mt-1 text-[13px] text-foreground-secondary">
          {domains.length > 0 ? (
            <>
              People signed in with an address at{' '}
              <span className="font-medium text-foreground">{domains.join(', ')}</span> may request an
              invitation. Every admin and owner is emailed each request.
            </>
          ) : (
            <>Nobody can request an invitation here — people get in by invite only.</>
          )}
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[13px]">
          <label htmlFor="auto-approve" className="text-foreground-secondary">
            Approve requests automatically:
          </label>
          {isOwner ? (
            <select
              id="auto-approve"
              value={autoRole}
              disabled={saving}
              onChange={(e) => void changeAutoApprove(e.target.value as AutoApproveRole)}
              className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
            >
              <option value="">Off — an admin or owner decides each one</option>
              <option value="viewer">On, as viewer</option>
              <option value="editor">On, as editor</option>
            </select>
          ) : (
            <span className="font-medium text-foreground">
              {autoRole ? `on, as ${autoRole}` : 'off'}
            </span>
          )}
          {!isOwner && <span className="text-[12px] text-muted-foreground">(an owner changes this)</span>}
        </div>
        {autoRole && (
          <p className="mt-2 text-[12px] text-muted-foreground">
            Auto-approved requests still email every admin and owner, with a link to change the
            person&apos;s role or remove them.
          </p>
        )}
        {settingError && <p className="mt-2 text-[12px] text-destructive">{settingError}</p>}
      </div>

      <WorkbenchSubHeader title="Access requests" count={pendingCount} />
      {error && <div className="mb-4 text-sm text-destructive">{error}</div>}
      {rows === null && !error ? (
        <div className="h-16 animate-pulse rounded-lg bg-muted" />
      ) : (rows ?? []).length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No access requests yet.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Who</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Asked</TableHead>
              <TableHead className="text-right">Action</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(rows ?? []).map((r) => {
              const problem = notifyProblem(r)
              return (
                <TableRow key={r.id}>
                  <TableCell className="whitespace-normal text-foreground">
                    {requesterLabel(r)}
                    {r.note && <div className="text-[12px] text-muted-foreground">“{r.note}”</div>}
                    {problem && <div className="text-[11px] text-warning">{problem}</div>}
                  </TableCell>
                  <TableCell className={r.status === 'pending' ? 'font-medium text-foreground' : 'text-muted-foreground'}>
                    {r.status === 'approved'
                      ? `approved as ${r.role}${r.auto ? ' (auto)' : ''}`
                      : r.status}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{new Date(r.created_at).toLocaleString()}</TableCell>
                  <TableCell className="text-right">
                    <Link to={String(r.id)} className="text-[13px] text-primary hover:underline">
                      {r.status === 'pending' ? 'Review' : 'View'}
                    </Link>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
