import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'
import {
  getLocalAppStore,
  listMyLocalApps,
  stopLocalAppRun,
  type LocalAppInstallation,
  type LocalAppListing,
} from '@/api/localApps'
import { errorText } from '@/api/problem'
import { describeRun } from '@/components/localApps/runState'
import { useScopedPath } from '@/lib/scopedLinks'
import { AppListHeading, InstalledAppList, InstalledAppRow, STORE_ACTION_CLASS, StoreAppCard, StoreAppGrid } from './AppLists'

/** A stop is read at the app's next heartbeat, about 15 seconds later on a computer that's online. */
const REFRESH_WHILE_RUNNING_MS = 15_000

const UNRELEASED = <StatusBadge tone="warning">Unreleased</StatusBadge>

/**
 * Apps a person installs for themselves, not for a workspace, and every copy they have. Installing
 * asks for a computer and settings, so it happens on the app's own page rather than here.
 * Design: docs/architecture/local-apps.md.
 */
export function PersonalAppsTab(): JSX.Element {
  const scoped = useScopedPath()
  const [store, setStore] = useState<LocalAppListing[] | null>(null)
  const [installs, setInstalls] = useState<LocalAppInstallation[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    const [listing, mine] = await Promise.all([getLocalAppStore(), listMyLocalApps()])
    setStore([...listing.apps])
    setInstalls(mine)
  }, [])

  useEffect(() => {
    load().catch((e: unknown) => setError(errorText(e, 'Failed to load your apps')))
  }, [load])

  const anyRunning = (installs ?? []).some((i) => i.last_run?.running)
  useEffect(() => {
    if (!anyRunning) return
    const timer = window.setInterval(() => void load().catch(() => undefined), REFRESH_WHILE_RUNNING_MS)
    return () => window.clearInterval(timer)
  }, [anyRunning, load])

  async function stop(runId: string) {
    setBusy(true)
    setError(null)
    try {
      await stopLocalAppRun(runId)
      await load()
    } catch (e) {
      setError(errorText(e, 'Failed to stop the app'))
    } finally {
      setBusy(false)
    }
  }

  const page = (key: string) => scoped(`/apps/personal/${key}`)

  return (
    <>
      <p className="mt-3 max-w-prose text-sm text-muted-foreground">
        Apps you install for yourself, not for a workspace. They're unreleased, so they may change or stop working.
      </p>
      {error && <div className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive" role="alert">{error}</div>}

      <AppListHeading>Installed</AppListHeading>
      <InstalledAppList loading={installs === null && !error} empty="No apps yet. Install one from the store below.">
        {(installs ?? []).map((i) => {
          const run = i.last_run
          return (
            <InstalledAppRow
              key={i.id}
              to={page(i.key)}
              name={i.name}
              meta={`${i.computer.name} · ${i.version ? `version ${i.version}` : 'no version to run'}`}
              description={describeRun(run)}
              badges={
                <span className="ml-2 inline-flex flex-wrap items-center gap-2 align-middle">
                  {UNRELEASED}
                  {i.needs_acceptance && (
                    <Link to={page(i.key)} className="rounded bg-warning/15 px-1.5 py-0.5 text-xs">needs your acceptance</Link>
                  )}
                </span>
              }
              actions={run?.running && (
                <Button size="sm" variant="outline" disabled={busy || run.stop_requested} onClick={() => void stop(run.id)}>Stop</Button>
              )}
            >
              {i.refusal && <p className="mt-1 text-xs text-warning">{i.refusal}</p>}
            </InstalledAppRow>
          )
        })}
      </InstalledAppList>

      <AppListHeading>App store</AppListHeading>
      {store?.length === 0 ? (
        <div className="rounded-lg border border-border bg-card p-4 text-sm text-muted-foreground">No apps here yet.</div>
      ) : (
        <StoreAppGrid>
          {(store ?? []).map((a) => (
            <StoreAppCard
              key={a.key}
              to={page(a.key)}
              name={a.name}
              meta={`version ${a.version}`}
              badge={UNRELEASED}
              summary={a.tagline || a.description}
              detailsLabel="Details"
              action={a.installed ? (
                <span className="text-sm text-muted-foreground">Installed</span>
              ) : (
                <Link to={`${page(a.key)}#install`} className={STORE_ACTION_CLASS}>Install</Link>
              )}
            />
          ))}
        </StoreAppGrid>
      )}
    </>
  )
}
