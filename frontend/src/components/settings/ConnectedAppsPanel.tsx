import { useEffect, useState } from 'react'
import { disconnectApp, listConnectedApps, type ConnectedApps } from '@/api/tokens'
import { CopyBlock } from '@/components/CopyBlock'

/**
 * Apps connected to canopy's MCP server by signing in, and how to connect one.
 *
 * Adding the MCP address to Claude (or any MCP client) opens canopy's sign-in
 * and consent page; approving it lists the app here. No token is copied by
 * hand — the app refreshes its own, and disconnecting stops it at once.
 */
export function ConnectedAppsPanel() {
  const [data, setData] = useState<ConnectedApps | null>(null)
  const [copied, setCopied] = useState('')
  const [error, setError] = useState('')

  async function refresh() {
    const res = await listConnectedApps()
    if (res) setData(res)
    else setError('Could not load connected apps.')
  }

  useEffect(() => {
    void refresh()
  }, [])

  async function copy(which: string, value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(which)
    } catch {
      // ignore
    }
  }

  async function onDisconnect(id: number, name: string) {
    if (!window.confirm(`Disconnect ${name}? It stops working immediately and must sign in again.`)) return
    if (await disconnectApp(id)) await refresh()
    else setError('Could not disconnect that app.')
  }

  const url = data?.mcp_url ?? ''
  const claudeCode = url ? `claude mcp add --transport http canopy ${url}` : ''

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4" data-testid="settings-connected-apps">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Connected apps (MCP)</h2>
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
            <tr className="text-left text-muted-foreground">
              <th className="py-1 font-medium">App</th>
              <th className="py-1 font-medium">Connected</th>
              <th className="py-1 font-medium">Last used</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.apps.map((a) => (
              <tr key={a.id} className="border-t border-border">
                <td className="py-1.5 text-foreground-secondary">{a.client_name}</td>
                <td className="py-1.5 text-muted-foreground">{a.connected_at.slice(0, 10)}</td>
                <td className="py-1.5 text-muted-foreground">
                  {a.last_used_at ? a.last_used_at.slice(0, 10) : 'never'}
                </td>
                <td className="py-1.5 text-right">
                  <button type="button" onClick={() => void onDisconnect(a.id, a.client_name)}
                          className="text-destructive hover:underline">
                    Disconnect
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}
