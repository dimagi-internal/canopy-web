import { useCallback, useEffect, useState } from 'react'

import { addAgentDefault, listAgentDefaults, removeAgentDefault, type AccessLineOut } from '@/api/agents'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { AccessLineForm } from './AccessLineForm'
import { DEFAULT_ROLES, roleWord } from './accessWords'

// DEFAULT ACCESS FOR AGENTS: lines every agent in this workspace and below inherits, unless
// that agent opts out (its Access setting). Workspace admins change them; a default that
// gives the whole agent (editor) is the workspace OWNER's call. The server decides both —
// `canManage` / `isOwner` only decide what is offered, and a refusal is shown in its words.
export function WorkspaceAgentDefaults({
  workspace,
  canManage,
  isOwner,
}: {
  workspace: string
  canManage: boolean
  isOwner: boolean
}) {
  const { workspaces } = useWorkspace()
  const [lines, setLines] = useState<AccessLineOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setLines(await listAgentDefaults(workspace))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not load the default access')
    }
  }, [workspace])

  useEffect(() => {
    void load()
  }, [load])

  const remove = async (l: AccessLineOut) => {
    setError(null)
    try {
      await removeAgentDefault(workspace, l.id)
      await load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not remove that')
    }
  }

  return (
    <div className="mb-8 rounded-lg border border-border bg-card p-5" data-testid="agent-defaults">
      <h2 className="mb-1 text-sm font-semibold text-foreground">Default access for agents</h2>
      <p className="mb-3 text-[12px] text-muted-foreground">
        These people get these roles on every agent in this workspace and below, unless an agent opts out. An
        agent can add its own on top.
      </p>

      {lines === null ? (
        error ? null : <div className="h-12 animate-pulse rounded-lg bg-muted" />
      ) : lines.length === 0 ? (
        <p className="m-0 mb-3 text-[12px] text-muted-foreground">No defaults yet.</p>
      ) : (
        <table className="mb-3 w-full text-[12px]">
          <tbody>
            {lines.map((l) => (
              <tr key={l.id} data-testid="agent-default" className="border-b border-border/50">
                <td className="py-1 pr-3 text-foreground">{l.who}</td>
                <td className="py-1 pr-3 text-foreground">{roleWord(l.role)}</td>
                <td className="py-1 text-muted-foreground">
                  {l.manual_only ? 'needs approval before anything goes out' : ''}
                </td>
                <td className="py-1 text-right">
                  {l.can_edit && (
                    <button
                      type="button"
                      onClick={() => void remove(l)}
                      aria-label={`Remove default for ${l.who}`}
                      className="rounded border border-input px-1.5 text-[11px] text-muted-foreground hover:border-destructive hover:text-destructive"
                    >
                      Remove
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {error && (
        <p role="alert" className="m-0 mb-2 text-[12px] text-destructive">
          {error}
        </p>
      )}

      {canManage && (
        <AccessLineForm
          roles={DEFAULT_ROLES}
          workspaces={workspaces.map((w) => [w.slug, w.display_name] as [string, string])}
          canGiveEditor={isOwner}
          submitLabel="Add default"
          onAdd={async (line) => {
            await addAgentDefault(workspace, line)
            setError(null)
            await load()
          }}
        />
      )}
      {canManage && !isOwner && (
        <p className="m-0 mt-2 text-[11px] text-muted-foreground">
          Only a workspace owner can give editor to everyone as a default.
        </p>
      )}
    </div>
  )
}
