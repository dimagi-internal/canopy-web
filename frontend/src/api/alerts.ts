import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

// Alerts — a system problem a person can fix (a dead runner credential, failed
// turns nobody could handle, stuck turns, a waiting device key, an agent with no
// runner it can use). One record per problem, read the same way by every screen
// (`docs/architecture/alerts.md`); a screen narrows by `scope`, never by its own rules.

type AlertWire = components['schemas']['AlertOut']
export type AlertAction = components['schemas']['AlertActionOut']
/** The wire alert with its arrays made plain (openapi-fetch degrades readonly
 *  arrays to ArrayLike, so they are rebuilt here once). */
export type Alert = Omit<AlertWire, 'actions' | 'scopes'> & { actions: AlertAction[]; scopes: string[] }
export type AlertSeverity = Alert['severity']
export type AlertStatus = 'open' | 'dismissed'

const normalize = (a: AlertWire): Alert => ({
  ...a,
  actions: Array.from(a.actions ?? []),
  scopes: Array.from(a.scopes ?? []),
})

/** Open alerts you have an action on, most severe first then newest — across
 *  every workspace you are in (one user-level read). `scope` keeps those that
 *  belong on one screen: `workspace:<slug>`, `runner:<id>`, `agent:<slug>`.
 *  `status: 'dismissed'` lists what was dismissed in the last 7 days. */
export async function loadAlerts(scope?: string, status: AlertStatus = 'open'): Promise<Alert[]> {
  const query = scope ? { scope, status } : { status }
  const res = await apiV2.GET('/api/me/alerts/', { params: { query } })
  const body = unwrap(res, 'listAlerts') as unknown as AlertWire[]
  return Array.from(body, normalize)
}

/** Run one of the alert's actions (dismiss included). The server re-checks the
 *  action is still yours: 409 `action-not-offered` when it is not, 409
 *  `action-failed` (its message is the reason) when the handler refused. */
export async function actOnAlert(id: number, action: string): Promise<Alert> {
  const res = await apiV2.POST('/api/me/alerts/{id}/act', { params: { path: { id } }, body: { action } })
  return normalize(unwrap(res, 'actOnAlert') as unknown as AlertWire)
}

/** Put a dismissed alert back on everyone's list. */
export async function undismissAlert(id: number): Promise<Alert> {
  const res = await apiV2.POST('/api/me/alerts/{id}/undismiss', { params: { path: { id } } })
  return normalize(unwrap(res, 'undismissAlert') as unknown as AlertWire)
}
