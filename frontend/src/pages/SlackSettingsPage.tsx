import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'
import {
  allowSlackChannel,
  clearSlackConfigToken,
  disallowSlackChannel,
  getSlackConfig,
  setSlackChannelIngest,
  setSlackConfigToken,
  setSlackHistory,
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
  // The history form's draft — seeded from the server, saved explicitly.
  const [historyOn, setHistoryOn] = useState(false)
  const [historyMinutes, setHistoryMinutes] = useState('')
  // The allow-list form's draft.
  const [allowChannel, setAllowChannel] = useState('')
  const [allowAgent, setAllowAgent] = useState('')
  const [allowNote, setAllowNote] = useState('')

  const load = useCallback(() => {
    getSlackConfig(workspace)
      .then((c) => {
        setConfig(c)
        setHistoryOn(c.history.enabled)
        setHistoryMinutes(String(c.history.max_minutes))
      })
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
      <div className="max-w-3xl">
        <WorkbenchSubHeader title="Slack" />
        <WorkbenchSkeleton />
      </div>
    )
  }
  const cmds = config.commands

  return (
    <div className="max-w-3xl" data-testid="slack-settings">
      <WorkbenchSubHeader title="Slack" />

      <section className="mb-8">
        <h2 className="text-[15px] font-semibold text-foreground">Connection</h2>
        {config.connected ? (
          <p className="mt-1 text-[13px] text-foreground-secondary">
            Connected to <b>{config.team_name || 'Slack'}</b>
            {config.installed_by_email ? ` by ${config.installed_by_email}` : ''}
            {config.installed_at ? `, ${relativeAge(config.installed_at)}` : ''}.
          </p>
        ) : (
          <p className="mt-1 text-[13px] text-foreground-secondary">
            Not connected.{' '}
            <a href={config.install_url} className="text-primary hover:underline">Connect Slack</a> (workspace
            owners).
          </p>
        )}
        {config.reinstall.needed && (
          <div className="mt-3 rounded-lg border border-border bg-card p-4 text-[13px]" data-testid="slack-reinstall">
            <p className="text-foreground">
              Slack needs a reinstall to turn on: {config.reinstall.turns_on.join('; ')}. Workspace owners can
              do this.
            </p>
            <a
              href={config.install_url}
              className="mt-3 inline-block rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground"
            >
              Reinstall
            </a>
          </div>
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
        <section className="mt-8" data-testid="slack-history">
          <h2 className="text-[15px] font-semibold text-foreground">Channel history</h2>
          <p className="mt-1 mb-3 text-[12px] text-muted-foreground">
            Lets someone start an agent on a conversation that already happened:{' '}
            <code>@canopy &lt;agent&gt; --history --min 30 &lt;ask&gt;</code> hands the agent the last 30 minutes of
            the channel it was asked in (only that channel, and only when asked); <code>--history --msgs 5</code> hands
            over its last 5 messages, and <code>--history 9am</code> reads back to 9:00 on the asker&apos;s own clock
            (their Slack timezone). Every way, the limit below applies. Workspace owners set this.
          </p>
          <form
            className="rounded-lg border border-border bg-card p-4 text-[13px]"
            onSubmit={(e) => {
              e.preventDefault()
              const minutes = Number(historyMinutes)
              void act(async () => {
                const c = await setSlackHistory(workspace, historyOn, minutes)
                return c.history.enabled
                  ? `History reads allowed, up to ${c.history.max_minutes} minutes back.`
                  : 'History reads are off.'
              })
            }}
          >
            <label className="flex items-center gap-2 text-foreground">
              <input
                type="checkbox"
                checked={historyOn}
                onChange={(e) => setHistoryOn(e.target.checked)}
                aria-label="Allow history read"
              />
              Allow history read
            </label>
            <label className="mt-3 flex items-center gap-2 text-foreground-secondary">
              How far back
              <input
                type="number"
                min={1}
                max={1440}
                step={1}
                value={historyMinutes}
                disabled={!historyOn}
                onChange={(e) => setHistoryMinutes(e.target.value)}
                aria-label="How far back, in minutes"
                className="w-24 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground disabled:opacity-50"
              />
              minutes <span className="text-[11px] text-muted-foreground">(at most 1440 — a day)</span>
            </label>
            <button
              type="submit"
              disabled={
                busy ||
                (historyOn === config.history.enabled && historyMinutes === String(config.history.max_minutes)) ||
                !Number.isInteger(Number(historyMinutes)) ||
                Number(historyMinutes) < 1 ||
                Number(historyMinutes) > 1440
              }
              className="mt-3 rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
            >
              Save
            </button>
          </form>
        </section>
      )}

      {config.connected && (
        <section className="mt-8" data-testid="slack-channel-ingest">
          <h2 className="text-[15px] font-semibold text-foreground">Reading whole channels</h2>
          <p className="mt-1 mb-3 text-[12px] text-muted-foreground">
            An agent can read a channel&apos;s full history, threads included, to learn from it. canopy does the
            reading and gives the agent a transcript. Nothing is read without permission, given one of two ways.
          </p>
          <div className="rounded-lg border border-border bg-card p-4 text-[13px]" data-testid="slack-channel-allowlist">
            <h3 className="text-[13px] font-semibold text-foreground">Channels agents may read</h3>
            <p className="mt-1 text-[12px] text-muted-foreground">
              Add a channel and the agent that may read it. Nothing is read until someone types{' '}
              <code>/&lt;agent&gt; read-channel &lt;ask&gt;</code> in that channel: canopy then reads everything since
              the agent&apos;s last read there (all of it, the first time) and the agent answers in a thread. The agent
              can&apos;t start a read itself. Removing a channel deletes what was read. Owners and admins can change
              this list.
            </p>
            {config.channel_ingest.allowed.length === 0 ? (
              <p className="mt-1 text-[12px] text-muted-foreground">None yet.</p>
            ) : (
              <ul className="mt-2 divide-y divide-border">
                {Array.from(config.channel_ingest.allowed).map((a) => (
                  <li key={a.id} className="flex items-center justify-between gap-3 py-2">
                    <span className="text-foreground">
                      <b>#{a.channel_name || a.channel}</b> for <b>{a.agent}</b>
                      {a.follow ? ' — read and follow' : ''}
                      <span className="block text-[11px] text-muted-foreground">
                        {a.note ? `${a.note} · ` : ''}added by {a.added_by_email || 'someone'}{' '}
                        {a.added_at ? relativeAge(a.added_at) : ''}
                      </span>
                    </span>
                    <button
                      type="button"
                      disabled={busy}
                      aria-label={`Remove #${a.channel_name || a.channel} for ${a.agent}`}
                      onClick={() =>
                        void act(async () => {
                          await disallowSlackChannel(workspace, a.id)
                          return `Removed #${a.channel_name || a.channel} for ${a.agent}; its reads were revoked.`
                        })
                      }
                      className="rounded-md border border-border px-2 py-1 text-[12px] text-foreground disabled:opacity-50"
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <form
              className="mt-3 flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                const channel = allowChannel.trim()
                const agent = allowAgent.trim()
                if (!channel || !agent) return
                void act(async () => {
                  await allowSlackChannel(workspace, { agent, channel, follow: false, note: allowNote.trim() })
                  setAllowChannel('')
                  setAllowNote('')
                  return `${agent} may now read ${channel}.`
                })
              }}
            >
              <input
                value={allowChannel}
                onChange={(e) => setAllowChannel(e.target.value)}
                placeholder="#channel or ID"
                aria-label="Channel"
                className="w-40 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground"
              />
              <input
                value={allowAgent}
                onChange={(e) => setAllowAgent(e.target.value)}
                placeholder="agent"
                aria-label="Agent"
                className="w-32 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground"
              />
              <input
                value={allowNote}
                onChange={(e) => setAllowNote(e.target.value)}
                placeholder="why (optional)"
                aria-label="Why"
                className="w-48 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground"
              />
              <button
                type="submit"
                disabled={busy || !allowChannel.trim() || !allowAgent.trim()}
                className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
              >
                Add
              </button>
            </form>
          </div>
          <div className="mt-3 rounded-lg border border-border bg-card p-4 text-[13px]">
            <h3 className="text-[13px] font-semibold text-foreground">Ask in the channel</h3>
            <p className="mt-1 text-[12px] text-muted-foreground">
              For a channel not on the list, an agent can ask instead. canopy posts Approve and Decline buttons in
              the channel, and an owner or admin who is in it, or whoever created it, decides. Owners turn this on.
            </p>
            <p className="mt-2 text-foreground">
              {config.channel_ingest.enabled ? 'On — agents can ask.' : 'Off — agents can only read channels on the list.'}
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void act(async () => {
                  const c = await setSlackChannelIngest(workspace, !config.channel_ingest.enabled)
                  return c.channel_ingest.enabled
                    ? 'Agents can now ask to read a channel; someone in the channel still has to approve each one.'
                    : 'Asking is off; reads that were approved this way stop.'
                })
              }
              className="mt-3 rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
            >
              {config.channel_ingest.enabled ? 'Turn off' : 'Turn on'}
            </button>
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
