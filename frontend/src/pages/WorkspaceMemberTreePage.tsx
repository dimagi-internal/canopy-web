import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Badge, Input, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import {
  addTreeMember,
  getMemberTree,
  removeMember,
  setMemberRole,
  WorkspaceApiError,
  type MemberRole,
  type MemberTreeCellOut,
  type MemberTreeOut,
  type MemberTreePersonOut,
  type MemberTreeWorkspaceOut,
} from '@/api/workspaces'
import { ROLE_SELECT_CLASS } from '@/components/people/RoleSelect'
import { scopedPath } from '@/lib/scopedLinks'
import { grantableRoles, mayManageMember, type WorkspaceRole } from '@/lib/workspaceRoles'

// Everyone across this workspace and the ones below it, one row per person,
// one column per workspace. Owners and admins of a workspace are in every
// workspace below it by inheritance; editors and viewers are NOT — so an org
// editor missing from a division is a stranger there. This page makes that
// gap visible and fixable in one place: an empty cell adds them (no invite),
// a role changes it, "—" removes them.

/** A system account is never above editor (apps/workspaces/system_accounts.py). */
const SYSTEM_ROLES: WorkspaceRole[] = ['viewer', 'editor']
const NONE = ''

function cellKey(userId: number, slug: string): string {
  return `${userId}:${slug}`
}

/** In some workspace of the tree but missing from at least one other. */
function hasGap(person: MemberTreePersonOut, workspaceCount: number): boolean {
  return Object.keys(person.cells).length < workspaceCount
}

export function WorkspaceMemberTreePage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const navigate = useNavigate()
  const { refresh: refreshWorkspaces } = useWorkspace()
  const auth = useAuth()
  const myEmail = auth.status === 'authenticated' ? auth.user.email.toLowerCase() : null

  const [tree, setTree] = useState<MemberTreeOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  const [gapsOnly, setGapsOnly] = useState(false)
  const [busy, setBusy] = useState<Set<string>>(new Set())
  const [cellErrors, setCellErrors] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    if (!slug) return
    try {
      setTree(await getMemberTree(slug))
      setLoadError(null)
    } catch (e: unknown) {
      if (e instanceof WorkspaceApiError && e.status === 404) {
        navigate('/', { replace: true })
        return
      }
      setLoadError(e instanceof Error ? e.message : 'Failed to load members')
    }
  }, [slug, navigate])

  useEffect(() => {
    setTree(null)
    void load()
  }, [load])

  const byslug = useMemo(
    () => Object.fromEntries((tree?.workspaces ?? []).map((w) => [w.slug, w])),
    [tree],
  )

  const rows = useMemo(() => {
    if (!tree) return null
    const q = filter.trim().toLowerCase()
    return tree.people.filter(
      (p) => (!q || p.email.toLowerCase().includes(q)) && (!gapsOnly || hasGap(p, tree.workspaces.length)),
    )
  }, [tree, filter, gapsOnly])

  const gapCount = useMemo(
    () => (tree ? tree.people.filter((p) => hasGap(p, tree.workspaces.length)).length : 0),
    [tree],
  )

  async function change(person: MemberTreePersonOut, ws: MemberTreeWorkspaceOut, next: string) {
    if (!slug) return
    const current = person.cells[ws.slug]
    const key = cellKey(person.user_id, ws.slug)
    if (next === NONE) {
      if (!current) return
      if (!window.confirm(`Remove ${person.email} from ${ws.display_name}?`)) return
    }
    setBusy((b) => new Set(b).add(key))
    setCellErrors((errs) => {
      const out = { ...errs }
      delete out[key]
      return out
    })
    try {
      if (next === NONE) await removeMember(ws.slug, person.user_id)
      else if (!current) await addTreeMember(ws.slug, person.user_id, next as MemberRole)
      else await setMemberRole(ws.slug, person.user_id, next as MemberRole)
      // Re-read the whole tree: a change can move what others inherit below it
      // (an admin demoted here leaves the workspaces under it too).
      await load()
      if (myEmail && person.email.toLowerCase() === myEmail) void refreshWorkspaces()
    } catch (e: unknown) {
      setCellErrors((errs) => ({ ...errs, [key]: e instanceof Error ? e.message : 'Failed' }))
    } finally {
      setBusy((b) => {
        const out = new Set(b)
        out.delete(key)
        return out
      })
    }
  }

  if (!slug) return null

  return (
    <div>
      <WorkbenchSubHeader
        title="Members across workspaces"
        count={tree?.people.length}
        action={
          <Link to={scopedPath(slug, '/settings/members')} className="text-[13px] text-primary hover:underline">
            Back to members
          </Link>
        }
      />
      <p className="mb-4 max-w-3xl text-[13px] text-muted-foreground">
        This workspace and every workspace below it that you are in. Owners and admins are in the workspaces
        below theirs automatically; editors and viewers are not, so someone missing from a column is an outsider
        there. Where you manage members, pick a role in an empty cell to add them (no invite is sent), change
        it, or pick “—” to remove them.
      </p>

      {loadError && <div className="mb-4 text-sm text-destructive">{loadError}</div>}

      <div className="mb-4 flex flex-wrap items-center gap-4">
        <Input
          type="search"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by email"
          aria-label="Filter by email"
          className="h-8 w-64"
        />
        <label className="flex items-center gap-2 text-[13px] text-foreground">
          <input type="checkbox" checked={gapsOnly} onChange={(e) => setGapsOnly(e.target.checked)} />
          Only show gaps
          <span className="text-muted-foreground">({gapCount})</span>
        </label>
      </div>

      {tree === null || rows === null ? (
        <div className="h-32 animate-pulse rounded-lg bg-muted" />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="sticky left-0 bg-background">Person</TableHead>
                {tree.workspaces.map((w) => (
                  <TableHead key={w.slug} className="align-bottom" title={w.slug}>
                    <div className="flex flex-col py-1">
                      <span className="font-medium text-foreground">{w.display_name}</span>
                      <span className="text-[11px] font-normal text-muted-foreground">
                        {w.depth === 0
                          ? 'this workspace'
                          : `${'› '.repeat(w.depth)}in ${byslug[w.parent ?? '']?.display_name ?? w.parent}`}
                      </span>
                    </div>
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={tree.workspaces.length + 1} className="text-muted-foreground">
                    {gapsOnly ? 'Nobody is missing from any workspace.' : 'Nobody matches.'}
                  </TableCell>
                </TableRow>
              ) : (
                rows.map((p) => (
                  <TableRow key={p.user_id}>
                    <TableCell className="sticky left-0 bg-background whitespace-normal">
                      <span className="text-foreground">{p.email}</span>
                      {p.system && (
                        <Badge variant="secondary" className="ml-2">
                          system
                        </Badge>
                      )}
                    </TableCell>
                    {tree.workspaces.map((w) => {
                      const key = cellKey(p.user_id, w.slug)
                      return (
                        <TableCell key={w.slug} className="align-top">
                          <TreeCell
                            person={p}
                            ws={w}
                            cell={p.cells[w.slug]}
                            fromName={(s) => byslug[s]?.display_name ?? s}
                            busy={busy.has(key)}
                            error={cellErrors[key]}
                            onChange={(next) => void change(p, w, next)}
                          />
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  )
}

function TreeCell({
  person,
  ws,
  cell,
  fromName,
  busy,
  error,
  onChange,
}: {
  person: MemberTreePersonOut
  ws: MemberTreeWorkspaceOut
  cell: MemberTreeCellOut | undefined
  fromName: (slug: string) => string
  busy: boolean
  error: string | undefined
  onChange: (next: string) => void
}): JSX.Element {
  const label = `${person.email} in ${ws.display_name}`
  let body: JSX.Element
  if (cell?.inherited) {
    body = (
      <div className="flex flex-col items-start gap-0.5">
        <span className="text-sm capitalize text-foreground">{cell.role}</span>
        <Badge variant="outline" className="font-normal text-muted-foreground">
          inherited{cell.inherited_from ? ` from ${fromName(cell.inherited_from)}` : ''}
        </Badge>
      </div>
    )
  } else {
    const grantable = grantableRoles(ws.my_role).filter((r) => !person.system || SYSTEM_ROLES.includes(r))
    // An empty cell may be filled at a role you may grant; a held role may be
    // changed only when it is below yours (an owner may change anyone).
    const editable = ws.can_manage && (cell ? mayManageMember(ws.my_role, cell.role) : grantable.length > 0)
    if (!editable) {
      body = cell ? (
        <span className="text-sm capitalize text-foreground">{cell.role}</span>
      ) : (
        <span className="text-sm text-muted-foreground">—</span>
      )
    } else {
      const values: string[] = [NONE, ...grantable]
      if (cell && !values.includes(cell.role)) values.push(cell.role)
      body = (
        <select
          aria-label={label}
          value={cell?.role ?? NONE}
          disabled={busy}
          onChange={(e) => onChange(e.target.value)}
          className={`${ROLE_SELECT_CLASS} ${cell ? '' : 'text-muted-foreground'}`}
        >
          {values.map((v) => (
            <option key={v || 'none'} value={v} className="capitalize">
              {v === NONE ? '—' : v}
            </option>
          ))}
        </select>
      )
    }
  }
  return (
    <div className="flex flex-col items-start gap-0.5">
      {body}
      {error && (
        <span role="alert" className="max-w-[12rem] text-[11px] text-destructive">
          {error}
        </span>
      )}
    </div>
  )
}
