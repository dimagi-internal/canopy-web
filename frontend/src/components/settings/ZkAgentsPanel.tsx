import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'
import { getAgentCredentialStatus, listAgents } from '@/api/agents'
import { getZkState, type ZkStateOut } from '@/api/vault'
import {
  ensureRecoveryCode,
  grantPending,
  prepareTurnOnZk,
  vaultKeyForFlow,
  type PreparedZk,
  type Unlocked,
} from '@/vault/operations'
import { PassphraseSetup } from '@/vault/PassphraseSetup'
import { browserSupportsPrf, PrfUnsupportedError, USE_A_PASSPHRASE } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

/**
 * "Your agents" on the Security card (canopy-web#1550). Zero-knowledge is the
 * canopy-web vault's only mode, so there is no switch here — each agent you
 * administer shows where it stands, and ONE "Finish setup" does whatever is
 * left for all of them with one unlock: creates your key if you have none,
 * gives you your one recovery code if you have none, finishes agents still
 * waiting for a key, and lets waiting machines in.
 */

interface AgentRow {
  slug: string
  name: string
  stored: string[]
  zk: ZkStateOut
}

type AgentStatus = 'ready' | 'finish' | 'waiting'

function statusOf(r: AgentRow): AgentStatus {
  if (!r.zk.enabled || r.zk.kv === 0) return 'finish'
  if (r.zk.pending.length || (r.zk.sealed ?? []).length) return 'waiting'
  return 'ready'
}

function statusText(r: AgentRow): string {
  const s = statusOf(r)
  if (s === 'ready') return 'Locked ✓'
  if (s === 'finish') return 'Not locked yet'
  const machines = r.zk.pending.filter((p) => p.kind === 'device').map((p) => p.runner_name || 'a machine')
  const people = r.zk.pending.filter((p) => p.kind === 'member').map((p) => p.email || 'someone')
  const who = [...machines, ...people]
  return who.length ? `${who.join(', ')} waiting` : 'New secrets to confirm'
}

function names(rows: AgentRow[]): string {
  const n = rows.map((r) => r.name)
  return n.length <= 2 ? n.join(' and ') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
}

/** What the one button does, in words: lock new agents, let waiting machines in, or both. */
function lockText(todo: AgentRow[]): string {
  const lock = todo.filter((r) => statusOf(r) === 'finish')
  const admit = todo.filter((r) => statusOf(r) === 'waiting')
  const parts: string[] = []
  if (lock.length) {
    parts.push(`Encrypts ${names(lock)}’s secrets with your key, so only you and the machines you approve can read them — canopy-web keeps only a locked copy.`)
  }
  if (admit.length) parts.push(`Gives the machines waiting on ${names(admit)} their copy of the key.`)
  return parts.join(' ')
}

function buttonText(todo: AgentRow[]): string {
  const lock = todo.filter((r) => statusOf(r) === 'finish')
  if (lock.length) return `Lock ${names(lock)}’s secrets`
  return `Let waiting machines in`
}

async function findAgents(): Promise<AgentRow[]> {
  // Only agents whose secrets live in this vault: a 1Password agent has nothing
  // here to finish, even when you administer it (or administer everything).
  const agents = (await listAgents({ limit: 200 })).items.filter((a) => a.credential_source === 'canopy-web')
  const rows = await Promise.all(agents.map(async (a): Promise<AgentRow | null> => {
    try {
      const zk = await getZkState(a.slug) // refuses unless you administer the agent
      const stored = (await getAgentCredentialStatus(a.slug)).filter((r) => r.set).map((r) => r.name)
      // Nothing stored and nobody waiting: nothing to show.
      if (!stored.length && !zk.pending.length && !(zk.enabled && zk.kv > 0)) return null
      return { slug: a.slug, name: a.name || a.slug, stored, zk }
    } catch {
      return null
    }
  }))
  return rows.filter((r): r is AgentRow => r !== null)
}

const STATUS_TONE: Record<AgentStatus, string> = {
  ready: 'text-success',
  finish: 'text-warning',
  waiting: 'text-warning',
}

export function ZkAgentsPanel({ onChanged }: { onChanged?: () => void } = {}) {
  const { prompt, element } = useUnlockPrompt()
  const [rows, setRows] = useState<AgentRow[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [askPassphrase, setAskPassphrase] = useState(false)
  /** A recovery code on screen, and what to do once it is kept. */
  const [code, setCode] = useState<{ code: string; slug?: string; resolve: () => void; reject: (e: Error) => void } | null>(null)
  const [saved, setSaved] = useState(false)

  const load = () => findAgents().then(setRows).catch(() => setRows([]))
  useEffect(() => {
    void load()
  }, [])

  if (rows === null) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground" data-testid="zk-agents-none">No agents keep secrets here yet.</p>
  }

  const todo = rows.filter((r) => statusOf(r) !== 'ready')

  /** Show a code ONCE; resolves when the person says they kept it. */
  const showCode = (slug?: string) => (c: string) =>
    new Promise<void>((resolve, reject) => {
      setSaved(false)
      setCode({ code: c, slug, resolve, reject })
    })

  const finishWith = async (me: Unlocked) => {
    await ensureRecoveryCode(me, showCode())
    const ready: PreparedZk[] = []
    for (const r of todo.filter((x) => statusOf(x) === 'finish')) {
      ready.push(await prepareTurnOnZk(r.slug, r.stored, async () => me))
    }
    for (const p of ready) {
      if (p.code) await showCode(p.slug)(p.code) // only someone without their own recovery code
      await p.commit()
    }
    for (const r of todo) {
      // Finished agents and already-ready ones: let waiting machines and people in.
      await grantPending(r.slug, async () => me)
    }
    await load()
    onChanged?.() // the You row re-reads: a recovery code may have just been made
  }

  const start = async () => {
    setBusy(true)
    setError('')
    try {
      const got = await vaultKeyForFlow(await browserSupportsPrf(), prompt)
      if (!got) setAskPassphrase(true)
      else await finishWith(got.me)
    } catch (e) {
      setError(e instanceof PrfUnsupportedError ? USE_A_PASSPHRASE
        : e instanceof Error && e.name === 'NotAllowedError' ? 'The passkey prompt was dismissed.'
          : e instanceof Error ? e.message : 'Could not finish setup.')
    } finally {
      setBusy(false)
    }
  }

  const download = () => {
    if (!code) return
    const text = code.slug
      ? `Canopy recovery code\nAgent: ${code.slug}\n\n${code.code}\n\nKeep it offline.\n`
      : `Canopy recovery code\n\n${code.code}\n\nIf you lose your passkey and passphrase, this gets you back into every agent you hold. Keep it offline.\n`
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = code.slug ? `canopy-recovery-code-${code.slug}.txt` : 'canopy-recovery-code.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-3" data-testid="settings-zk-agents">
      <ul className="divide-y divide-border text-sm">
        {rows.map((r) => (
          <li key={r.slug} className="flex items-center justify-between gap-2 py-1.5" data-testid={`zk-agent-${r.slug}`}>
            <a href={`/agents/${r.slug}/settings#cred-zk`} className="text-foreground hover:underline">{r.name}</a>
            <span className={`text-xs ${STATUS_TONE[statusOf(r)]}`} data-testid={`zk-agent-status-${r.slug}`}>
              {statusText(r)}
            </span>
          </li>
        ))}
      </ul>

      {code ? (
        <div role="dialog" aria-label="Recovery code" className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
             data-testid="zk-agents-code">
          <p className="font-medium text-foreground">
            {code.slug ? `Recovery code for ${code.slug} — shown once` : 'Your recovery code — shown once'}
          </p>
          <p className="text-muted-foreground">
            {code.slug
              ? 'It opens this agent’s secrets if everyone loses access.'
              : 'If you lose your passkey and passphrase, this gets you back into every agent you hold. canopy-web does not keep a copy — save it in your password manager.'}
          </p>
          <code className="block select-all break-all font-mono text-foreground" data-testid="zk-agents-code-value">{code.code}</code>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={download}>Download</Button>
            <label className="flex items-center gap-1 text-foreground">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="zk-agents-saved" />
              I have saved it
            </label>
            <Button size="sm" disabled={!saved} data-testid="zk-agents-commit"
                    onClick={() => {
                      const c = code
                      setCode(null)
                      c.resolve()
                    }}>
              Continue
            </Button>
            <Button size="sm" variant="ghost"
                    onClick={() => {
                      const c = code
                      setCode(null)
                      c.reject(new Error('Cancelled — nothing was changed.'))
                    }}>
              Cancel
            </Button>
          </div>
        </div>
      ) : todo.length > 0 && (askPassphrase ? (
        <PassphraseSetup note={USE_A_PASSPHRASE} submitLabel={buttonText(todo)}
                         onCancel={() => setAskPassphrase(false)}
                         onReady={(me) => {
                           setAskPassphrase(false)
                           setBusy(true)
                           finishWith(me)
                             .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not finish setup.'))
                             .finally(() => setBusy(false))
                         }} />
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground" data-testid="zk-agents-explain">
            {lockText(todo)} One tap of your passkey; the first time, you also get a recovery code to keep.
          </p>
          <Button size="sm" disabled={busy} onClick={() => void start()} data-testid="zk-agents-start">
            {busy ? 'Working…' : buttonText(todo)}
          </Button>
        </div>
      ))}
      {error && <p className="text-sm text-destructive" data-testid="zk-agents-error">{error}</p>}
      {element}
    </div>
  )
}
