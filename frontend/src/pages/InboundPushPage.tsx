import { useCallback, useEffect, useMemo, useState, type FormEvent, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button, Input, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { listAgents, type AgentOut } from '@/api/agents'
import {
  createMailbox,
  deleteMailbox,
  getPushConfig,
  listMailboxes,
  setMailboxEnabled,
  setPushConfig,
  type MailboxOut,
  type PushConfigOut,
} from '@/api/inbound'
import {
  DEFAULT_PROJECT,
  DEFAULT_TOPIC,
  consoleLinks,
  setupCommands,
  suggestedServiceAccount,
  topicPath,
  watchLabel,
  watchTone,
} from './inboundSetup'

const TONE_CLASS: Record<string, string> = {
  success: 'text-success',
  warning: 'text-warning',
  destructive: 'text-destructive',
  muted: 'text-muted-foreground',
}

function CopyField({ label, value }: { label: string; value: string }): JSX.Element {
  const [copied, setCopied] = useState(false)
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="flex gap-2">
        <code className="flex-1 overflow-x-auto rounded border border-border bg-muted px-2 py-1 text-xs">
          {value}
        </code>
        <Button
          type="button"
          variant="outline"
          onClick={() => {
            void navigator.clipboard?.writeText(value)
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          }}
        >
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
    </div>
  )
}

export function InboundPushPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const { workspaces } = useWorkspace()
  const isOwner = workspaces.find((w) => w.slug === slug)?.role === 'owner'

  const [config, setConfig] = useState<PushConfigOut | null>(null)
  const [mailboxes, setMailboxes] = useState<MailboxOut[] | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Provisioning inputs. Local-only — they exist to GENERATE the commands, not
  // to be stored: canopy-web never talks to GCP, so it has no business keeping a
  // project id it cannot verify.
  const [project, setProject] = useState(DEFAULT_PROJECT)
  const [topic, setTopic] = useState(DEFAULT_TOPIC)

  const [audience, setAudience] = useState('')
  const [serviceAccount, setServiceAccount] = useState('')
  const [watchTopic, setWatchTopic] = useState('')

  const [newAddress, setNewAddress] = useState('')
  const [newAgent, setNewAgent] = useState('')

  const load = useCallback(async () => {
    if (!slug) return
    try {
      const [cfg, boxes, agentPage] = await Promise.all([
        getPushConfig(slug),
        listMailboxes(slug),
        listAgents().catch(() => ({ items: [] as AgentOut[] })),
      ])
      setConfig(cfg)
      setMailboxes(boxes)
      // listAgents is scoped to the caller's DEFAULT workspace, not the one in
      // the URL, so filter to this tenant — offering an agent from another
      // workspace would just 422 on submit.
      setAgents((agentPage.items ?? []).filter((a) => a.workspace === slug))
      setAudience(cfg.audience || cfg.push_url)
      setServiceAccount(cfg.service_account)
      setWatchTopic(cfg.watch_topic)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load')
    }
  }, [slug])

  useEffect(() => {
    void load()
  }, [load])

  const commands = useMemo(
    () =>
      setupCommands({
        pushUrl: config?.push_url ?? '',
        project,
        topic,
        serviceAccount: serviceAccount || suggestedServiceAccount(project),
      }),
    [config?.push_url, project, topic, serviceAccount],
  )

  async function saveConfig(e: FormEvent) {
    e.preventDefault()
    if (!slug) return
    setSaving(true)
    try {
      setConfig(await setPushConfig(slug, {
        audience: audience.trim(),
        service_account: serviceAccount.trim(),
        watch_topic: watchTopic.trim(),
      }))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  async function addMailbox(e: FormEvent) {
    e.preventDefault()
    if (!slug || !newAddress.trim() || !newAgent) return
    try {
      await createMailbox(slug, { address: newAddress.trim(), agent_slug: newAgent })
      setNewAddress('')
      await load()
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add mailbox')
    }
  }

  if (!slug) return null

  return (
    <div className="space-y-8 p-6">
      <WorkbenchSubHeader title="Inbound email push" />
      <p className="-mt-6 text-sm text-foreground-secondary">
        Deliver mail to an agent in seconds instead of waiting out the 5-minute poll.
      </p>

      {error && (
        <div className="rounded border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {config && !config.verifies && (
        <div className="rounded border border-warning/30 bg-warning/10 px-3 py-2 text-sm text-warning">
          No audience configured — this workspace <strong>refuses every push</strong>. That is the
          safe default, not a bug: until it is set, mail is still found by the 5-minute poll.
        </div>
      )}

      {/* ── 1. what to paste into GCP ────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">1. Provision Google Cloud</h2>
        <p className="text-sm text-foreground-secondary">
          The push endpoint is generated for this workspace — copy it rather than typing it, since a
          wrong URL fails silently (pushes simply go nowhere).
        </p>
        <CopyField label="Push endpoint (this workspace)" value={config?.push_url ?? ''} />
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">GCP project</span>
            <Input value={project} onChange={(e) => setProject(e.target.value)} />
          </label>
          <label className="space-y-1 text-sm">
            <span className="text-muted-foreground">Topic name</span>
            <Input value={topic} onChange={(e) => setTopic(e.target.value)} />
          </label>
        </div>
        <CopyField label="Run these" value={commands} />
        <div className="flex flex-wrap gap-3 text-xs">
          {consoleLinks(project).map((l) => (
            <a key={l.url} className="text-primary underline" href={l.url} target="_blank" rel="noreferrer">
              {l.label}
            </a>
          ))}
        </div>
      </section>

      {/* ── 2. tell canopy-web what to trust ─────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">2. What this workspace trusts</h2>
        <p className="text-sm text-foreground-secondary">
          Verification is per workspace, so another tenant&rsquo;s subscription can never satisfy
          this one&rsquo;s check. Pin the service account too — audience alone is not identity,
          since anyone who learns the audience string could mint a token for it.
        </p>
        <form className="space-y-3" onSubmit={saveConfig}>
          <label className="block space-y-1 text-sm">
            <span className="text-muted-foreground">Audience (must equal the push endpoint)</span>
            <Input value={audience} onChange={(e) => setAudience(e.target.value)} disabled={!isOwner} />
          </label>
          <label className="block space-y-1 text-sm">
            <span className="text-muted-foreground">Push service account</span>
            <Input
              value={serviceAccount}
              placeholder={suggestedServiceAccount(project)}
              onChange={(e) => setServiceAccount(e.target.value)}
              disabled={!isOwner}
            />
          </label>
          <label className="block space-y-1 text-sm">
            <span className="text-muted-foreground">
              Watch topic (served to runners — no runner.json editing)
            </span>
            <Input
              value={watchTopic}
              placeholder={topicPath(project, topic)}
              onChange={(e) => setWatchTopic(e.target.value)}
              disabled={!isOwner}
            />
          </label>
          {isOwner ? (
            <Button type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
          ) : (
            <p className="text-xs text-muted-foreground">Only a workspace owner can change this.</p>
          )}
        </form>
      </section>

      {/* ── 3. the mailboxes ─────────────────────────────────────────────── */}
      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-foreground">3. Mailboxes</h2>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Address</TableHead>
              <TableHead>Agent</TableHead>
              <TableHead>Watch</TableHead>
              <TableHead>Last push</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {(mailboxes ?? []).map((mb) => (
              <TableRow key={mb.id} className={mb.enabled ? '' : 'opacity-50'}>
                <TableCell className="font-mono text-xs">{mb.address}</TableCell>
                <TableCell>{mb.agent_slug}</TableCell>
                <TableCell className={TONE_CLASS[watchTone(mb.watch_state)]}>
                  {watchLabel(mb.watch_state, mb.watch_expires_at)}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  {mb.last_push_at ? new Date(mb.last_push_at).toLocaleString() : 'never'}
                </TableCell>
                <TableCell className="space-x-2 text-right">
                  {isOwner && (
                    <>
                      <Button
                        variant="outline"
                        onClick={async () => {
                          await setMailboxEnabled(slug, mb.id, !mb.enabled).catch((e) =>
                            setError(e instanceof Error ? e.message : 'Failed'))
                          void load()
                        }}
                      >
                        {mb.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={async () => {
                          if (!window.confirm(`Remove ${mb.address}?`)) return
                          await deleteMailbox(slug, mb.id).catch((e) =>
                            setError(e instanceof Error ? e.message : 'Failed'))
                          void load()
                        }}
                      >
                        Remove
                      </Button>
                    </>
                  )}
                </TableCell>
              </TableRow>
            ))}
            {mailboxes?.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-muted-foreground">
                  No mailboxes registered — push has nothing to deliver to.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>

        {isOwner && (
          <form className="flex flex-wrap items-end gap-2" onSubmit={addMailbox}>
            <label className="space-y-1 text-sm">
              <span className="text-muted-foreground">Address</span>
              <Input
                value={newAddress}
                placeholder="eva@dimagi-ai.com"
                onChange={(e) => setNewAddress(e.target.value)}
              />
            </label>
            <label className="space-y-1 text-sm">
              <span className="text-muted-foreground">Agent</span>
              <select
                className="h-9 rounded border border-input bg-input px-2 text-sm text-foreground"
                value={newAgent}
                onChange={(e) => setNewAgent(e.target.value)}
              >
                <option value="">Choose…</option>
                {agents.map((a) => (
                  <option key={a.slug} value={a.slug}>
                    {a.slug}
                  </option>
                ))}
              </select>
            </label>
            <Button type="submit" disabled={!newAddress.trim() || !newAgent}>
              Add
            </Button>
          </form>
        )}
      </section>

      {/* ── 4. arming ────────────────────────────────────────────────────── */}
      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-foreground">4. Arming</h2>
        <p className="text-sm text-foreground-secondary">
          A Gmail watch expires within 7 days and Google will not renew it, so a runner re-arms each
          mailbox 24h before expiry using the credentials it already holds, and reports the result
          here. Nothing to do by hand — but if the Watch column shows{' '}
          <span className="text-destructive">EXPIRED</span>, push is <em>not</em> being delivered and
          mail is falling back to the 5-minute poll.
        </p>
      </section>
    </div>
  )
}

export default InboundPushPage
