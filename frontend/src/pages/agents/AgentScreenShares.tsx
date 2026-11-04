import { useCallback, useEffect, useState } from 'react'
import {
  approveSurfaceShare,
  listSurfaceShares,
  previewSurfaceShare,
  stopSurfaceShare,
  type SurfaceKind,
  type SurfaceShare,
  type SurfaceSharePreview,
} from '@/api/connections'
import { listProjects } from '@/api/work'
import { listInstalledApps } from '@/api/workspaceApps'
import { ApiError, errorText } from '@/api/problem'
import { ConnectionLine } from '@/lib/ConnectionLine'
import { describeLines } from '@/lib/connectionLines'
import { Button } from 'canopy-ui/ui'

type Option = { value: string; label: string }
type Line = { method: string; path: string }
const CHANGED = 'This screen changed while you were looking. Check what it asks for now.'

function Lines({ reads, writes, who }: { reads: readonly Line[]; writes: readonly Line[]; who: string }) {
  const lines = describeLines({ reads, writes }, who)
  if (!lines.length) return <p className="text-[12px] text-muted-foreground">Nothing.</p>
  return (
    <ul className="list-disc pl-4 text-[12px] text-foreground">
      {lines.map((line) => <li key={line.code ?? line.words}><ConnectionLine line={line} /></li>)}
    </ul>
  )
}

/**
 * Project and app screens that may call Salesforce as an agent's account. The agent's own admins are the
 * ones who may list, add, approve and stop (`Agent.is_admin`, not a workspace role), so whoever the list
 * loads for gets the controls, and for anyone else it is a 404 and there is no section at all. Approving
 * always asks for a passkey, which the API client's step-up middleware prompts for.
 */
export function AgentScreenShares({
  connectionId, agentName, workspace, serverUse,
}: {
  connectionId: string
  agentName: string
  workspace: string
  serverUse: boolean
}) {
  const [shares, setShares] = useState<SurfaceShare[] | null>(null)
  const [hidden, setHidden] = useState(false)
  const [options, setOptions] = useState<Option[]>([])
  const [pick, setPick] = useState('')
  const [preview, setPreview] = useState<SurfaceSharePreview | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    listSurfaceShares(connectionId).then(setShares).catch((e) => {
      if (e instanceof ApiError && e.status === 404) setHidden(true)
      else {
        setShares((was) => was ?? [])
        setError(errorText(e, 'Could not read which screens use this.'))
      }
    })
  }, [connectionId])
  useEffect(load, [load])

  useEffect(() => {
    if (!serverUse) return
    let live = true
    Promise.all([
      workspace ? listProjects('open', workspace).catch(() => []) : Promise.resolve([]),
      workspace ? listInstalledApps(workspace).catch(() => []) : Promise.resolve([]),
    ]).then(([projects, apps]) => {
      if (!live) return
      setOptions([
        ...projects.filter((p) => !workspace || p.workspace === workspace)
          .map((p) => ({ value: `project:${p.id}`, label: `Project: ${p.name || p.handle}` })),
        ...apps.map((a) => ({ value: `app:${a.workspace}/${a.key}`, label: `App: ${a.name}` })),
      ])
    })
    return () => { live = false }
  }, [workspace, serverUse])

  async function look(kind: SurfaceKind, ref: string, said = '') {
    setBusy(true)
    setError('')
    setNotice(said)
    try {
      setPreview(await previewSurfaceShare(connectionId, kind, ref))
    } catch (e) {
      setPreview(null)
      setError(errorText(e, 'Could not read what that screen asks for.'))
    } finally {
      setBusy(false)
    }
  }

  async function approve() {
    if (!preview) return
    const { kind, ref } = preview as { kind: SurfaceKind; ref: string }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await approveSurfaceShare(connectionId, kind, ref, { reads: preview.reads, writes: preview.writes })
      setNotice(`${preview.title} may now use ${agentName}’s account for what is listed.`)
      setPreview(null)
      setPick('')
      load()
    } catch (e) {
      if (e instanceof ApiError && e.code === 'declaration_changed') {
        await look(kind, ref, CHANGED)
        return
      }
      setError(errorText(e, 'Could not approve that.'))
    } finally {
      setBusy(false)
    }
  }

  async function stop(id: number) {
    setBusy(true)
    setError('')
    try {
      await stopSurfaceShare(connectionId, id)
      load()
    } catch (e) {
      setError(errorText(e, 'Could not stop that.'))
    } finally {
      setBusy(false)
    }
  }

  const cut = pick.indexOf(':')
  const kind = (cut < 0 ? 'project' : pick.slice(0, cut)) as SurfaceKind
  const ref = cut < 0 ? '' : pick.slice(cut + 1)

  if (hidden || shares === null) return null

  return (
    <div className="space-y-2 border-t border-border pt-2" data-testid={`screen-shares-${connectionId}`}>
      <p className="text-[12px] font-medium text-foreground">Project screens that may use {agentName}’s account</p>
      <p className="text-[12px] text-muted-foreground">Anyone who can open that screen acts as {agentName} in Salesforce.</p>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      {notice && <p role="status" className="text-[12px] text-success">{notice}</p>}
      {shares.map((s) => (
        <div key={s.id} className="rounded border border-border p-2" data-testid={`screen-share-${s.id}`}>
          <div className="flex items-center justify-between gap-3">
            <p className="text-[13px] font-medium">{s.title ?? 'A screen that no longer exists'}</p>
            <div className="flex shrink-0 gap-2">
              {s.needs_approval && (
                <Button size="sm" disabled={busy} onClick={() => look(s.kind as SurfaceKind, s.ref)}>Approve changes</Button>
              )}
              <Button size="sm" variant="outline" disabled={busy} onClick={() => stop(s.id)}>Stop</Button>
            </div>
          </div>
          <Lines reads={s.approved_reads} writes={s.approved_writes} who={agentName} />
          {s.needs_approval && (
            <p className="text-[12px] text-warning">
              Stopped until approved. It now also asks to:{' '}
              {describeLines({ reads: s.pending_reads, writes: s.pending_writes }, agentName).map((line, n) => (
                <span key={line.code ?? line.words}>{n > 0 && ', '}<ConnectionLine line={line} /></span>
              ))}
            </p>
          )}
        </div>
      ))}
      {!serverUse ? (
        <p className="text-[12px] text-warning">Turn on Used by canopy-web first.</p>
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <label className="text-[12px] text-muted-foreground" htmlFor={`add-${connectionId}`}>Screen to add</label>
            <select
              id={`add-${connectionId}`}
              className="rounded border border-border bg-background px-2 py-1 text-[12px]"
              value={pick}
              onChange={(e) => { setPick(e.target.value); setPreview(null); setNotice('') }}
            >
              <option value="">Choose a project or app…</option>
              {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <Button size="sm" variant="outline" disabled={!pick || busy} onClick={() => look(kind, ref)}>Look at what it asks for</Button>
          </div>
          {preview && (
            <div className="rounded border border-border bg-muted/40 p-2">
              <p className="text-[12px] font-medium">{preview.title} would be able to:</p>
              <Lines reads={preview.reads} writes={preview.writes} who={agentName} />
              <div className="mt-2">
                <Button size="sm" disabled={busy} onClick={approve}>Approve</Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
