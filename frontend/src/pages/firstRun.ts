/**
 * Which first-run state applies. Extracted as a pure function so it stays
 * unit-testable independent of the component tree (same reason as
 * workspace/resolveActiveWorkspace.ts).
 *
 * There are THREE zero-workspace states, not one. An invite-admitted user who
 * holds no membership may not create a workspace (the F1 finding — see
 * apps/workspaces/services.py::can_create_workspace), so offering them a
 * button would 403. `canCreate` is a plain boolean, not nullable: the caller
 * (FirstRunPage) reads it from `useAuth()`, which is resolved before any
 * route mounts (AuthProvider gates on `status === 'loading'` itself), so
 * there is no "auth not answered yet" state to represent here — only
 * `loading` from `useWorkspace()`, which genuinely resolves on its own timer.
 */
export interface FirstRunInput {
  loading: boolean
  workspaceCount: number
  canCreate: boolean
}

export type FirstRunState = 'loading' | 'can-create' | 'needs-invite' | 'ready'

export function firstRunState({ loading, workspaceCount, canCreate }: FirstRunInput): FirstRunState {
  if (loading) return 'loading'
  if (workspaceCount > 0) return 'ready'
  return canCreate ? 'can-create' : 'needs-invite'
}
