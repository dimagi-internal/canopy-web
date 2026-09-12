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

/**
 * Whether to render the create FORM (as opposed to the needs-invite panel).
 *
 * `/new-workspace` passes `alwaysOfferForm` so a user who already belongs
 * somewhere still sees the form — but eligibility is still consulted, never
 * bypassed: an invite-admitted user holding no membership may not create a
 * workspace (the F1 finding, apps/workspaces/services.py::can_create_workspace),
 * and offering them a form would render a button that 403s.
 */
export function shouldOfferCreateForm({
  state,
  canCreate,
  alwaysOfferForm,
}: {
  state: FirstRunState
  canCreate: boolean
  alwaysOfferForm: boolean
}): boolean {
  return alwaysOfferForm ? canCreate : state === 'can-create'
}
