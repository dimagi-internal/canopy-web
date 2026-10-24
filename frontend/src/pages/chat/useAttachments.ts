import { useCallback, useRef, useState } from 'react'
import type { PendingAttachment } from 'canopy-ui/chat'

import { deleteAttachment, uploadAttachment, uploadSealedCopy, type Attachment } from '@/api/chat'
import { sealAttachment, type SealTarget } from '@/vault/sealedFiles'

/** Why a private conversation's file could not be attached here: it is sealed to the
 *  conversation's key in this browser, and there is none to seal to yet. */
export const PRIVATE_NO_KEY = "This private conversation has no key yet, so a file can't be sealed to its people. Send a message first."

/** A private conversation's file is two uploads (apps/canopy_sessions/private_attachments.py):
 *  the file for the agent's one turn, then the people's copy, sealed here. */
export interface PrivateFiles {
  /** The conversation and its public key (`SessionDetailOut.sealed_key`). */
  target: SealTarget
  /** The uploader, bound into the seal. */
  userId: number | string
}

/** How the composer reaches canopy for files: canopy's own chat page uses its signed-in
 *  routes (the default); the embedded panel passes its frame's delegated-token client. */
export interface AttachmentApi {
  upload(sessionId: string, file: File): Promise<Attachment>
  uploadSealed(attachmentId: string, sealedName: string, blob: Uint8Array): Promise<void>
  remove(attachmentId: string): Promise<void>
}

export const pageAttachmentApi: AttachmentApi = {
  upload: uploadAttachment,
  uploadSealed: uploadSealedCopy,
  remove: deleteAttachment,
}

/** Seal `file` (already uploaded as `saved`) to the conversation and store that copy. */
export async function storeSealedCopy(saved: Attachment, file: File, privateFiles: PrivateFiles,
                                      api: AttachmentApi = pageAttachmentApi): Promise<void> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  const { sealedName, blob } = await sealAttachment(privateFiles.target,
    { id: String(saved.id), uploadedBy: privateFiles.userId }, { name: file.name, type: file.type, bytes })
  await api.uploadSealed(String(saved.id), sealedName, blob)
}

/** A chat's staged attachments. Uploaded eagerly on pick/paste/drop so the file
 *  is on the server before you press send — sending then sweeps up whatever the
 *  session is holding, which is why no ids need to cross the WebSocket.
 *
 *  In a private conversation (`privateFiles`) each file is also sealed here to the
 *  conversation's people, and the chip stays "uploading" until both copies are stored:
 *  the server sends only a file whose sealed copy it holds. A file that cannot be
 *  sealed is discarded and the chip says why. `refuse` shows a picked file refused,
 *  with why, and sends nothing. */
export function useAttachments(sessionId: string, refuse?: string, privateFiles?: PrivateFiles | null,
                               api: AttachmentApi = pageAttachmentApi) {
  const [attachments, setAttachments] = useState<PendingAttachment[]>([])

  // Uploads go one at a time: the server hands files to the runner in upload
  // order, and the composer's "[Image #N]" means the Nth file staged.
  const uploads = useRef<Promise<unknown>>(Promise.resolve())
  const attach = useCallback(
    (files: File[]) => {
      for (const file of files) {
        // Optimistic chip keyed on a temp id, swapped for the server's id on
        // success. Without it a large paste looks like nothing happened.
        const tempId = `pending:${file.name}:${Date.now()}:${Math.random()}`
        if (refuse) {
          setAttachments((prev) => [...prev, { id: tempId, filename: file.name, uploading: false, error: refuse }])
          continue
        }
        setAttachments((prev) => [...prev, { id: tempId, filename: file.name, uploading: true }])
        uploads.current = uploads.current.then(async () => {
          let saved: Attachment | null = null
          try {
            saved = await api.upload(sessionId, file)
            if (privateFiles) await storeSealedCopy(saved, file, privateFiles, api)
            const done = saved
            // A private file's row holds no name: the chip keeps the one this browser picked.
            setAttachments((prev) =>
              prev.map((a) => (a.id === tempId
                ? { id: String(done.id), filename: privateFiles ? file.name : done.filename } : a)))
          } catch (err: unknown) {
            // A private file whose sealed copy did not land is not sent: forget the
            // agent's copy now rather than wait for the sweep.
            if (saved && privateFiles) void api.remove(String(saved.id)).catch(() => undefined)
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === tempId
                  ? { ...a, uploading: false, error: err instanceof Error ? err.message : 'failed' }
                  : a,
              ),
            )
          }
        })
      }
    },
    [sessionId, refuse, privateFiles, api],
  )

  const remove = useCallback((attachmentId: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== attachmentId))
    // A failed upload has no server row (its id is still the temp one), so only
    // ask the server to forget the ones it actually knows about.
    if (!attachmentId.startsWith('pending:')) {
      api.remove(attachmentId).catch(() => {
        /* the chip is already gone from the UI; a stray row is swept server-side */
      })
    }
  }, [api])

  // The server consumes whatever the session is holding, so the chips are spent
  // the moment we send — leaving them would imply they ride along again.
  const clear = useCallback(() => setAttachments([]), [])

  return { attachments, attach, remove, clear }
}
