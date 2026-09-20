import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'
import {
  clearSlackConfigToken,
  declareSlackAgent,
  getSlackConfig,
  setSlackConfigToken,
  syncSlackCommands,
  syncSummary,
  type SlackConfigOut,
} from '@/api/slack'
import { relativeAge } from '@/lib/relativeAge'

// The workspace's Slack connection, and the one credential that lets canopy
// keep the Slack app's `/<agent>` commands in step with each agent's Slack
// switch. Slack has no wildcard command — each one lives in the app's own
// config — so without this, turning an agent on for Slack does not make its
// command exist.
export function SlackSettingsPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [config, setConfig] = useState<SlackConfigOut | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)

  const load = useCallback(() => {
    getSlackConfig(workspace)
      .then(setConfig)
      .catch((e: unknown) => setNote({ tone: 'error', text: e instanceof Error ? e.message : 'Failed to load' }))
  }, [workspace])
  useEffect(load, [load])

  const act = async (fn: () => Promise<string>) => {
    setBusy(true)
    setNote(null)
    try {
      setNote({ tone: 'ok', text: await fn() })
    } catch (e: unknown) {
      setNote({ tone: 'error', text: e instanceof Error ? e.message : 'Failed' })
    } finally {
      setBusy(false)
      load()
    }
  }

  const connect = (e: FormEvent) => {
    e.preventDefault()
    const value = token.trim()
    if (!value) return
    void act(async () => {
      const r = await setSlackConfigToken(workspace, value)
      setToken('') // never keep a secret in component state once it has landed
      return syncSummary(r)
    })
  }

  if (config === null) {
    return (
      <div className="max-w-3xl px-6 py-8">
        <WorkbenchSubHeader title="Slack" />
        <WorkbenchSkeleton />
      </div>
    )
  }
  const cmds = config.commands

  return (
    <div className="max-w-3xl px-6 py-8" data-testid="slack-settings">
      <WorkbenchSubHeader title="Slack" />

      <section className="mb-8">
        <h2 className="text-[15px] font-semibold text-foreground">Connection</h2>
        {config.connected ? (
          <p className="mt-1 text-[13px] text-foreground-secondary">
            Connected to <b>{config.team_name || 'Slack'}</b>
            {config.installed_by_email ? ` by ${config.installed_by_email}` : ''}
            {config.installed_at ? `, ${relativeAge(config.installed_at)}` : ''}.{' '}
            <a href={config.install_url} className="text-primary hover:underline">Reinstall</a> after changing the
            app&apos;s permissions.
          </p>
        ) : (
          <p className="mt-1 text-[13px] text-foreground-secondary">
            Not connected.{' '}
            <a href={config.install_url} className="text-primary hover:underline">Connect Slack</a> (workspace
            owners).
          </p>
        )}
      </section>

      {config.connected && (
        <section>
          <h2 className="text-[15px] font-semibold text-foreground">Slash commands</h2>
          <p className="mt-1 mb-3 text-[12px] text-muted-foreground">
            Each agent switched on for Slack gets <code>/&lt;its name&gt;</code> in Slack — added and removed
            automatically when you flip the switch. <code>/canopy &lt;agent&gt;</code> always works. Every command is
            visible to everyone in the Slack workspace.
          </p>

          {cmds.managed ? (
            <div className="rounded-lg border border-border bg-card p-4 text-[13px]">
              <p className="text-foreground">
                canopy manages the app&apos;s commands
                {cmds.set_by_email ? <> (set up by {cmds.set_by_email})</> : null}.
                {cmds.synced_at ? <> Last synced {relativeAge(cmds.synced_at)}.</> : null}
              </p>
              {cmds.error && <p className="mt-1 text-destructive">Last sync failed: {cmds.error}</p>}
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void act(async () => syncSummary(await syncSlackCommands(workspace)))}
                  className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
                >
                  Sync now
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm('Stop managing the Slack app\'s commands? Existing commands stay as they are.')) {
                      void act(async () => {
                        await clearSlackConfigToken(workspace)
                        return 'canopy no longer manages the commands.'
                      })
                    }
                  }}
                  className="rounded-md border border-border px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50"
                >
                  Disconnect
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={connect} className="rounded-lg border border-border bg-card p-4 text-[13px]" data-testid="slack-config-form">
              {!cmds.app_id && (
                <p className="mb-3 text-warning">
                  canopy doesn&apos;t know this Slack app&apos;s id yet — <a href={config.install_url} className="underline">reinstall</a> once
                  and it will.
                </p>
              )}
              <ol className="mb-3 list-decimal space-y-1 pl-5 text-foreground-secondary">
                <li>
                  Open <a href="https://api.slack.com/apps" target="_blank" rel="noreferrer" className="text-primary hover:underline">api.slack.com/apps</a>{' '}
                  (the list page, not the app) and scroll to <b>Your App Configuration Tokens</b>.
                </li>
                <li>Click <b>Generate Token</b> for this Slack workspace.</li>
                <li>Copy the <b>Refresh Token</b> (starts <code>xoxe-</code>) and paste it here. canopy rotates it at once and keeps it encrypted.</li>
              </ol>
              <div className="flex gap-2">
                <input
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="xoxe-1-…"
                  aria-label="Slack app configuration refresh token"
                  className="min-w-0 flex-1 rounded-md border border-input bg-input px-3 py-1.5 font-mono text-[12px] text-foreground"
                />
                <button
                  type="submit"
                  disabled={busy || !token.trim() || !cmds.app_id}
                  className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
                >
                  Connect
                </button>
              </div>
              <p className="mt-2 text-[11px] text-muted-foreground">Workspace owners only.</p>
            </form>
          )}
        </section>
      )}

      {config.connected && (
        <section className="mt-8">
          <h2 className="text-[15px] font-semibold text-foreground">Working indicator</h2>
          <p className="mt-1 mb-3 text-[12px] text-muted-foreground">
            Declaring the app an agent lets Slack draw its own <b>Working…</b> indicator and a{' '}
            <b>Stop</b> button in the thread, instead of only the status line canopy posts. Two things
            to know first: agent conversations in DMs move to the app&apos;s Messages tab, and Slack does
            not allow this to be swapped back to the older assistant experience.
          </p>
          <div className="rounded-lg border border-border bg-card p-4 text-[13px]">
            {config.agent?.declared ? (
              <p className="text-foreground">
                Declared an agent{config.agent?.declared_at ? <> {relativeAge(config.agent.declared_at)}</> : null}. If
                you have not re-installed since,{' '}
                <a href={config.install_url} className="text-primary hover:underline">do that once</a> — the
                permission it needs only lands on a fresh install.
              </p>
            ) : (
              <>
                <p className="text-foreground-secondary">
                  Not declared, so Slack shows no indicator of its own. canopy&apos;s status line works either way.
                </p>
                <button
                  type="button"
                  disabled={busy || !cmds.managed}
                  onClick={() => {
                    if (
                      window.confirm(
                        'Declare this Slack app an agent? DM conversations move to the app\'s Messages tab, and Slack cannot switch this back to the older assistant experience.',
                      )
                    ) {
                      void act(async () => {
                        const r = await declareSlackAgent(workspace)
                        return r.changed.length ? `${r.detail} Changed: ${r.changed.join('; ')}.` : r.detail
                      })
                    }
                  }}
                  className="mt-3 rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
                >
                  Declare as agent
                </button>
                {!cmds.managed && (
                  <p className="mt-2 text-[11px] text-muted-foreground">
                    Needs the app configuration token above — canopy edits the app&apos;s manifest to do this.
                  </p>
                )}
              </>
            )}
          </div>
        </section>
      )}

      {note && (
        <p className={`mt-4 text-[13px] ${note.tone === 'ok' ? 'text-success' : 'text-destructive'}`} data-testid="slack-note">
          {note.text}
        </p>
      )}
    </div>
  )
}
