import { useEffect, useState } from 'react'
import { disconnectAuthorizedApp, listAuthorizedApps, type AuthorizedApps } from '@/api/tokens'
import { CopyBlock } from '@/components/CopyBlock'
import { copyText } from 'canopy-ui/lib'
import { Td, Th, Tr } from '@/components/DataTable'

/**
 * Apps connected to canopy's MCP server by signing in, and how to connect one.
 *
 * Adding the MCP address to Claude (or any MCP client) opens canopy's sign-in
 * and consent page; approving it lists the app here. No token is copied by
 * hand — the app refreshes its own, and disconnecting stops it at once.
 */
export function AuthorizedAppsPanel() {
  const [data, setData] = useState<AuthorizedApps | null>(null)
  const [copied, setCopied] = useState('')
  const [error, setError] = useState('')

  async function refresh() {
    const res = await listAuthorizedApps()
    if (res) setData(res)
    else setError('Could not load authorized apps.')
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function copy(which: string, value: string) {
    if (await copyText(value)) setCopied(which)
  }

  async function onDisconnect(id: number, name: string) {
    if (!window.confirm(`Disconnect ${name}? It stops working immediately and must sign in again.`)) return
    if (await disconnectAuthorizedApp(id)) await refresh()
    else setError('Could not disconnect that app.')
  }

  const url = data?.mcp_url ?? ''
  const claudeCode = url ? `claude mcp add --transport http canopy ${url}` : ''

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4" data-testid="settings-authorized-apps">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Authorized apps (MCP)</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Add canopy to Claude or any MCP client with the address below. It opens a canopy
          sign-in in your browser; once you allow it, the app acts as you — no token to copy.
        </p>
      </div>

      {url && (
        <div className="space-y-3">
          <CopyBlock label="MCP server address" value={url} copied={copied === 'url'}
                     onCopy={() => void copy('url', url)} />
          <CopyBlock label="Claude Code" value={claudeCode} copied={copied === 'cc'}
                     onCopy={() => void copy('cc', claudeCode)} />
        </div>
      )}
      {error && <p className="text-sm text-destructive">{error}</p>}

      {data && data.apps.length === 0 ? (
        <p className="text-sm text-muted-foreground">No apps connected yet.</p>
      ) : data ? (
        <table className="w-full text-xs">
          <thead>
            <Tr head="plain">
              <Th>App</Th>
              <Th>Connected</Th>
              <Th>Last used</Th>
              <Th density="none" weight="inherit" />
            </Tr>
          </thead>
          <tbody>
            {data.apps.map((a) => (
              <Tr rule="top" key={a.id}>
                <Td density="cozy" tone="secondary">{a.client_name}</Td>
                <Td density="cozy" tone="muted">{a.connected_at.slice(0, 10)}</Td>
                <Td density="cozy" tone="muted">
                  {a.last_used_at ? a.last_used_at.slice(0, 10) : 'never'}
                </Td>
                <Td density="cozy" align="right">
                  <button type="button" onClick={() => void onDisconnect(a.id, a.client_name)}
                          className="text-destructive hover:underline">
                    Disconnect
                  </button>
                </Td>
              </Tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}
