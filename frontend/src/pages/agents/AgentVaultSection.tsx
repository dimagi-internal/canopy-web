import { useEffect, useState } from 'react'
import { getAgentVault, importAgentCredentials, setAgentVault } from '@/api/agents'

// The 1Password half of the credentials screen.
//
// Jonathan, 2026-09-06: "we should have 45 stored secrets for now… and then we
// can offer a 1pass vault and a service key." Nobody is pasting 45 secrets by
// hand, so getting to 45 stored has to be an import — and for canopy-web to
// import, it needs vault access of its own. That is what these two fields are.
//
// The key is PER AGENT, not fleet-wide. One key that reads every vault is
// simpler to operate and makes canopy-web worth attacking for every agent's
// secrets at once; this bounds a compromise to the one agent whose key was taken.

interface Result {
  imported: string[]
  skipped: { name?: string; reason?: string }[]
  failures: { name?: string; ref?: string; error?: string }[]
}

export function AgentVaultSection({ slug, onImported }: { slug: string; onImported: () => void }) {
  const [vault, setVault] = useState('')
  const [keySet, setKeySet] = useState(false)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<Result | null>(null)

  useEffect(() => {
    let off = false
    getAgentVault(slug)
      .then((v) => {
        if (off) return
        setVault(v.vault ?? '')
        setKeySet(Boolean(v.key_set))
      })
      .catch(() => {})
    return () => {
      off = true
    }
  }, [slug])

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      // A blank key is OMITTED, not sent as "". Sending it would turn a rename
      // into a de-provisioning.
      const v = await setAgentVault(slug, {
        vault,
        ...(key.trim() ? { service_key: key.trim() } : {}),
      })
      setKeySet(Boolean(v.key_set))
      setKey('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  const runImport = async () => {
    setBusy(true)
    setError(null)
    setResult(null)
    try {
      setResult((await importAgentCredentials(slug)) as Result)
      onImported()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Import failed')
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
            className="rounded-md border border-border px-2 py-1 text-[12px] text-foreground disabled:opacity-40"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => void runImport()}
            disabled={busy || !keySet}
            title={keySet ? '' : 'Set a service key first'}
            className="ml-auto rounded-md bg-primary px-2 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
            data-testid="import-from-1password"
          >
            Import secrets from 1Password
          </button>
        </div>

        <p className="mt-2 text-[11px] text-foreground-subtle">
          A service account scoped to this one vault. canopy-web reads it only to import; the key is
          encrypted at rest and never returned to a browser.
        </p>

        {result && (
          // Failures are shown as prominently as successes on purpose: a ref
          // that no longer resolves is the single most useful thing this screen
          // can tell anyone, and it is exactly what a silent partial import hides.
          <div className="mt-3 space-y-1 text-[12px]" data-testid="import-result">
            <p className="text-success">Imported {result.imported.length}.</p>
            {result.failures.length > 0 && (
              <div className="text-destructive">
                <p>{result.failures.length} could not be read:</p>
                <ul className="ml-4 list-disc">
                  {result.failures.map((f) => (
                    <li key={f.name}>
                      <code className="font-mono text-[11px]">{f.name}</code> — {f.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {result.skipped.length > 0 && (
              <p className="text-muted-foreground">
                {result.skipped.length} skipped ({result.skipped.map((s) => s.name).join(', ')}).
              </p>
            )}
          </div>
        )}

        {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
      </div>
    </section>
  )
}
