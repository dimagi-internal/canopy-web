// A screen's calls through its connections: `apps/connections/api.py`. Refusals are problems whose `detail` is a
// sentence to show (and whose `error` says which); `ApiError` carries both. Used only by the page around a sandboxed
// frame (`dynamicViews/connectionCalls.ts`), never by the frame.
import { apiV2 } from './client.v2'
import { unwrap } from './problem'
import type { components } from './generated'
import type { CallBody, CallResult, ConnectionClient, Operation, Surface } from '../dynamicViews/connectionCalls'

export const connectionClient: ConnectionClient = {
  async request(provider: string, body: CallBody, signal?: AbortSignal): Promise<CallResult> {
    const res = await apiV2.POST('/api/connections/{provider}/request', {
      params: { path: { provider } },
      body: body as unknown as components['schemas']['ConnectionCallIn'],
      signal,
    })
    return unwrap(res, 'That call did not go through.') as unknown as CallResult
  },
  async openOperation(surface: Surface, signal?: AbortSignal): Promise<Operation> {
    const res = await apiV2.POST('/api/connections/operations', {
      body: { surface } as unknown as components['schemas']['ConnectionOperationIn'],
      signal,
    })
    return unwrap(res, 'Could not start that.') as unknown as Operation
  },
}
