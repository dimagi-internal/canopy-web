import { useEffect, useState } from 'react'
import { listTokens, mintToken, revokeToken, type PersonalToken } from '@/api/tokens'
import { tokenStatus } from '@/api/tokenStatus'
import { CopyBlock } from '@/components/CopyBlock'
import { Button } from 'canopy-ui/ui'
import { Input } from 'canopy-ui/ui'

/**
 * List / mint / revoke Personal Access Tokens.
 *
 * A PAT is how a machine caller authenticates (Authorization: Bearer <raw>) —
 * the canopy plugin, the MCP surface at /api/mcp/, and any script. It had no UI
 * at all, so the only way to get one was a skill that needs the plugin you are
 * trying to set up.
 */
export function TokensPanel() {
  const [tokens, setTokens] = useState<PersonalToken[]>([])
  const [label, setLabel] = useState('')
  const [minted, setMinted] = useState<string>('')
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function refresh() {
    try {
      setTokens(await listTokens())
    } catch {
      setError('Could not load tokens.')
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function onMint(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    const res = await mintToken(label.trim(), null)
    setBusy(false)
    if ('error' in res) {
      setError(res.error)
      return
    }
    setMinted(res.raw)
    setCopied(false)
    setLabel('')
    await refresh()
  }

  async function onRevoke(t: PersonalToken) {
    if (!window.confirm(`Revoke "${t.label}"? Anything using it stops working immediately.`)) return
    if (await revokeToken(t.id)) await refresh()
    else setError('Could not revoke that token.')
  }

  async function copyMinted() {
    try {
      await navigator.clipboard.writeText(minted)
      setCopied(true)
    } catch {
      // ignore
    }
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Personal access tokens</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          How a machine authenticates as you — the canopy plugin, the MCP endpoint, or your
          own scripts. Sent as <code>Authorization: Bearer &lt;token&gt;</code>.
        </p>
      </div>

      {minted && (
        <div className="space-y-3">
          <div className="rounded-lg border border-warning/20 bg-warning/5 p-3 text-xs text-warning/80">
            <strong className="text-warning">Copy this now — it is not shown again.</strong>{' '}
            The server stores only a hash, so this value cannot be retrieved later.
          </div>
          <CopyBlock label="Token" value={minted} copied={copied} onCopy={() => void copyMinted()} />
          <div className="text-right">
            <button
              type="button"
              onClick={() => setMinted('')}
              className="text-xs text-muted-foreground hover:text-foreground-secondary"
            >
              Done
            </button>
          </div>
        </div>
      )}

      <form onSubmit={onMint} className="flex flex-wrap items-end gap-2">
        <div className="flex-1 min-w-[200px]">
          <label htmlFor="pat-label" className="block text-xs text-muted-foreground mb-1">
            What is it for?
          </label>
          <Input
            id="pat-label"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="my laptop's canopy plugin"
            required
          />
        </div>
        <Button type="submit" size="sm" disabled={busy || !label.trim()}>
          {busy ? 'Minting…' : 'Mint token'}
        </Button>
      </form>
      {error && <p className="text-sm text-destructive">{error}</p>}

      {tokens.length === 0 ? (
        <p className="text-sm text-muted-foreground">No tokens yet.</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">Label</th>
              <th className="py-1 font-medium">Created</th>
              <th className="py-1 font-medium">Last used</th>
              <th className="py-1 font-medium">Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tokens.map((t) => (
              <tr key={t.id} className="border-t border-border">
                <td className="py-1.5 text-foreground-secondary">{t.label}</td>
                <td className="py-1.5 text-muted-foreground">{t.created_at.slice(0, 10)}</td>
                <td className="py-1.5 text-muted-foreground">
                  {t.last_used_at ? t.last_used_at.slice(0, 10) : 'never'}
                </td>
                <td className="py-1.5 text-muted-foreground">{tokenStatus(t)}</td>
                <td className="py-1.5 text-right">
                  {t.revoked_at ? null : (
                    <button
                      type="button"
                      onClick={() => void onRevoke(t)}
                      className="text-destructive hover:underline"
                    >
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
