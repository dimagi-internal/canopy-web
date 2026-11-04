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
import { listProjects } from '@/api/agents'
import { listInstalledApps } from '@/api/workspaceApps'
import { ApiError, errorText } from '@/api/problem'
import { describeLines } from '@/lib/connectionLines'
import { Button } from 'canopy-ui/ui'

type Option = { value: string; label: string }
type Line = { method: string; path: string }
const CHANGED = 'This screen changed while you were looking. Check what it asks for now.'

function Lines({ reads, writes }: { reads: readonly Line[]; writes: readonly Line[] }) {
  const items = [...describeLines(reads), ...describeLines(writes)]
  const unique = [...new Set(items)]
  if (!unique.length) return <p className="text-[12px] text-muted-foreground">Nothing.</p>
  return (
    <ul className="list-disc pl-4 text-[12px] text-foreground">
      {unique.map((t) => <li key={t}>{t}</li>)}
    </ul>
  )
}

/**
 * Project and app screens that may call Salesforce as an agent's account. Listing is for anyone who can
 * see the panel; adding, approving and stopping are for `canChange` (the server checks it again, and
 * approving always asks for a passkey, which the API client's step-up middleware prompts for).
 */
export function AgentScreenShares({
  connectionId, agentSlug, agentName, workspace, serverUse, canChange,
}: {
  connectionId: string
  agentSlug: string
  agentName: string
  workspace: string
  serverUse: boolean
  canChange: boolean
}) {
  const [shares, setShares] = useState<SurfaceShare[]>([])
  const [options, setOptions] = useState<Option[]>([])
  const [pick, setPick] = useState('')
  const [preview, setPreview] = useState<SurfaceSharePreview | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    listSurfaceShares(connectionId).then(setShares).catch((e) => setError(errorText(e, 'Could not read which screens use this.')))
  }, [connectionId])
  useEffect(load, [load])

  useEffect(() => {
    if (!canChange || !serverUse) return
    let live = true
    Promise.all([
      listProjects(agentSlug).catch(() => []),
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
  }, [agentSlug, workspace, canChange, serverUse])

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
            {canChange && (
              <div className="flex shrink-0 gap-2">
                {s.needs_approval && (
                  <Button size="sm" disabled={busy} onClick={() => look(s.kind as SurfaceKind, s.ref)}>Approve changes</Button>
                )}
                <Button size="sm" variant="outline" disabled={busy} onClick={() => stop(s.id)}>Stop</Button>
              </div>
            )}
          </div>
          <Lines reads={s.approved_reads} writes={s.approved_writes} />
          {s.needs_approval && (
            <p className="text-[12px] text-warning">
              Stopped until approved. It now also asks to: {describeLines([...s.pending_reads, ...s.pending_writes]).join(', ')}
            </p>
          )}
        </div>
      ))}
      {!serverUse ? (
        <p className="text-[12px] text-warning">Turn on Used by canopy-web first.</p>
      ) : canChange ? (
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
              <Lines reads={preview.reads} writes={preview.writes} />
              <div className="mt-2">
                <Button size="sm" disabled={busy} onClick={approve}>Approve</Button>
              </div>
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
