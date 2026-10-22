import { useCallback, useRef, useState } from 'react'
import type { PendingAttachment } from 'canopy-ui/chat'

import { deleteAttachment, uploadAttachment } from '@/api/chat'

/** A chat's staged attachments. Uploaded eagerly on pick/paste/drop so the file
 *  is on the server before you press send — sending then sweeps up whatever the
 *  session is holding, which is why no ids need to cross the WebSocket. */
export function useAttachments(sessionId: string) {
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
        setAttachments((prev) => [...prev, { id: tempId, filename: file.name, uploading: true }])
        uploads.current = uploads.current.then(() => uploadAttachment(sessionId, file)
          .then((saved) =>
            setAttachments((prev) =>
              prev.map((a) => (a.id === tempId ? { id: saved.id, filename: saved.filename } : a)),
            ),
          )
          .catch((err: unknown) =>
            setAttachments((prev) =>
              prev.map((a) =>
                a.id === tempId
                  ? { ...a, uploading: false, error: err instanceof Error ? err.message : 'failed' }
                  : a,
              ),
            ),
          ))
      }
    },
    [sessionId],
  )

  const remove = useCallback((attachmentId: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== attachmentId))
    // A failed upload has no server row (its id is still the temp one), so only
    // ask the server to forget the ones it actually knows about.
    if (!attachmentId.startsWith('pending:')) {
      deleteAttachment(attachmentId).catch(() => {
        /* the chip is already gone from the UI; a stray row is swept server-side */
      })
    }
  }, [])

  // The server consumes whatever the session is holding, so the chips are spent
  // the moment we send — leaving them would imply they ride along again.
  const clear = useCallback(() => setAttachments([]), [])

  return { attachments, attach, remove, clear }
}
