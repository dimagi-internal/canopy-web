import {
  newClientId,
  type AppHost,
  type AppResourceUpdates,
  type AppViewResource,
  type CallToolResult,
} from 'canopy-ui/chat'
import { type CanopyClient } from 'canopy-client'
import { currentFrameBaseUrl } from './frameBase'
import { isNoGrant } from './pageGrant'

/** The embed panel's MCP Apps host: the same five routes as canopy's chat page,
 *  under `/api/contact/` for a contact (their whole surface) — through the
 *  panel's own client, so the visitor's token is what authorises every click. */
export function embedAppHost(client: CanopyClient, sessionPath: string, expanded = false,
                      updates?: AppResourceUpdates,
                      grant?: { forCard: () => Promise<void>, forSend: () => Promise<void> }): AppHost {
  const withGrant = grant?.forCard
  const at = (callId: string) => `${sessionPath}/apps/${encodeURIComponent(callId)}`
  // A View reading the site as the person needs this tab's act-as-you permission.
  // Opening their own conversation is the person's act, so a card that finds none
  // asks for one (the panel then shows "can act as you … · Stop") and tries once more.
  const granted = async <T,>(go: () => Promise<T>): Promise<T> => {
    try {
      return await go()
    } catch (error) {
      if (!withGrant || !isNoGrant(error)) throw error
      await withGrant()
      return go()
    }
  }
  const send = <T,>(path: string, method: string, body: unknown) =>
    granted(() => client.rest.json(path, { method, body: JSON.stringify(body) }) as Promise<T>)
  return {
    // A card that loads read-only for want of a grant asks for one, then loads again.
    load: async (callId) => {
      const read = () => client.rest.json(`${at(callId)}/resource`) as Promise<AppViewResource>
      const res = await read()
      if (res.can_act || !withGrant) return res
      await withGrant()
      return read()
    },
    callTool: (callId, name, args) =>
      send<CallToolResult>(`${at(callId)}/call`, 'POST', { name, arguments: args }),
    readResource: (callId, uri) => send(`${at(callId)}/read`, 'POST', { uri }),
    updateContext: (callId, params) =>
      send<void>(`${at(callId)}/context`, 'PUT', params).then(() => undefined),
    // A View's ui/message is the person sending: the grant first, as for a typed send.
    sendMessage: (callId, text) =>
      (grant ? grant.forSend() : Promise.resolve())
        .then(() => send<void>(`${at(callId)}/message`, 'POST', { text, client_id: newClientId() }))
        .then(() => undefined),
    subscribe: (callId, uri) => send<void>(`${at(callId)}/subscribe`, 'POST', { uri }).then(() => undefined),
    unsubscribe: (callId, uri) => send<void>(`${at(callId)}/unsubscribe`, 'POST', { uri }).then(() => undefined),
    ...(updates ? { onResourceUpdated: updates.onResourceUpdated } : {}),
    resolveUrl: (path) => (/^https?:\/\//.test(path) ? path : `${currentFrameBaseUrl()}${path}`),
    // Narrow-panel sized, until the visitor expands the panel: then the View
    // gets the panel's width (no cap) and most of the window's height.
    ...(expanded
      ? { maxHeight: Math.max(900, Math.round(window.innerHeight * 0.85)) }
      : { maxWidth: 420 }),
    // A View may ask to go full screen: it is drawn over the panel.
    fullscreen: true,
  }
}
