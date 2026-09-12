/**
 * Which first-run state applies. Extracted as a pure function because this
 * project has no jsdom/testing-library — logic in a .ts module is the only
 * thing unit-testable here (same reason as workspace/resolveActiveWorkspace.ts).
 *
 * There are THREE zero-workspace states, not one. An invite-admitted user who
 * holds no membership may not create a workspace (the F1 finding — see
 * apps/workspaces/services.py::can_create_workspace), so offering them a button
 * would 403. `canCreate: null` means /api/me/ has not answered yet.
 */
export interface FirstRunInput {
  loading: boolean
  workspaceCount: number
  canCreate: boolean | null
}

export type FirstRunState = 'loading' | 'can-create' | 'needs-invite' | 'ready'

export function firstRunState({ loading, workspaceCount, canCreate }: FirstRunInput): FirstRunState {
  if (workspaceCount > 0) return 'ready'
  if (loading || canCreate === null) return 'loading'
  return canCreate ? 'can-create' : 'needs-invite'
}
