import { useCallback, useRef, useState } from 'react'
import type { PendingAttachment } from 'canopy-ui/chat'

import { deleteAttachment, uploadAttachment } from '@/api/chat'

/** Why a private conversation takes no file: it would be its words, kept where canopy
 *  could read them, and the agent's box holds no key to open a sealed one. The server
 *  refuses the upload too (422). */
export const PRIVATE_NO_FILES = "Files can't be attached to a private conversation yet — paste the part that matters as text."

/** A chat's staged attachments. Uploaded eagerly on pick/paste/drop so the file
 *  is on the server before you press send — sending then sweeps up whatever the
 *  session is holding, which is why no ids need to cross the WebSocket. In a private
 *  conversation (`refuse`) a picked file is shown refused, with why, and never sent. */
export function useAttachments(sessionId: string, refuse?: string) {
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
    [sessionId, refuse],
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
