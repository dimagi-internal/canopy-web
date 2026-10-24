/**
 * Files in the embedded panel, through the frame's own delegated-token client.
 *
 * The panel runs in the person's browser, in canopy's own frame, so its requests are
 * `Sec-Fetch-Site: same-origin` — the mark canopy-web uses to tell the person's browser
 * from the site's backend holding the same token (`sensitivity.request_is_panel_browser`).
 * A private conversation's file is attached, and its sealed copy stored, only from here
 * or canopy's chat page (apps/canopy_sessions/private_attachments.py).
 */
import type { CanopyRest } from 'canopy-client'

import type { Attachment, SealedAttachment } from '@/api/chat'
import type { SealedFilesApi } from '@/components/chat/SealedAttachments'
import type { AttachmentApi } from '@/pages/chat/useAttachments'

type Rest = Pick<CanopyRest, 'raw' | 'json'>

/** `fetch` that lets the browser write a multipart body's own Content-Type: canopy-client
 *  labels every request with a body as JSON, which would break a FormData upload. */
export function formDataSafeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (init?.body instanceof FormData && init.headers && !(init.headers instanceof Headers) && !Array.isArray(init.headers)) {
    const { 'Content-Type': _json, ...headers } = init.headers as Record<string, string>
    void _json
    return fetch(input, { ...init, headers })
  }
  return fetch(input, init)
}

async function ok(r: Response, what: string): Promise<Response> {
  if (r.ok) return r
  const problem = await r.json().catch(() => undefined) as { detail?: string } | undefined
  throw new Error(problem?.detail || `${what} failed (${r.status})`)
}

export function panelAttachmentApi(rest: Rest): AttachmentApi {
  return {
    async upload(sessionId, file) {
      const body = new FormData()
      body.append('file', file)
      const r = await ok(await rest.raw(`/api/canopy-sessions/${encodeURIComponent(sessionId)}/attachments`,
        { method: 'POST', body }), 'upload')
      return (await r.json()) as Attachment
    },
    async uploadSealed(attachmentId, sealedName, blob) {
      const body = new FormData()
      body.append('sealed_name', sealedName)
      body.append('file', new Blob([blob as Uint8Array<ArrayBuffer>], { type: 'application/octet-stream' }), 'sealed')
      await ok(await rest.raw(`/api/canopy-sessions/attachments/${encodeURIComponent(attachmentId)}/sealed`,
        { method: 'POST', body }), 'sealing')
    },
    async remove(attachmentId) {
      const r = await rest.raw(`/api/canopy-sessions/attachments/${encodeURIComponent(attachmentId)}`,
        { method: 'DELETE' })
      if (!r.ok && r.status !== 404) await ok(r, 'delete')
    },
  }
}

export function panelSealedFilesApi(rest: Rest): SealedFilesApi {
  return {
    list: (sessionId) => rest.json<SealedAttachment[]>(
      `/api/canopy-sessions/${encodeURIComponent(sessionId)}/sealed-attachments`),
    async fetch(attachmentId) {
      const r = await ok(await rest.raw(`/api/canopy-sessions/attachments/${encodeURIComponent(attachmentId)}/sealed`),
        'fetching the file')
      return new Uint8Array(await r.arrayBuffer())
    },
  }
}
