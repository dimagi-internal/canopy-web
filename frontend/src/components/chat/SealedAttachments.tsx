import { useCallback, useEffect, useMemo, useState } from 'react'
import { Download, Eye, FileLock2, Lock } from 'lucide-react'
import type { AttachmentHost, Message } from 'canopy-ui/chat'

import { fetchSealedAttachment, listSealedAttachments, type SealedAttachment } from '@/api/chat'
import type { ConversationKey } from '@/vault/sealedChat'
import { openSealedBlob, openSealedName, type FileMeta } from '@/vault/sealedFiles'

/** What a private file reads as until its key opens: never its name. */
export const LOCKED_FILE = 'Attachment'

function sizeLabel(bucket: number): string {
  if (bucket >= 1024 * 1024) return `up to ${Math.round(bucket / (1024 * 1024))} MB`
  return `up to ${Math.round(bucket / 1024)} KB`
}

/**
 * The sealed files of a private conversation (apps/canopy_sessions/private_attachments.py),
 * re-read whenever `refresh` changes (a new line of the person's arrived). Empty for an
 * ordinary conversation.
 */
export function useSealedAttachments(sessionId: string, enabled: boolean, refresh: unknown): SealedAttachment[] {
  const [files, setFiles] = useState<SealedAttachment[]>([])
  useEffect(() => {
    if (!enabled || !sessionId) {
      setFiles([])
      return
    }
    let live = true
    listSealedAttachments(sessionId).then((rows) => live && setFiles(rows)).catch(() => undefined)
    return () => {
      live = false
    }
  }, [sessionId, enabled, refresh])
  return files
}

/** The canopy-ui host that shows each line's sealed files under it, by `turn_index`. */
export function sealedAttachmentHost(files: SealedAttachment[], conversationKey: ConversationKey | null,
                                     onUnlock: () => void): AttachmentHost | undefined {
  if (!files.length) return undefined
  const byIndex = new Map<number, SealedAttachment[]>()
  for (const f of files) {
    if (f.turn_index === null || f.turn_index === undefined) continue
    byIndex.set(f.turn_index, [...(byIndex.get(f.turn_index) ?? []), f])
  }
  return {
    render: (m: Message) => {
      const mine = byIndex.get(m.turn_index)
      return mine?.length ? <SealedAttachments files={mine} conversationKey={conversationKey} onUnlock={onUnlock} /> : null
    },
  }
}

export function SealedAttachments({ files, conversationKey, onUnlock }: {
  files: SealedAttachment[]
  conversationKey: ConversationKey | null
  onUnlock: () => void
}) {
  return (
    <ul className="mt-1.5 flex flex-col gap-1" data-testid="sealed-attachments">
      {files.map((f) => (
        <SealedFile key={f.id} file={f} conversationKey={conversationKey} onUnlock={onUnlock} />
      ))}
    </ul>
  )
}

function SealedFile({ file, conversationKey, onUnlock }: {
  file: SealedAttachment
  conversationKey: ConversationKey | null
  onUnlock: () => void
}) {
  const place = useMemo(() => ({ id: String(file.id), uploadedBy: file.uploaded_by ?? '' }), [file.id, file.uploaded_by])
  const [meta, setMeta] = useState<FileMeta | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setMeta(null)
    setError(null)
    if (!conversationKey) return
    let live = true
    openSealedName(file.sealed_name, conversationKey, place)
      .then((m) => live && setMeta(m))
      .catch(() => live && setError('This file could not be opened with your key.'))
    return () => {
      live = false
    }
  }, [file.sealed_name, conversationKey, place])

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview)
  }, [preview])

  const open = useCallback(async (): Promise<Blob | null> => {
    if (!conversationKey || !meta) return null
    setBusy(true)
    try {
      const bytes = await openSealedBlob(await fetchSealedAttachment(String(file.id)), conversationKey, place, meta)
      return new Blob([bytes], { type: meta.type || 'application/octet-stream' })
    } catch {
      setError('This file could not be opened with your key.')
      return null
    } finally {
      setBusy(false)
    }
  }, [conversationKey, meta, file.id, place])

  const download = useCallback(async () => {
    const blob = await open()
    if (!blob || !meta) return
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = meta.name
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }, [open, meta])

  const showPreview = useCallback(async () => {
    if (preview) {
      setPreview(null)
      return
    }
    const blob = await open()
    if (blob) setPreview(URL.createObjectURL(blob))
  }, [open, preview])

  if (!conversationKey) {
    return (
      <li>
        <button type="button" onClick={onUnlock} data-testid="sealed-attachment-locked"
          className="inline-flex items-center gap-1 rounded-md border border-current/30 px-1.5 py-0.5 text-[12px] opacity-90 hover:opacity-100">
          <Lock className="h-3 w-3" aria-hidden="true" />
          {LOCKED_FILE}
          <span className="opacity-70">· {sizeLabel(file.size_bucket)} · unlock to open</span>
        </button>
      </li>
    )
  }
  return (
    <li className="text-[12px]" data-testid="sealed-attachment">
      <span className="inline-flex flex-wrap items-center gap-1.5 rounded-md border border-current/30 px-1.5 py-0.5">
        <FileLock2 className="h-3 w-3" aria-hidden="true" />
        <span className="font-medium">{error ? LOCKED_FILE : meta?.name ?? 'Opening…'}</span>
        {meta && !error && (
          <>
            {meta.type.startsWith('image/') && (
              <button type="button" onClick={() => void showPreview()} disabled={busy}
                className="inline-flex items-center gap-0.5 underline-offset-2 hover:underline" aria-label={`Preview ${meta.name}`}>
                <Eye className="h-3 w-3" aria-hidden="true" />{preview ? 'Hide' : 'Preview'}
              </button>
            )}
            <button type="button" onClick={() => void download()} disabled={busy}
              className="inline-flex items-center gap-0.5 underline-offset-2 hover:underline" aria-label={`Download ${meta.name}`}>
              <Download className="h-3 w-3" aria-hidden="true" />Download
            </button>
          </>
        )}
      </span>
      {error && <div className="mt-0.5 opacity-80" role="status">{error}</div>}
      {preview && meta && (
        <img src={preview} alt={meta.name} className="mt-1 max-h-64 max-w-full rounded-md border border-border" />
      )}
    </li>
  )
}
