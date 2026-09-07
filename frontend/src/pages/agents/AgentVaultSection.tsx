import { useEffect, useState } from 'react'
import { getAgentVault, setAgentVault } from '@/api/agents'

// The 1Password half of the credentials screen.
//
// canopy-web CUSTODIES this pair; it does not use it. Jonathan, 2026-09-06: "the
// service account and vault should be used on the runner… canopy-web should just
// store what it needs or to send to the runner." An earlier version had this page
// import all 45 secrets into canopy-web, which makes it a second copy of every
// credential — the thing it was told twice not to become. The runner already has
// 1Password access; what it lacked was WHICH vault per agent (it derived
// Agent-<Slug> in bash) and a key scoped to it.
//
// The key is PER AGENT rather than fleet-wide: one key that reads every vault
// makes canopy-web worth attacking for every agent's secrets at once, where this
// bounds a compromise to the one agent whose key was taken.

export function AgentVaultSection({ slug }: { slug: string }) {
  const [vault, setVault] = useState('')
  const [keySet, setKeySet] = useState(false)
  const [declared, setDeclared] = useState(0)
  const [locatable, setLocatable] = useState(0)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let off = false
    getAgentVault(slug)
      .then((v) => {
        if (off) return
        setVault(v.vault ?? '')
        setKeySet(Boolean(v.key_set))
        setDeclared(v.declared ?? 0)
        setLocatable(v.locatable ?? 0)
      })
      .catch(() => {})
    return () => {
      off = true
    }
  }, [slug])

  const save = async () => {
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      // A blank key is OMITTED, not sent as "". Sending it would turn a rename
      // into a de-provisioning.
      const v = await setAgentVault(slug, {
        vault,
        ...(key.trim() ? { service_key: key.trim() } : {}),
      })
      setKeySet(Boolean(v.key_set))
      setDeclared(v.declared ?? 0)
      setLocatable(v.locatable ?? 0)
      setKey('')
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mb-5" data-testid="agent-vault">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        1Password
      </h3>
      <div className="rounded-lg border border-border bg-card px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-[12px] text-muted-foreground" htmlFor="vault-name">
            Vault
          </label>
          <input
            id="vault-name"
            value={vault}
            onChange={(e) => setVault(e.target.value)}
            placeholder="Agent-Ace"
            className="w-44 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground"
          />
          <label className="text-[12px] text-muted-foreground" htmlFor="vault-key">
            Service key
          </label>
          <input
            id="vault-key"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={keySet ? 'set — paste to rotate' : 'paste to set'}
            className="w-52 rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground"
          />
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy}
            className="ml-auto rounded-md bg-primary px-2 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
          >
            Save
          </button>
        </div>

        <p className="mt-2 text-[11px] text-foreground-subtle">
          A service account scoped to this one vault. <strong>The runner uses it</strong> — canopy-web
          holds it and hands it to a runner this agent routes to, and never resolves secrets itself.
          Encrypted at rest, never returned to a browser.
        </p>

        {declared > 0 && (
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="locatable-note">
            {locatable === 0
              ? `No source map for ${declared} refs — canopy-web cannot say where this agent's secrets live.`
              : `${locatable} of ${declared} declared refs resolve from this vault on the runner.`}
          </p>
        )}

        {saved && !error && (
          <p className="mt-2 text-[12px] text-success">
            Saved. A runner picks this up on its next bootstrap.
          </p>
        )}
        {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
      </div>
    </section>
  )
}
