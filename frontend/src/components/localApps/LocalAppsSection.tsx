import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'
import {
  getLocalAppStore,
  listMyLocalApps,
  setLocalAppsSwitch,
  stopLocalAppRun,
  type LocalAppInstallation,
  type LocalAppStore,
} from '@/api/localApps'
import { errorText } from '@/api/problem'
import { Td, Th, Tr } from '@/components/DataTable'
import { useScopedPath } from '@/lib/scopedLinks'
import { describeRun } from './runState'

/** A stop is read at the app's next heartbeat, about 15 seconds later on a computer that's online. */
const REFRESH_WHILE_RUNNING_MS = 15_000

/**
 * The store's "Runs on your computer" section. Kept apart from web apps: these install per person
 * and per computer, not per workspace. Off, it says nothing to anyone but an admin, because an
 * admin choosing to offer apps that can record conversations isn't something to advertise to members.
 */
export function LocalAppsSection({ workspace }: { workspace: string }): JSX.Element | null {
  const scoped = useScopedPath()
  const [store, setStore] = useState<LocalAppStore | null>(null)
  const [installs, setInstalls] = useState<LocalAppInstallation[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const next = await getLocalAppStore(workspace)
    setStore(next)
    if (!next.enabled) {
      setInstalls([])
      return
    }
    const mine = await listMyLocalApps()
    setInstalls(mine.filter((i) => i.workspace === workspace))
  }, [workspace])

  useEffect(() => {
    load().catch((e: unknown) => setError(errorText(e, 'Failed to load apps that run on your computer')))
  }, [load])

  const anyRunning = installs.some((i) => i.last_run?.running)
  useEffect(() => {
    if (!anyRunning) return
    const timer = window.setInterval(() => void load().catch(() => undefined), REFRESH_WHILE_RUNNING_MS)
    return () => window.clearInterval(timer)
  }, [anyRunning, load])

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setError(errorText(e, 'Something went wrong'))
    } finally {
      setBusy(false)
    }
  }

  function flip(on: boolean) {
    if (!on && !window.confirm('Stop offering these apps in this workspace? Running copies stop at their next check-in, and nobody can install or start one.')) return
    void act(() => setLocalAppsSwitch(workspace, on))
  }

  const errorLine = error && <p className="mb-2 text-sm text-destructive">{error}</p>
  if (!store) return errorLine ? <div className="mt-8">{errorLine}</div> : null
  if (!store.enabled && !store.can_switch) return null

  return (
    <section className="mt-8" aria-labelledby="local-apps-heading" data-testid="local-apps">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 id="local-apps-heading" className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Runs on your computer</h2>
        <StatusBadge tone="warning">Experimental</StatusBadge>
        {store.can_switch && (
          <label className="ml-auto flex items-center gap-1.5 text-sm">
            <input type="checkbox" role="switch" checked={store.enabled} disabled={busy}
              onChange={(e) => flip(e.target.checked)} />
            Offer in this workspace
          </label>
        )}
      </div>
      {errorLine}

      {!store.enabled ? (
        <p className="max-w-prose text-sm text-muted-foreground" data-testid="local-apps-off">
          Programs from Canopy's store that people here can run on their own computers. These are off until an admin offers them, because they can record conversations.
        </p>
      ) : (
        <>
          <p className="mb-2 max-w-prose text-sm text-muted-foreground">
            You install one for yourself, on one of your computers, and start it from the menu bar there. They can change or disappear without notice.
          </p>
          {store.apps.length === 0 ? (
            <p className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">No apps have been released yet.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border bg-card">
              <table className="w-full text-[13px]">
                <thead>
                  <Tr head="xs" rule="bottom">
                    <Th density="snug">App</Th>
                    <Th density="snug">What it does</Th>
                    <Th density="snug">Version</Th>
                    <Th density="snug" srOnly>Install</Th>
                  </Tr>
                </thead>
                <tbody>
                  {store.apps.map((a) => (
                    <Tr key={a.key} rule="bottom-last" top>
                      <Td density="snug" nowrap>
                        <Link to={scoped(`/apps/local/${a.key}`)} className="font-medium hover:underline">{a.name}</Link>
                      </Td>
                      <Td density="snug" tone="muted">{a.tagline || a.description}</Td>
                      <Td density="snug" tone="muted" nowrap>{a.version}</Td>
                      <Td density="snug" align="right" nowrap>
                        {a.installed ? (
                          <span className="text-muted-foreground">Installed</span>
                        ) : (
                          <Link to={scoped(`/apps/local/${a.key}`)} className="text-primary hover:underline">Details and install</Link>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h3 className="mb-2 mt-4 text-sm font-semibold text-foreground">Your installations</h3>
          {installs.length === 0 ? (
            <p className="text-sm text-muted-foreground">You haven't installed any here.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border bg-card">
              <table className="w-full text-[13px]" data-testid="local-app-installations">
                <thead>
                  <Tr head="xs" rule="bottom">
                    <Th density="snug">App</Th>
                    <Th density="snug">Computer</Th>
                    <Th density="snug">Version</Th>
                    <Th density="snug">Last run</Th>
                    <Th density="snug" srOnly>Actions</Th>
                  </Tr>
                </thead>
                <tbody>
                  {installs.map((i) => (
                    <Tr key={i.id} rule="bottom-last" top>
                      <Td density="snug" nowrap>
                        <Link to={scoped(`/apps/local/${i.key}`)} className="font-medium hover:underline">{i.name}</Link>
                      </Td>
                      <Td density="snug" tone="muted">{i.computer.name}</Td>
                      <Td density="snug" tone="muted" nowrap>{i.version ?? 'None'}</Td>
                      <Td density="snug" tone="muted">
                        {describeRun(i.last_run)}
                        {i.refusal && <div className="text-xs text-warning">{i.refusal}</div>}
                      </Td>
                      <Td density="snug" align="right" nowrap>
                        {i.needs_acceptance && (
                          <Link to={scoped(`/apps/local/${i.key}`)} className="mr-2 text-warning hover:underline">Needs your acceptance</Link>
                        )}
                        {i.last_run?.running && (
                          <Button size="sm" variant="outline" disabled={busy || i.last_run.stop_requested}
                            onClick={() => { const id = i.last_run?.id; if (id) void act(() => stopLocalAppRun(id)) }}>
                            Stop
                          </Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  )
}
