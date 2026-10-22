import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { listMcpServers, type McpServerOut } from '@/api/agents'
import { relativeAge } from '@/lib/relativeAge'
import { CopyCommand } from '@/components/agents/CopyCommand'

// THE MCP DIRECTORY — every server a workspace project serves (a knowledge base
// today), in this workspace and the ones below it the reader belongs to. A list to
// browse and choose from: nothing here is added to anyone's sessions. Each row is
// one its reader can call, and carries the line that adds it.

function size(bytes: number | null | undefined): string {
  if (!bytes) return ''
  const mb = bytes / 1e6
  return mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.max(mb, 0.1).toFixed(1)} MB`
}

export function McpServersPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [servers, setServers] = useState<McpServerOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setServers(null)
    listMcpServers(workspace)
      .then((rows) => !cancelled && setServers(rows))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load MCP servers'))
    return () => {
      cancelled = true
    }
  }, [workspace])

  return (
    <div className="max-w-4xl">
      <h1 className="text-xl font-semibold text-foreground">MCP servers</h1>
      <p className="mt-1 text-[13px] text-muted-foreground">
        Tools the projects in this workspace, and the workspaces below it you belong to, serve on their own. Add the
        ones you want; nothing is added for you. You sign in with your canopy account the first time.
      </p>

      {error && <p className="mt-4 text-sm text-destructive">{error}</p>}
      {!servers && !error && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
      {servers && servers.length === 0 && (
        <p className="mt-4 rounded border border-border bg-card p-4 text-[13px] text-foreground-secondary">
          No project here serves an MCP server yet. A knowledge base gets one when its steward publishes it.
        </p>
      )}

      {servers && servers.length > 0 && (
        <ul className="mt-4 divide-y divide-border rounded-lg border border-border" data-testid="mcp-servers">
          {servers.map((s) => (
            <li key={s.address} className="p-4">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <Link to={s.project_url} className="font-medium text-foreground hover:text-primary">
                  {s.title}
                </Link>
                <span className="font-mono text-[12px] text-muted-foreground">{s.address}</span>
                {s.workspace !== workspace && (
                  <span className="rounded bg-muted px-1 py-px text-[10px] text-muted-foreground">{s.workspace_name}</span>
                )}
                <span className="rounded bg-muted px-1 py-px text-[10px] text-muted-foreground">
                  {s.kind === 'kb' ? 'knowledge base' : s.kind}
                </span>
              </div>
              {s.description && <p className="mt-1 text-[13px] text-foreground-secondary">{s.description}</p>}
              <p className="mt-1 text-[12px] text-muted-foreground">
                Kept by {s.steward_slug}
                {s.published_at && <> · published {relativeAge(s.published_at, new Date(), { seconds: false })}</>}
                {s.bytes ? <> · {size(s.bytes)}</> : null}
              </p>
              <button
                type="button"
                onClick={() => setOpen(open === s.address ? null : s.address)}
                className="mt-2 text-[12px] text-primary hover:underline"
                aria-expanded={open === s.address}
              >
                {open === s.address ? 'Hide' : 'Add it'}
              </button>
              {open === s.address && (
                <div className="mt-2">
                  <p className="text-[12px] text-foreground">Claude Code</p>
                  <CopyCommand command={s.add_command} testId={`mcp-add-${s.name}`} />
                  <p className="mt-3 text-[12px] text-foreground">Claude (web or desktop): Settings → Connectors → Add custom connector</p>
                  <CopyCommand command={s.url} />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
