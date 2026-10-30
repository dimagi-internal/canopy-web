import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { createSession, sendMessage } from '@/api/chat'
import {
  addHelper,
  listHelpers,
  type AppHelper,
  retentionApply,
  retentionPreview,
  subjectExport,
  type RetentionRule,
  addChange,
  addGrant,
  discardDraft,
  getDraft,
  getInstalledApp,
  listAppStore,
  listGrants,
  listVersions,
  publishDraft,
  removeChange,
  removeGrant,
  rollbackTo,
  runChecks,
  startDraft,
  type AppDraft,
  type AppGrant,
  type AppVersionRow,
  type InstalledApp,
} from '@/api/workspaceApps'
import { listMembers, type MemberOut } from '@/api/workspaces'
import { AddPeoplePicker, type Candidate } from '@/components/people/AddPeoplePicker'
import { roleOptions } from '@/components/people/roles'
import { useScopedPath } from '@/lib/scopedLinks'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

/**
 * Customize an installed app — built for someone who is not a developer (the
 * head of HR for Hiring). Every change goes into a DRAFT: described in plain
 * words (to an agent, or with the simple forms here), reviewed as a list of
 * plain-English changes, tried on the draft's TEST COPY (sample data, nothing
 * sent), checked, then published — and any earlier version can be restored.
 * The changes are kept apart from the template, so the template's updates still
 * arrive: as a draft of their own, tested the same way. Design:
 * docs/architecture/workspace-apps.md.
 */

type Manifest = {
  settings: Record<string, { type: string; label: string; help?: string; value?: unknown; default?: unknown }>
  collections: Record<string, { label?: string; fields: Record<string, { type: string; label?: string; options?: string[]; extensible?: boolean; fixed_first?: string; fixed_last?: string; origin?: string }> }>
  actions: Record<string, { label: string; role: string; collection: string; input: string[]; origin?: string; enabled?: boolean }>
  pages: { slug: string; title: string; audience: string; nav: boolean }[]
}

function Section({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <h2 className="font-semibold">{title}</h2>
        {actions}
      </header>
      <div className="p-4">{children}</div>
    </section>
  )
}

const BTN = 'rounded-md border border-border px-3 py-1.5 text-sm font-medium hover:bg-muted disabled:opacity-50'
const PRIMARY = 'rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-80 disabled:opacity-50'
const INPUT = 'w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm'

export default function WorkspaceAppCustomizePage() {
  const { workspace = '', appKey = '' } = useParams()
  const scoped = useScopedPath()
  const role = useWorkspaceList().find((w) => w.slug === workspace)?.role ?? ''
  const mayGrant = roleAllows(role, 'members.manage')
  const [app, setApp] = useState<InstalledApp | null>(null)
  const [draft, setDraft] = useState<AppDraft | null>(null)
  const [draftApp, setDraftApp] = useState<InstalledApp | null>(null)
  const [versions, setVersions] = useState<AppVersionRow[]>([])
  const [changelog, setChangelog] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    const a = await getInstalledApp(workspace, appKey)
    setApp(a)
    const d = a.may_maintain ? await getDraft(workspace, appKey) : null
    setDraft(d)
    setDraftApp(d ? await getInstalledApp(workspace, appKey, d.id) : null)
    if (a.may_maintain) setVersions(await listVersions(workspace, appKey))
    if (a.upgrade_available) {
      const store = await listAppStore(workspace)
      setChangelog(store.find((s) => s.key === a.template)?.changelog ?? '')
    }
  }, [workspace, appKey])
  useEffect(() => {
    load().catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [load])

  async function run(fn: () => Promise<unknown>, done?: string) {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await fn()
      await load()
      if (done) setNotice(done)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (error && !app) return <div className="p-6 text-sm text-destructive">{error}</div>
  if (!app) return <div className="p-6 text-sm text-muted-foreground">Loading…</div>
  if (!app.may_maintain) {
    return <div className="p-6 text-sm text-muted-foreground">Customizing {app.name} needs its maintainer role. Ask a workspace admin.</div>
  }
  const m = (draftApp?.manifest ?? app.manifest) as unknown as Manifest
  const testHref = (slug: string) => scoped(`/apps/${app.key}/${slug}?draft=${draft?.id}`)

  return (
    <div className="mx-auto max-w-5xl space-y-4 p-6">
      <header>
        <div className="text-xs text-muted-foreground">
          <Link className="hover:underline" to={scoped('/apps')}>Apps</Link> · <Link className="hover:underline" to={scoped(`/apps/${app.key}`)}>{app.name}</Link>
        </div>
        <h1 className="text-2xl font-semibold">Customize {app.name}</h1>
        <p className="text-sm text-muted-foreground">
          Live: version {app.live_version}, on the {app.template} template {app.template_version}. Your changes are kept apart from the
          template, so its updates still reach you.
        </p>
      </header>
      {error && <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
      {notice && <div className="rounded-md border border-border bg-muted p-3 text-sm">{notice}</div>}

      {!draft && app.upgrade_available && (
        <Section title={`Update available: ${app.template} ${app.upgrade_available}`}>
          {changelog && <p className="mb-3 whitespace-pre-wrap text-sm text-muted-foreground">{changelog}</p>}
          <p className="mb-3 text-sm">Your changes are carried onto the new version in a draft. Anything that no longer fits is set aside for you to look at — nothing breaks the live app.</p>
          <button type="button" className={PRIMARY} disabled={busy}
            onClick={() => run(() => startDraft(workspace, app.key, { upgrade: true, note: `update to ${app.upgrade_available}` }))}>
            Try the update in a draft
          </button>
        </Section>
      )}

      {!draft ? (
        <Section title="Make a change">
          <p className="mb-3 text-sm text-muted-foreground">
            A change starts as a draft with its own test copy — sample candidates, nothing emailed — so you can try it before anyone else sees it.
          </p>
          <AskAgent workspace={workspace} app={app} draftId={null} busy={busy}
            onAsk={(text, agent) => run(async () => {
              await startDraft(workspace, app.key, { note: text.slice(0, 200) })
              await ask(workspace, app, text, agent)
            }, 'Draft started, and the agent is on it. Its changes appear below as it makes them.')} />
          <div className="mt-3">
            <button type="button" className={BTN} disabled={busy} onClick={() => run(() => startDraft(workspace, app.key))}>
              Or start a draft and change it yourself
            </button>
          </div>
        </Section>
      ) : (
        <DraftPanel workspace={workspace} app={app} draft={draft} m={m} busy={busy} run={run} testHref={testHref}
          onPoll={async () => {
            const d = await getDraft(workspace, app.key)
            setDraft(d)
            if (d) setDraftApp(await getInstalledApp(workspace, app.key, d.id))
          }} />
      )}

      <Section title="Versions">
        <table className="w-full text-sm">
          <thead><tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th className="py-1.5">Version</th><th>What</th><th>Template</th><th>Changes</th><th>Published</th><th />
          </tr></thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.number} className="border-b border-border last:border-0">
                <td className="py-1.5">{v.number} {v.status === 'published' && <span className="ml-1 rounded bg-success/15 px-1.5 text-xs text-success">live</span>}{v.status === 'draft' && <span className="ml-1 rounded bg-warning/15 px-1.5 text-xs">draft</span>}</td>
                <td>{v.origin}{v.note ? ` — ${v.note}` : ''}</td>
                <td>{v.template_version}</td>
                <td>{v.changes}</td>
                <td>{v.published_at ? `${v.published_at.slice(0, 10)} by ${v.published_by ?? '—'}` : '—'}</td>
                <td className="text-right">
                  {(v.status === 'superseded') && !draft && (
                    <button type="button" className="text-primary hover:underline" disabled={busy}
                      onClick={() => window.confirm(`Make version ${v.number} live again? (It becomes a new version; nothing is lost.)`)
                        && run(() => rollbackTo(workspace, app.key, v.number), `Version ${v.number} is live again.`)}>
                      Restore
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      {mayGrant && <Maintainers workspace={workspace} app={app} />}
      {roleAllows(role, 'integrations') && <Privacy workspace={workspace} app={app} />}
    </div>
  )
}

/** The app's retention rules (what the clock will anonymize or delete) and a
 * data-subject export — the parts of privacy an admin acts on, for any app. */
function Privacy({ workspace, app }: { workspace: string; app: InstalledApp }) {
  const [rules, setRules] = useState<RetentionRule[] | null>(null)
  const [email, setEmail] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const reload = useCallback(() => {
    retentionPreview(workspace, app.key).then(setRules, (e) => setMsg(String(e instanceof Error ? e.message : e)))
  }, [workspace, app.key])
  useEffect(reload, [reload])
  if (rules === null && !msg) return null
  return (
    <Section title="Privacy">
      <p className="text-sm text-muted-foreground">
        These rules run on their own twice a day. Anonymized records keep their counts for reports, without anything that identifies the person.
      </p>
      {rules && rules.length > 0 ? (
        <table className="mt-3 w-full text-sm">
          <thead><tr className="text-left text-muted-foreground"><th className="py-1">Rule</th><th>Due now</th><th>Examples</th></tr></thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.name} className="border-t">
                <td className="py-1.5">{r.label}</td>
                <td>{r.count} to {r.does}</td>
                <td className="text-muted-foreground">{r.examples.map((x) => x.title).filter(Boolean).slice(0, 3).join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="mt-2 text-sm">This app has no retention rules.</p>}
      {rules && rules.some((r) => r.count > 0) && (
        <button type="button" className={`${BTN} mt-3`} onClick={async () => {
          if (!window.confirm('Anonymize or delete these records now? This cannot be undone.')) return
          try {
            const done = await retentionApply(workspace, app.key)
            setMsg(`Done: ${Object.entries(done).map(([k, n]) => `${k} ${n}`).join(', ')}`)
            reload()
          } catch (e) {
            setMsg(e instanceof Error ? e.message : String(e))
          }
        }}>Run the rules now</button>
      )}
      <div className="mt-5 flex flex-wrap items-end gap-2">
        <label className="block text-sm">
          <span className="mb-1 block font-medium">Export everything about one person (a data request)</span>
          <input className={INPUT} type="email" placeholder="their email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <button type="button" className={BTN} disabled={!email} onClick={async () => {
          try {
            const data = await subjectExport(workspace, app.key, email)
            const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }))
            const a = document.createElement('a')
            a.href = url
            a.download = `${app.key}-${email}.json`
            a.click()
            URL.revokeObjectURL(url)
          } catch (e) {
            setMsg(e instanceof Error ? e.message : String(e))
          }
        }}>Download</button>
      </div>
      {msg && <p className="mt-2 text-sm">{msg}</p>}
    </Section>
  )
}

/** Let the agent onto the open draft, then open a chat with it where it lives (its
 * home workspace, which need not be the app's) and hand it the request. */
async function ask(workspace: string, app: InstalledApp, text: string, agent: AppHelper) {
  await addHelper(workspace, app.key, agent.slug)
  const session = await createSession({ workspace: agent.home, agentSlug: agent.slug, title: `Customize ${app.name}` })
  const page = customizeUrl(workspace, app.key)
  await sendMessage(session.id, [
    `Please change the ${app.name} app in the "${workspace}" workspace for me. Here is what I want, in my words:`,
    '',
    `> ${text.split('\n').join('\n> ')}`,
    '',
    `How: read the app's guide first (tool get_workspace_app_guide, workspace="${workspace}", key="${app.key}"), then add each`,
    'change to the open draft with add_workspace_app_change, putting my words in its `intent`. Prefer adding fields,',
    'options, settings and slot fills over replacing pages. Do NOT publish — I will try the draft\'s test copy and publish',
    `it myself at ${page}. Tell me in plain words what you changed and anything you could not do.`,
  ].join('\n'))
  return session
}

function customizeUrl(workspace: string, key: string) {
  return `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}/w/${encodeURIComponent(workspace)}/apps/${key}/customize`
}

const MCP_URL = `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}/api/mcp/`

function AskAgent({ workspace, app, draftId, busy, onAsk }: {
  workspace: string; app: InstalledApp; draftId: number | null; busy: boolean; onAsk: (text: string, agent: AppHelper) => void
}) {
  const [helpers, setHelpers] = useState<AppHelper[] | null>(null)
  const [agent, setAgent] = useState('')
  const [text, setText] = useState('')
  useEffect(() => {
    listHelpers(workspace, app.key).then((rows) => {
      setHelpers(rows)
      setAgent((cur) => cur || rows.find((h) => h.reachable)?.slug || '')
    }, () => setHelpers([]))
  }, [workspace, app.key])
  const reachable = (helpers ?? []).filter((h) => h.reachable)
  const outside = (helpers ?? []).filter((h) => !h.reachable)
  const picked = reachable.find((h) => h.slug === agent)
  return (
    <div className="space-y-2">
      <label className="block text-sm font-medium" htmlFor="ask">Describe what you want, in your own words</label>
      <textarea id="ask" className={INPUT} rows={3} value={text} onChange={(e) => setText(e.target.value)}
        placeholder={`e.g. “Ask applicants whether they need visa sponsorship, and add a Phone screen stage before Interview.”`} />
      <div className="flex flex-wrap items-center gap-2">
        {reachable.length > 0 ? (
          <>
            <select aria-label="Agent to ask" className="rounded-md border border-border bg-background px-2 py-1.5 text-sm" value={agent} onChange={(e) => setAgent(e.target.value)}>
              {reachable.map((a) => <option key={a.slug} value={a.slug}>{a.name}{a.home !== workspace ? ` (from ${a.home})` : ''}</option>)}
            </select>
            <button type="button" className={PRIMARY} disabled={busy || !text.trim() || !picked}
              onClick={() => { if (picked) { onAsk(text.trim(), picked); setText('') } }}>
              {draftId ? 'Ask' : 'Start a draft and ask'}
            </button>
          </>
        ) : helpers === null ? (
          <span className="text-sm text-muted-foreground">Looking for agents you can ask…</span>
        ) : (
          <span className="text-sm text-muted-foreground">No agent can reach this workspace yet. Use your own Claude (below), or the forms.</span>
        )}
      </div>
      {outside.length > 0 && reachable.length === 0 && (
        <p className="text-xs text-muted-foreground">
          {outside.slice(0, 3).map((h) => h.name).join(', ')}{outside.length > 3 ? ' and others' : ''} could help, but{' '}
          {outside.length === 1 ? 'is' : 'are'} not a member of this workspace. A workspace admin adds{' '}
          {outside.slice(0, 3).map((h) => h.login).join(', ')} as a member (a viewer is enough).
        </p>
      )}
      {reachable.length > 0 && (
        <p className="text-xs text-muted-foreground">
          The agent works in a chat you can follow. It may change and test only this draft, for as long as the draft is open,
          and never publishes: you try the test copy and publish it yourself.
        </p>
      )}
      {helpers !== null && <OwnClaude workspace={workspace} app={app} open={reachable.length === 0} />}
    </div>
  )
}

/** The other way in: the person's own Claude (or any MCP client), signed in as them
 * to canopy's MCP server, where every route of this page is a tool. */
function OwnClaude({ workspace, app, open }: { workspace: string; app: InstalledApp; open: boolean }) {
  const [copied, setCopied] = useState('')
  const prompt = [
    `Change the ${app.name} app in my canopy workspace "${workspace}" (app key "${app.key}").`,
    `First read its guide with get_workspace_app_guide. Start a draft with start_workspace_app_draft if none is open,`,
    `add each change with add_workspace_app_change (put my words in "intent"), then run check_workspace_app_draft.`,
    `Do not publish: I will try the test copy and publish at ${customizeUrl(workspace, app.key)}`,
    ``,
    `What I want: `,
  ].join('\n')
  async function copy(what: string, value: string) {
    try { await navigator.clipboard.writeText(value); setCopied(what) } catch { setCopied('') }
  }
  return (
    <details className="mt-4 rounded-md border border-border p-3 text-sm" open={open}>
      <summary className="cursor-pointer font-medium">{open ? 'Use your own Claude' : 'Or use your own Claude'}</summary>
      <ol className="mt-2 list-decimal space-y-2 pl-5 text-muted-foreground">
        <li>
          In Claude, add a custom connector with this address, and sign in to canopy when it asks:
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <code className="rounded bg-muted px-2 py-1 text-xs text-foreground">{MCP_URL}</code>
            <button type="button" className="text-xs text-primary hover:underline" onClick={() => copy('url', MCP_URL)}>{copied === 'url' ? 'Copied' : 'Copy'}</button>
          </div>
        </li>
        <li>
          Start a chat with this, and finish the last line in your own words:
          <pre className="mt-1 whitespace-pre-wrap rounded bg-muted p-2 text-xs text-foreground">{prompt}</pre>
          <button type="button" className="text-xs text-primary hover:underline" onClick={() => copy('prompt', prompt)}>{copied === 'prompt' ? 'Copied' : 'Copy'}</button>
        </li>
        <li>Its changes appear in the draft here, like anyone else's. It acts as you, so it can do only what you can.</li>
      </ol>
    </details>
  )
}

function DraftPanel({ workspace, app, draft, m, busy, run, testHref, onPoll }: {
  workspace: string; app: InstalledApp; draft: AppDraft; m: Manifest; busy: boolean
  run: (fn: () => Promise<unknown>, done?: string) => Promise<void>; testHref: (slug: string) => string
  onPoll: () => Promise<void>
}) {
  const r = draft.review
  const [polling, setPolling] = useState(0)
  // After asking an agent, watch the draft for its changes for a few minutes.
  useEffect(() => {
    if (!polling) return
    const t = setInterval(() => { onPoll().catch(() => {}) }, 5000)
    const stop = setTimeout(() => setPolling(0), 5 * 60 * 1000)
    return () => { clearInterval(t); clearTimeout(stop) }
  }, [polling, onPoll])
  const checks = r.checks
  const updateRisks = r.template_change?.risks ?? []
  const needsAdmin = (r.risks.length > 0 || updateRisks.length > 0) && !app.may_manage
  const pages = m.pages.filter((p) => p.nav)

  return (
    <>
      <Section title={`Draft v${draft.number}${draft.origin === 'upgrade' ? ` — update to template ${draft.template_version}` : ''}`}
        actions={<button type="button" className="text-sm text-destructive hover:underline" disabled={busy}
          onClick={() => window.confirm('Throw this draft and its test copy away?') && run(() => discardDraft(workspace, app.key), 'Draft discarded.')}>Discard draft</button>}>
        {r.template_change && (
          <div className="mb-4 rounded-md border border-primary/30 bg-primary/10 p-3 text-sm">
            <div className="font-medium">Template update {r.template_change.from} → {r.template_change.to}</div>
            {r.template_change.changelog && <div className="mt-1 whitespace-pre-wrap text-muted-foreground">{r.template_change.changelog}</div>}
            {updateRisks.length > 0 ? (
              <div className="mt-3 rounded-md border border-warning/40 bg-warning/10 p-2">
                <div className="font-medium">This update changes who can see or do what — a workspace admin publishes it:</div>
                <ul className="mt-1 list-disc pl-5">{updateRisks.map((x) => <li key={x}>{x}</li>)}</ul>
              </div>
            ) : (
              <div className="mt-2 text-xs text-muted-foreground">Compared with the version you run, this update changes no roles, no access to records or fields, nothing public, no email recipients and no retention.</div>
            )}
          </div>
        )}
        {r.quarantine.length > 0 && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <div className="font-medium text-destructive">{r.quarantine.length} of your changes no longer fit and were set aside</div>
            <ul className="mt-2 space-y-2">
              {r.quarantine.map((q) => (
                <li key={q.id} className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div>{q.summary}</div>
                    <div className="text-xs text-muted-foreground">Why: {q.reason}{q.intent ? ` · You asked: “${q.intent}”` : ''}</div>
                  </div>
                  <button type="button" className={BTN} disabled={busy} onClick={() => run(() => removeChange(workspace, app.key, q.id))}>Remove</button>
                </li>
              ))}
            </ul>
            <div className="mt-2 text-xs">Ask the agent to redo any of these the new way, then remove the old one.</div>
          </div>
        )}
        <h3 className="mb-2 text-sm font-semibold">Changes ({r.changes.length})</h3>
        {r.changes.length === 0 ? (
          <p className="text-sm text-muted-foreground">No changes yet — this is the app as the template ships it.</p>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border">
            {r.changes.map((c) => (
              <li key={c.id} className="flex flex-wrap items-start justify-between gap-2 px-3 py-2 text-sm">
                <div className="min-w-0">
                  <div>{c.summary} {!c.new && <span className="text-xs text-muted-foreground">(already live)</span>}
                    {c.risk && c.new && <span className="ml-1 rounded bg-warning/15 px-1.5 text-xs">needs an admin: {c.risk}</span>}</div>
                  {c.intent && <div className="text-xs text-muted-foreground">“{c.intent}” — {c.by}</div>}
                </div>
                <button type="button" className="text-xs text-muted-foreground hover:text-destructive" disabled={busy}
                  onClick={() => run(() => removeChange(workspace, app.key, c.id))}>Remove</button>
              </li>
            ))}
          </ul>
        )}
        {r.forked_pages.length > 0 && (
          <p className="mt-2 text-xs text-warning">Replaced pages ({r.forked_pages.join(', ')}) no longer receive template updates.</p>
        )}
      </Section>

      <Section title="Ask for a change">
        <AskAgent workspace={workspace} app={app} draftId={draft.id} busy={busy}
          onAsk={(text, agent) => run(async () => { await ask(workspace, app, text, agent); setPolling(Date.now()) },
            'The agent is on it. Its changes appear above as it makes them.')} />
      </Section>

      <Section title="Or change it yourself">
        <div className="grid gap-6 md:grid-cols-2">
          <AddQuestion m={m} busy={busy} add={(ops) => run(async () => { for (const op of ops) await addChange(workspace, app.key, op) }, 'Added to the draft.')} />
          <AddOption m={m} busy={busy} add={(op) => run(() => addChange(workspace, app.key, op), 'Added to the draft.')} />
        </div>
        <Settings m={m} busy={busy} save={(ops) => run(async () => { for (const op of ops) await addChange(workspace, app.key, op) }, 'Settings saved to the draft.')} />
      </Section>

      <Section title="Try it, then publish">
        <p className="mb-2 text-sm">Open the draft's test copy — sample candidates, nothing is emailed or made live:</p>
        <div className="mb-4 flex flex-wrap gap-2">
          {pages.map((p) => (
            <Link key={p.slug} className={BTN} to={testHref(p.slug)}>{p.title}{p.audience === 'public' ? ' (as a visitor)' : ''}</Link>
          ))}
        </div>
        <div className="mb-4">
          <div className="flex items-center gap-2">
            <button type="button" className={BTN} disabled={busy} onClick={() => run(() => runChecks(workspace, app.key))}>Run the checks</button>
            {checks.ran_at && <span className={`text-sm ${checks.ok ? 'text-success' : 'text-destructive'}`}>{checks.ok ? 'All checks pass.' : 'Some checks fail.'}</span>}
          </div>
          {checks.results && (
            <ul className="mt-2 space-y-1 text-sm">
              {checks.results.map((c) => (
                <li key={c.name}>{c.ok ? '✓' : '✗'} {c.name}{c.origin === 'workspace' ? ' (yours)' : ''}{c.error && <span className="block pl-4 text-xs text-destructive">{c.error}</span>}</li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className={PRIMARY} disabled={busy || r.quarantine.length > 0 || needsAdmin}
            onClick={() => run(() => publishDraft(workspace, app.key), 'Published — the draft is now the live app.')}>
            Publish
          </button>
          {needsAdmin && <span className="text-sm text-muted-foreground">This draft{updateRisks.length > 0 ? '’s template update' : ''} changes what the public sees or who can do what, so a workspace admin publishes it.</span>}
          {r.quarantine.length > 0 && <span className="text-sm text-muted-foreground">Deal with the set-aside changes first.</span>}
          <span className="text-xs text-muted-foreground">Publishing runs the checks again first.</span>
        </div>
      </Section>
    </>
  )
}

function AddQuestion({ m, busy, add }: { m: Manifest; busy: boolean; add: (ops: Record<string, unknown>[]) => void }) {
  const forms = useMemo(() => Object.entries(m.actions).filter(([, a]) => a.enabled !== false && a.input.length > 0), [m])
  const [action, setAction] = useState(forms.find(([k]) => k === 'apply')?.[0] ?? forms[0]?.[0] ?? '')
  const [label, setLabel] = useState('')
  const [type, setType] = useState('text')
  const [options, setOptions] = useState('')
  const [required, setRequired] = useState(false)
  const a = m.actions[action]
  const key = 'x_' + label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 36)
  const intent = `Add the question “${label}” to “${a?.label}”.`
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold">Add a question to a form</h3>
      <select className={INPUT} value={action} onChange={(e) => setAction(e.target.value)}>
        {forms.map(([k, f]) => <option key={k} value={k}>{f.label}{f.role === 'public' ? ' (public form)' : ''}</option>)}
      </select>
      <input className={INPUT} placeholder="The question, e.g. Do you need visa sponsorship?" value={label} onChange={(e) => setLabel(e.target.value)} />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <select className="rounded-md border border-border bg-background px-2 py-1.5" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="text">Short answer</option><option value="longtext">Long answer</option><option value="bool">Yes / no</option>
          <option value="enum">Choose one</option><option value="number">Number</option><option value="date">Date</option>
          <option value="url">Link</option><option value="file">File</option>
        </select>
        <label className="flex items-center gap-1"><input type="checkbox" checked={required} onChange={(e) => setRequired(e.target.checked)} /> Required</label>
      </div>
      {type === 'enum' && <input className={INPUT} placeholder="Choices, separated by commas" value={options} onChange={(e) => setOptions(e.target.value)} />}
      <button type="button" className={BTN} disabled={busy || key.length < 3 || !a}
        onClick={() => {
          const field: Record<string, unknown> = { type, label, required }
          if (type === 'enum') field.options = options.split(',').map((s) => s.trim()).filter(Boolean)
          add([{ op: 'add_field', collection: a.collection, key, field, intent },
            { op: 'set_action', action, add_input: [key], intent }])
          setLabel('')
          setOptions('')
        }}>Add question</button>
      {required && <p className="text-xs text-muted-foreground">A required question can make the checks fail if a form the app relies on doesn't ask it — run the checks to see.</p>}
    </div>
  )
}

function AddOption({ m, busy, add }: { m: Manifest; busy: boolean; add: (op: Record<string, unknown>) => void }) {
  const lists = useMemo(() => Object.entries(m.collections).flatMap(([c, col]) => Object.entries(col.fields)
    .filter(([, f]) => f.type === 'enum' && f.extensible)
    .map(([f, spec]) => ({ id: `${c}.${f}`, collection: c, field: f, label: `${spec.label ?? f} (${col.label ?? c})`, options: spec.options ?? [] }))), [m])
  const [id, setId] = useState(lists.find((l) => l.field === 'stage')?.id ?? lists[0]?.id ?? '')
  const [option, setOption] = useState('')
  const [before, setBefore] = useState('')
  const list = lists.find((l) => l.id === id)
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold">Add a stage or a choice to a list</h3>
      <select className={INPUT} value={id} onChange={(e) => { setId(e.target.value); setBefore('') }}>
        {lists.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
      </select>
      <input className={INPUT} placeholder="New choice, e.g. Phone screen" value={option} onChange={(e) => setOption(e.target.value)} />
      <select className={INPUT} value={before} onChange={(e) => setBefore(e.target.value)}>
        <option value="">At the end (before any fixed last step)</option>
        {list?.options.map((o) => <option key={o} value={o}>Before “{o}”</option>)}
      </select>
      <div className="text-xs text-muted-foreground">Now: {list?.options.join(' → ')}</div>
      <button type="button" className={BTN} disabled={busy || !option.trim() || !list}
        onClick={() => {
          add({ op: 'add_option', collection: list!.collection, field: list!.field, option: option.trim(), ...(before ? { before } : {}),
            intent: `Add “${option.trim()}” to ${list!.label}${before ? ` before “${before}”` : ''}.` })
          setOption('')
        }}>Add</button>
    </div>
  )
}

function Settings({ m, busy, save }: { m: Manifest; busy: boolean; save: (ops: Record<string, unknown>[]) => void }) {
  const current = useMemo(() => Object.fromEntries(Object.entries(m.settings).map(([k, s]) => [k, s.value ?? s.default ?? ''])), [m])
  const [values, setValues] = useState<Record<string, unknown>>(current)
  useEffect(() => setValues(current), [current])
  const changed = Object.keys(values).filter((k) => values[k] !== current[k])
  if (Object.keys(m.settings).length === 0) return null
  return (
    <div className="mt-6 space-y-3">
      <h3 className="text-sm font-semibold">Settings and wording</h3>
      <p className="text-xs text-muted-foreground">Emails can use {'{{name}}'}, {'{{job.title}}'} and {'{{settings.company_name}}'}.</p>
      <div className="grid gap-3 md:grid-cols-2">
        {Object.entries(m.settings).map(([k, s]) => (
          <label key={k} className={`block text-sm ${s.type === 'longtext' ? 'md:col-span-2' : ''}`}>
            <span className="mb-1 block font-medium">{s.label}</span>
            {s.type === 'bool' ? (
              <input type="checkbox" checked={values[k] === true} onChange={(e) => setValues({ ...values, [k]: e.target.checked })} />
            ) : s.type === 'longtext' ? (
              <textarea className={INPUT} rows={5} value={String(values[k] ?? '')} onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
            ) : s.type === 'number' ? (
              <input className={INPUT} type="number" min={0} value={String(values[k] ?? '')}
                onChange={(e) => setValues({ ...values, [k]: e.target.value === '' ? null : Number(e.target.value) })} />
            ) : (
              <input className={INPUT} type={s.type === 'color' ? 'color' : 'text'} value={String(values[k] ?? '')}
                onChange={(e) => setValues({ ...values, [k]: e.target.value })} />
            )}
          </label>
        ))}
      </div>
      <button type="button" className={BTN} disabled={busy || changed.length === 0}
        onClick={() => save(changed.map((k) => ({ op: 'set_setting', key: k, value: values[k], intent: `Set “${m.settings[k].label}”.` })))}>
        Save {changed.length || ''} setting{changed.length === 1 ? '' : 's'} to the draft
      </button>
    </div>
  )
}

function Maintainers({ workspace, app }: { workspace: string; app: InstalledApp }) {
  const [grants, setGrants] = useState<AppGrant[]>([])
  // App roles go only to members of the workspace: pick from them, never type an address.
  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const load = useCallback(() => { listGrants(workspace, app.key).then(setGrants, () => setGrants([])) }, [workspace, app.key])
  useEffect(load, [load])
  useEffect(() => { listMembers(workspace).then(setMembers, () => setMembers([])) }, [workspace])
  const roles = ['maintainer', ...Object.keys((app.manifest as { roles?: Record<string, unknown> } | null)?.roles ?? {})]
  const candidates: Candidate[] = (members ?? []).map((m) => ({ user_id: m.user_id, email: m.email, display_name: m.email }))
  return (
    <Section title="Who has which role in this app">
      <p className="mb-2 text-sm text-muted-foreground">
        A <b>maintainer</b> customizes the app (drafts, tests, publishes ordinary changes) without being a workspace admin — the
        head of HR for Hiring, or the agent you ask. The app's own roles come with workspace roles; give one here to someone who needs it.
      </p>
      <ul className="mb-3 divide-y divide-border text-sm">
        {grants.map((g) => (
          <li key={g.id} className="flex items-center justify-between py-1.5">
            <span>{g.email} — {g.role}</span>
            <button type="button" className="text-xs text-muted-foreground hover:text-destructive"
              onClick={() => removeGrant(workspace, app.key, g.id).then(load)}>Remove</button>
          </li>
        ))}
        {grants.length === 0 && <li className="py-1.5 text-muted-foreground">Nobody yet.</li>}
      </ul>
      <AddPeoplePicker candidates={candidates} loading={members === null} options={roleOptions(roles)} defaultRole="maintainer"
        onAdd={(email, role) => addGrant(workspace, app.key, email, role).then(load)} />
      <p className="mt-1 text-xs text-muted-foreground">Only members of this workspace are listed. Add someone to the workspace first to give them a role here.</p>
    </Section>
  )
}
