// Canopy Vault (canopy-web#1538 Phase 1a): runner device keys, the agent's
// device-binding switch, and the credential read log. Its own module so the
// pages that show these can be tested without dragging in ./agents or ./harness.
import { apiV2 } from './client.v2'
import type { components } from './generated'

type Schemas = components['schemas']
export type RunnerDeviceKeyOut = Schemas['RunnerDeviceKeyOut']
export type AgentCredentialReadOut = Schemas['AgentCredentialReadOut']

function unwrap<T>(res: { data?: T; error?: unknown }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    const detail = (res.error as { detail?: unknown } | undefined)?.detail
    throw new Error(typeof detail === 'string' ? detail : `${what} failed`)
  }
  return res.data
}

/** Keys on every runner the caller owns or administers. */
export async function listRunnerDeviceKeys(): Promise<RunnerDeviceKeyOut[]> {
  const res = await apiV2.GET('/api/runner-device-keys/', {})
  return Array.from(unwrap(res, 'listRunnerDeviceKeys')) as RunnerDeviceKeyOut[]
}

/** Keys on the runners this agent routes to or its owner owns. */
export async function getAgentDeviceKeys(slug: string): Promise<RunnerDeviceKeyOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/device-keys', { params: { path: { slug } } })
  return Array.from(unwrap(res, 'getAgentDeviceKeys')) as RunnerDeviceKeyOut[]
}

/** pending → active. Browser session + a fresh passkey step-up: a 403
 *  `step_up_required` is answered by `apiV2`'s step-up middleware
 *  (`auth/stepUp.ts`), which prompts for the passkey and retries this call —
 *  so both approve buttons (agent Credentials, account Settings) get it free. */
export async function approveRunnerDeviceKey(id: string): Promise<RunnerDeviceKeyOut> {
  const res = await apiV2.POST('/api/runner-device-keys/{key_id}/approve', {
    params: { path: { key_id: id } },
  })
  return unwrap(res, 'approveRunnerDeviceKey') as RunnerDeviceKeyOut
}

/** Revoke immediately. */
export async function revokeRunnerDeviceKey(id: string): Promise<RunnerDeviceKeyOut> {
  const res = await apiV2.DELETE('/api/runner-device-keys/{key_id}', {
    params: { path: { key_id: id } },
  })
  return unwrap(res, 'revokeRunnerDeviceKey') as RunnerDeviceKeyOut
}

export async function setDeviceBinding(slug: string, on: boolean): Promise<boolean> {
  const res = await apiV2.PATCH('/api/agents/{slug}/device-binding', {
    params: { path: { slug } },
    body: { require_device_binding: on },
  })
  return Boolean(unwrap(res, 'setDeviceBinding').require_device_binding)
}

/** One credential's read history, newest first — never the value. */
export async function getCredentialReads(slug: string, name: string): Promise<AgentCredentialReadOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/credentials/{name}/reads', {
    params: { path: { slug, name } },
  })
  return Array.from(unwrap(res, 'getCredentialReads')) as AgentCredentialReadOut[]
}

// ---- Canopy Vault Phase 1b (canopy-web#1549): read alerts + canopy-pat rotation ----

export type AgentCredentialAlertOut = Schemas['AgentCredentialAlertOut']
export type PatRotationOut = Schemas['PatRotationOut']

/** Read-anomaly alerts on this agent's secrets, newest first. */
export async function getCredentialAlerts(slug: string): Promise<AgentCredentialAlertOut[]> {
  const res = await apiV2.GET('/api/agents/{slug}/credential-alerts', { params: { path: { slug } } })
  return Array.from(unwrap(res, 'getCredentialAlerts')) as AgentCredentialAlertOut[]
}

export async function getPatRotation(slug: string): Promise<PatRotationOut> {
  const res = await apiV2.GET('/api/agents/{slug}/pat-rotation', { params: { path: { slug } } })
  return unwrap(res, 'getPatRotation') as PatRotationOut
}

export async function setPatRotation(slug: string, enabled: boolean, periodDays: number): Promise<PatRotationOut> {
  const res = await apiV2.PATCH('/api/agents/{slug}/pat-rotation', {
    params: { path: { slug } },
    body: { enabled, period_days: periodDays },
  })
  return unwrap(res, 'setPatRotation') as PatRotationOut
}

export type AgentCanopyLoginOut = Schemas['AgentCanopyLoginOut']

function problem(res: { error?: unknown }, what: string): never {
  const e = res.error as { detail?: unknown; title?: unknown } | undefined
  const msg = typeof e?.detail === 'string' ? e.detail : typeof e?.title === 'string' ? e.title : `${what} failed`
  throw new Error(msg)
}

/** Where the agent's secrets live. Needs a passkey step-up, which `apiV2` prompts for. */
export async function setCredentialSource(slug: string, source: '1password' | 'canopy-web'): Promise<void> {
  const res = await apiV2.PATCH('/api/agents/{slug}/credential-source', {
    params: { path: { slug } },
    body: { credential_source: source },
  })
  if (res.error !== undefined) problem(res, 'setCredentialSource')
}

/** Make the agent's canopy login and store it in its Canopy vault. The token is never returned. */
export async function giveCanopyLogin(slug: string): Promise<AgentCanopyLoginOut> {
  const res = await apiV2.POST('/api/agents/{slug}/canopy-login', {
    params: { path: { slug } },
    body: { email: '', label: '' },
  })
  if (res.error !== undefined || res.data === undefined) problem(res, 'giveCanopyLogin')
  return res.data as AgentCanopyLoginOut
}

/** Rotate now. Needs a passkey step-up, which `apiV2` prompts for and retries. */
export async function rotatePatNow(slug: string): Promise<PatRotationOut> {
  const res = await apiV2.POST('/api/agents/{slug}/pat-rotation/rotate', { params: { path: { slug } } })
  return unwrap(res, 'rotatePatNow') as PatRotationOut
}
