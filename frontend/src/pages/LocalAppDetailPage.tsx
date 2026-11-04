import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button, StatusBadge } from 'canopy-ui/ui'
import {
  acceptLocalAppChanges,
  configureLocalApp,
  getLocalApp,
  installLocalApp,
  listMyLocalApps,
  removeLocalApp,
  settingsSpecOf,
  stopLocalAppRun,
  type LocalAppDetail,
  type LocalAppInstallation,
} from '@/api/localApps'
import { ApiError, errorText } from '@/api/problem'
import { initialSettings, SettingsFields, settingsToSend, type SettingValues } from '@/components/localApps/SettingsFields'
import { describeRun } from '@/components/localApps/runState'
import { useScopedPath } from '@/lib/scopedLinks'

const START_HERE = 'You start it from the menu bar on that computer. This page can’t start it.'
const SELECT_CLASS = 'w-full rounded-md border border-input bg-input px-2 py-1.5 text-[13px] text-foreground'

/**
 * One app that runs on a person's own computer, as someone deciding to install it reads it, and
 * their copies of it once they have. What it says it reads and sends sits above the form, because
 * clicking Install is consenting to it. `digest` names what was on screen, so a version released
 * while they read is refused, not accepted unseen. Design: docs/architecture/local-apps.md.
 */
export default function LocalAppDetailPage(): JSX.Element {
  const { workspace = '', key = '' } = useParams()
  const scoped = useScopedPath()
  const [app, setApp] = useState<LocalAppDetail | null>(null)
  const [mine, setMine] = useState<LocalAppInstallation[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)

  const load = useCallback(async () => {
    const [detail, installs] = await Promise.all([getLocalApp(workspace, key), listMyLocalApps()])
    setApp(detail)
    setMine(installs.filter((i) => i.key === key))
  }, [workspace, key])

  useEffect(() => {
    setApp(null)
    setLoadError(null)
    load().catch((e: unknown) => setLoadError(errorText(e, 'Failed to load the app')))
  }, [load])

  const back = <Link to={scoped('/apps')} className="text-sm text-primary hover:underline">← Apps</Link>
  if (loadError) return <div className="mx-auto max-w-3xl p-6">{back}<p className="mt-4 text-sm text-destructive">{loadError}</p></div>
  if (!app) return <div className="mx-auto max-w-3xl p-6">{back}<p className="mt-4 text-sm text-muted-foreground">Loading…</p></div>

  const here = mine.filter((i) => i.workspace === workspace)
  const hereComputers = new Set(here.map((i) => i.computer.id))
  const free = app.computers.filter((c) => !hereComputers.has(c.id))

  return (
    <div className="mx-auto max-w-3xl p-6">
      {back}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <h1 className="text-2xl font-semibold">{app.name}</h1>
        <StatusBadge tone="warning">Experimental</StatusBadge>
      </div>
      <div className="mt-0.5 text-xs text-muted-foreground">by {app.publisher} · version {app.version}</div>
      <p className="mt-3 max-w-prose whitespace-pre-line text-sm">{app.description}</p>

      <section className="mt-6 rounded-lg border border-border bg-card p-4" data-testid="local-app-disclosure">
        <h2 className="text-sm font-semibold">What it does on your computer</h2>
        {app.notice && <p className="mt-2 rounded-md border border-warning/30 bg-warning/10 p-2 text-[13px] text-foreground">{app.notice}</p>}
        <Promises title="Reads" items={app.disclosure.reads} />
        <Promises title="Sends" items={app.disclosure.sends} />
        {app.installs_with.length > 0 && (
          <p className="mt-3 text-[13px]" data-testid="local-app-installs-with">
            Installing this also installs {app.installs_with.join(', ')}, which it needs in order to work.
          </p>
        )}
        {app.source_disclosures?.map((source) => {
          const promise = source as unknown as { name: string; disclosure?: { reads?: string[]; sends?: string[] } }
          return (
            <div key={promise.name} data-testid="local-app-source-disclosure">
              <Promises title={`${promise.name} reads`} items={promise.disclosure?.reads} />
              <Promises title={`${promise.name} sends`} items={promise.disclosure?.sends} />
            </div>
          )
        })}
        <p className="mt-3 text-[13px] text-muted-foreground">
          Canopy reviewed this code before listing it. It runs as you, and can read what you can read.
        </p>
      </section>

      {here.length > 0 && (
        <section className="mt-6" aria-label="Your copies">
          <h2 className="text-sm font-semibold">Installed</h2>
          <p className="mt-1 text-[13px] text-muted-foreground">{START_HERE}</p>
          <div className="mt-2 space-y-2">
            {here.map((i) => <InstalledCopy key={i.id} install={i} reload={load} />)}
          </div>
        </section>
      )}

      {app.computers.length === 0 ? (
        <section className="mt-6 rounded-lg border border-border bg-card p-4 text-[13px]" data-testid="local-app-no-computer">
          <h2 className="text-sm font-semibold">Install</h2>
          <p className="mt-1">
            You have no computer connected to Canopy. These apps run on your own laptop, through Canopy Runner.{' '}
            <Link to="/get-started#runner" className="text-primary hover:underline">How to get the runner</Link>
          </p>
        </section>
      ) : free.length > 0 && (
        // Keyed on the free computers, so one just installed on can't stay selected. Not on the digest:
        // a refusal for a changed app redraws the page and must keep its message and what was typed.
        <InstallForm key={free.map((c) => c.id).join()} workspace={workspace} app={app} free={free} mine={mine} again={here.length > 0} reload={load} />
      )}

      <section className="mt-6">
        <h2 className="text-sm font-semibold">Versions</h2>
        <div className="mt-1 space-y-2 text-[13px]">
          {app.versions.map((v) => (
            <div key={v.version}>
              <div>
                {v.version}
                {v.released_at && <span className="ml-2 text-xs text-muted-foreground">{v.released_at.slice(0, 10)}</span>}
                {v.revoked && <StatusBadge tone="destructive" className="ml-2">Withdrawn</StatusBadge>}
              </div>
              {v.changelog && <p className="mt-0.5 whitespace-pre-line text-xs text-muted-foreground">{v.changelog}</p>}
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

function Promises({ title, items }: { title: string; items?: readonly string[] }): JSX.Element | null {
  if (!items?.length) return null
  return (
    <div className="mt-3">
      <h3 className="text-[13px] font-medium">{title}</h3>
      <ul className="mt-0.5 list-disc pl-5 text-[13px]">{items.map((s) => <li key={s}>{s}</li>)}</ul>
    </div>
  )
}

function InstallForm({
  workspace,
  app,
  free,
  mine,
  again,
  reload,
}: {
  workspace: string
  app: LocalAppDetail
  free: LocalAppDetail['computers']
  mine: LocalAppInstallation[]
  again: boolean
  reload: () => Promise<void>
}): JSX.Element {
  const spec = settingsSpecOf(app.settings)
  const [computer, setComputer] = useState(free.length === 1 ? free[0].id : '')
  const [values, setValues] = useState<SettingValues>(() => initialSettings(spec))
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // An installation belongs to one workspace, and installing again on that computer moves it here.
  const elsewhere = (id: string) => mine.find((i) => i.computer.id === id)?.workspace

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await installLocalApp(workspace, app.key, { runner_id: computer, settings: settingsToSend(spec, values), accept_digest: app.digest })
      await reload()
    } catch (err) {
      setError(errorText(err, 'Failed to install the app'))
      // A new version arrived while they read: draw what they'd now be accepting.
      if (err instanceof ApiError && err.status === 409) await reload().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">{again ? 'Install on another computer' : 'Install'}</h2>
      <p className="mt-1 text-[13px] text-muted-foreground">Installing starts nothing. {START_HERE}</p>
      <form className="mt-3 space-y-3" onSubmit={(e) => void submit(e)} aria-label="Install">
        <div>
          <label htmlFor="local-app-computer" className="mb-1 block text-[13px] font-medium">Computer</label>
          <select id="local-app-computer" required className={SELECT_CLASS} value={computer} onChange={(e) => setComputer(e.target.value)}>
            {free.length > 1 && <option value="">Choose a computer</option>}
            {free.map((c) => {
              const other = elsewhere(c.id)
              return <option key={c.id} value={c.id}>{c.name}{other ? ` (moves it here from ${other})` : ''}</option>
            })}
          </select>
        </div>
        <SettingsFields spec={spec} agents={app.agents} values={values} onChange={setValues} />
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" disabled={busy}>{busy ? 'Installing…' : 'Install'}</Button>
      </form>
    </section>
  )
}

function InstalledCopy({ install, reload }: { install: LocalAppInstallation; reload: () => Promise<void> }): JSX.Element {
  const spec = settingsSpecOf(install.settings_spec)
  const [configuring, setConfiguring] = useState(false)
  const [values, setValues] = useState<SettingValues>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const run = install.last_run

  async function act(fn: () => Promise<unknown>): Promise<boolean> {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await reload()
      return true
    } catch (e) {
      setError(errorText(e, 'Something went wrong'))
      return false
    } finally {
      setBusy(false)
    }
  }

  async function accept() {
    setBusy(true)
    setError(null)
    try {
      await acceptLocalAppChanges(install.id, install.digest)
      await reload()
    } catch (e) {
      setError(errorText(e, 'Failed to accept the changes'))
      // Another version arrived since this was drawn: show its changes, and its digest for the next click.
      if (e instanceof ApiError && e.status === 409) await reload().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (await act(() => configureLocalApp(install.id, settingsToSend(spec, values, install.settings)))) setConfiguring(false)
  }

  function remove() {
    if (window.confirm(`Remove ${install.name} from ${install.computer.name}? A running copy stops at its next check-in.`)) {
      void act(() => removeLocalApp(install.id))
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3 text-[13px]" data-testid="local-app-installed">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <span className="font-medium">{install.computer.name}</span>
          <span className="ml-2 text-xs text-muted-foreground">{install.version ? `version ${install.version}` : 'no version to run'} · {describeRun(run)}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {run?.running && (
            <Button size="sm" variant="outline" disabled={busy || run.stop_requested} onClick={() => void act(() => stopLocalAppRun(run.id))}>Stop</Button>
          )}
          {Object.keys(spec).length > 0 && (
            <Button size="sm" variant="outline" disabled={busy} aria-expanded={configuring}
              onClick={() => { setValues(initialSettings(spec, install.settings)); setConfiguring(!configuring) }}>
              Configure
            </Button>
          )}
          <Button size="sm" variant="outline" disabled={busy} className="text-destructive" onClick={remove}>Remove</Button>
        </div>
      </div>

      {install.refusal && <p className="mt-2 text-warning" data-testid="local-app-refusal">{install.refusal}</p>}
      {error && <p className="mt-2 text-destructive" role="alert">{error}</p>}

      {install.needs_acceptance && (
        <div className="mt-3 rounded-md border border-warning/30 bg-warning/10 p-3" data-testid="local-app-acceptance">
          <p className="font-medium">A new version changes what this app reads or sends</p>
          {install.version && <p className="mt-0.5 text-muted-foreground">This computer stays on {install.version} until you accept.</p>}
          <ul className="mt-1 list-disc pl-5">{install.changes.map((c) => <li key={c}>{c}</li>)}</ul>
          <Button size="sm" className="mt-2" disabled={busy} onClick={() => void accept()}>Accept</Button>
        </div>
      )}

      {configuring && (
        <form className="mt-3 space-y-3 border-t border-border pt-3" onSubmit={(e) => void save(e)} aria-label="Configure">
          <SettingsFields spec={spec} agents={install.agents} values={values} onChange={setValues} />
          <Button type="submit" size="sm" disabled={busy}>Save</Button>
        </form>
      )}
    </div>
  )
}
