import { useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'
import { getAgentCredentialStatus, listAgents } from '@/api/agents'
import { getZkState } from '@/api/vault'
import { prepareTurnOnZk, vaultKeyForFlow, type PreparedZk, type Unlocked } from '@/vault/operations'
import { PassphraseSetup } from '@/vault/PassphraseSetup'
import { browserSupportsPrf, PrfUnsupportedError, USE_A_PASSPHRASE } from '@/vault/prf'
import { useUnlockPrompt } from '@/vault/UnlockPrompt'

/**
 * Turn zero-knowledge on for several agents at once (canopy-web#1550): every
 * agent you administer that stores secrets on canopy-web with zero-knowledge
 * still off. One unlock — one passkey touch when it can hold your vault key —
 * prepares them all; their recovery codes are shown together, ONCE, and only
 * after you say you kept them is anything enabled. Hidden when there is none.
 */

interface Candidate {
  slug: string
  name: string
  stored: string[]
}

async function findCandidates(): Promise<Candidate[]> {
  const agents = (await listAgents({ limit: 200 })).items
  const found = await Promise.all(agents.map(async (a): Promise<Candidate | null> => {
    try {
      const zk = await getZkState(a.slug) // refuses unless you administer the agent
      if (zk.enabled) return null
      const stored = (await getAgentCredentialStatus(a.slug)).filter((r) => r.set).map((r) => r.name)
      return stored.length ? { slug: a.slug, name: a.name || a.slug, stored } : null
    } catch {
      return null
    }
  }))
  return found.filter((c): c is Candidate => c !== null)
}

function codesText(done: PreparedZk[]): string {
  return 'Canopy vault recovery codes\n\n' +
    done.map((p) => `Agent: ${p.slug} (vault key version ${p.kv})\n${p.code}\n`).join('\n') +
    '\nAnyone with one of these codes and access to its agent can read that agent\'s secrets. Keep them offline.\n'
}

export function ZkAgentsPanel() {
  const { prompt, element } = useUnlockPrompt()
  const [candidates, setCandidates] = useState<Candidate[] | null>(null)
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [askPassphrase, setAskPassphrase] = useState(false)
  const [prepared, setPrepared] = useState<PreparedZk[] | null>(null)
  const [saved, setSaved] = useState(false)
  const [doneSlugs, setDoneSlugs] = useState<string[]>([])

  useEffect(() => {
    let cancelled = false
    findCandidates()
      .then((c) => {
        if (cancelled) return
        setCandidates(c)
        setPicked(new Set(c.map((x) => x.slug)))
      })
      .catch(() => !cancelled && setCandidates([]))
    return () => {
      cancelled = true
    }
  }, [])

  if (!candidates || (candidates.length === 0 && doneSlugs.length === 0)) return null

  const prepareAll = async (me: Unlocked) => {
    const ready: PreparedZk[] = []
    for (const c of candidates.filter((x) => picked.has(x.slug))) {
      ready.push(await prepareTurnOnZk(c.slug, c.stored, async () => me))
    }
    setSaved(false)
    setPrepared(ready)
  }

  const start = async () => {
    setBusy(true)
    setError('')
    try {
      const got = await vaultKeyForFlow(await browserSupportsPrf(), prompt)
      if (!got) setAskPassphrase(true)
      else await prepareAll(got.me)
    } catch (e) {
      setError(e instanceof PrfUnsupportedError ? USE_A_PASSPHRASE
        : e instanceof Error && e.name === 'NotAllowedError' ? 'The passkey prompt was dismissed.'
          : e instanceof Error ? e.message : 'Could not turn zero-knowledge on.')
    } finally {
      setBusy(false)
    }
  }

  const commitAll = async () => {
    if (!prepared) return
    setBusy(true)
    setError('')
    const ok: string[] = []
    for (const p of prepared) {
      try {
        await p.commit()
        ok.push(p.slug)
      } catch (e) {
        setError(`${p.slug}: ${e instanceof Error ? e.message : 'could not turn zero-knowledge on'}`)
      }
    }
    setDoneSlugs((d) => [...d, ...ok])
    setCandidates((c) => (c ?? []).filter((x) => !ok.includes(x.slug)))
    setPrepared(null)
    setBusy(false)
  }

  const download = () => {
    if (!prepared) return
    const url = URL.createObjectURL(new Blob([codesText(prepared)], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'canopy-vault-recovery-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-zk-agents">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Zero-knowledge for your agents</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Encrypt these agents&rsquo; secrets in your browser so canopy-web keeps only ciphertext. One confirmation
          does them all; each gets a recovery code you keep.
        </p>
      </div>

      {doneSlugs.length > 0 && (
        <p className="text-sm text-success" data-testid="zk-agents-done">Zero-knowledge is on for {doneSlugs.join(', ')}.</p>
      )}

      {prepared ? (
        <div role="dialog" aria-label="Recovery codes" className="space-y-2 rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm"
             data-testid="zk-agents-codes">
          <p className="font-medium text-foreground">Recovery codes — shown once</p>
          <ul className="space-y-1">
            {prepared.map((p) => (
              <li key={p.slug}>
                <span className="text-foreground-secondary">{p.slug}: </span>
                <code className="select-all font-mono text-foreground">{p.code}</code>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={download}>Download</Button>
            <label className="flex items-center gap-1 text-foreground">
              <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} data-testid="zk-agents-saved" />
              I have saved them
            </label>
            <Button size="sm" disabled={!saved || busy} onClick={() => void commitAll()} data-testid="zk-agents-commit">
              {busy ? 'Working…' : 'Turn it on'}
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setPrepared(null)}>Cancel</Button>
          </div>
        </div>
      ) : candidates.length > 0 && (
        <>
          <ul className="space-y-1 text-sm">
            {candidates.map((c) => (
              <li key={c.slug}>
                <label className="flex items-center gap-2 text-foreground-secondary">
                  <input type="checkbox" checked={picked.has(c.slug)} data-testid={`zk-agents-pick-${c.slug}`}
                         onChange={(e) => {
                           const next = new Set(picked)
                           if (e.target.checked) next.add(c.slug)
                           else next.delete(c.slug)
                           setPicked(next)
                         }} />
                  {c.name} <span className="text-muted-foreground">· {c.stored.length} secret(s)</span>
                </label>
              </li>
            ))}
          </ul>
          {askPassphrase ? (
            <PassphraseSetup note={USE_A_PASSPHRASE} submitLabel="Turn on zero-knowledge"
                             onCancel={() => setAskPassphrase(false)}
                             onReady={(me) => {
                               setAskPassphrase(false)
                               setBusy(true)
                               prepareAll(me)
                                 .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not turn zero-knowledge on.'))
                                 .finally(() => setBusy(false))
                             }} />
          ) : (
            <Button size="sm" disabled={busy || picked.size === 0} onClick={() => void start()} data-testid="zk-agents-start">
              {busy ? 'Working…' : `Turn on zero-knowledge for ${picked.size === candidates.length ? 'all' : picked.size} agent${picked.size === 1 ? '' : 's'}`}
            </Button>
          )}
        </>
      )}
      {error && <p className="text-sm text-destructive" data-testid="zk-agents-error">{error}</p>}
      {element}
    </div>
  )
}
