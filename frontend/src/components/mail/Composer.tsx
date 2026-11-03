import { useRef, useState, type JSX } from 'react'

import { uploadMailAttachment, type MailUploadOut } from '@/api/mail'
import { invalidAddresses, parseAddresses } from '@/components/mail/addresses'
import { fileSize } from '@/components/mail/MessageView'

export type Draft = {
  id: string
  /** The thread a reply answers; "" for a new message. */
  threadId: string
  from: string
  to: string
  cc: string
  bcc: string
  showBcc: boolean
  subject: string
  text: string
  uploads: MailUploadOut[]
}

export const mailButton =
  'min-h-11 rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted disabled:opacity-40 sm:min-h-0'
export const mailPrimary =
  'min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0'
export const mailInput = 'rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground'

const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

export function Composer({ workspace, draft, addresses, replyFrom, busy, saved, saveFailed, error, note, onChange, onSend, onClose, onDiscard, onUploaded }: {
  workspace: string
  draft: Draft
  note: string
  addresses: string[]
  /** The address a reply goes out from, shown because it cannot be chosen. */
  replyFrom: string
  busy: boolean
  saved: boolean
  saveFailed: boolean
  error: string | null
  onChange: (d: Draft) => void
  onSend: () => void
  /** Close, keeping the draft. */
  onClose: () => void
  onDiscard: () => void
  /** A file finished uploading. Separate from onChange: the person may have typed on meanwhile. */
  onUploaded: (u: MailUploadOut) => void
}): JSX.Element {
  const isNew = draft.threadId === ''
  const [uploading, setUploading] = useState(0)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch })
  const bad = [draft.to, draft.cc, draft.bcc].flatMap(invalidAddresses)
  const ready = parseAddresses(draft.to).length > 0 && bad.length === 0 && uploading === 0 && draft.text.trim() !== ''
    && (!isNew || (draft.from !== '' && draft.subject.trim() !== ''))

  async function attach(files: FileList | null) {
    setUploadError(null)
    let total = draft.uploads.reduce((n, u) => n + u.size, 0)
    for (const file of Array.from(files ?? [])) {
      total += file.size
      if (total > MAX_ATTACHMENT_BYTES) {
        setUploadError(`${file.name} is too large: attachments are limited to 10 MB per message.`)
        break
      }
      setUploading((n) => n + 1)
      try {
        onUploaded(await uploadMailAttachment(workspace, file))
      } catch (e: unknown) {
        setUploadError(e instanceof Error ? e.message : `Could not attach ${file.name}`)
      } finally {
        setUploading((n) => n - 1)
      }
    }
    if (fileRef.current) fileRef.current.value = ''
  }

  const row = 'flex items-center gap-2 text-[12px] text-muted-foreground'
  return (
    <form
      aria-label={isNew ? 'New message' : 'Reply'}
      className="mt-4 flex flex-col gap-2 rounded-md border border-border p-3"
      onSubmit={(e) => { e.preventDefault(); if (ready && !busy) onSend() }}
      onKeyDown={(e) => {
        if (e.key === 'Escape') { e.stopPropagation(); onClose() }
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && ready && !busy) { e.preventDefault(); onSend() }
      }}
    >
      {isNew ? (
        <label className={row}>
          <span className="w-12">From</span>
          <select aria-label="From" className={`${mailInput} min-w-0 flex-1`} value={draft.from} onChange={(e) => set({ from: e.target.value })}>
            <option value="">Choose an address</option>
            {addresses.map((a) => <option key={a} value={a}>{a}</option>)}
          </select>
        </label>
      ) : replyFrom && (
        <div className={row}><span className="w-12">From</span><span className="truncate text-foreground">{replyFrom}</span></div>
      )}
      <label className={row}>
        <span className="w-12">To</span>
        <input aria-label="To" className={`${mailInput} min-w-0 flex-1`} value={draft.to} onChange={(e) => set({ to: e.target.value })}
          placeholder="name@example.com, another@example.com" />
      </label>
      <label className={row}>
        <span className="w-12">Cc</span>
        <input aria-label="Cc" className={`${mailInput} min-w-0 flex-1`} value={draft.cc} onChange={(e) => set({ cc: e.target.value })} />
        {!draft.showBcc && <button type="button" className="text-[12px] text-primary hover:underline" onClick={() => set({ showBcc: true })}>Bcc</button>}
      </label>
      {draft.showBcc && (
        <label className={row}>
          <span className="w-12">Bcc</span>
          <input aria-label="Bcc" className={`${mailInput} min-w-0 flex-1`} value={draft.bcc} onChange={(e) => set({ bcc: e.target.value })} />
        </label>
      )}
      {bad.length > 0 && <p role="alert" className="text-[12px] text-destructive">Not an email address: {bad.join(', ')}</p>}
      {isNew && (
        <label className={row}>
          <span className="w-12">Subject</span>
          <input aria-label="Subject" className={`${mailInput} min-w-0 flex-1`} value={draft.subject} onChange={(e) => set({ subject: e.target.value })} />
        </label>
      )}
      <textarea aria-label="Message" rows={isNew ? 12 : 8} className={`${mailInput} w-full font-sans`} value={draft.text}
        onChange={(e) => set({ text: e.target.value })} autoFocus />
      {(draft.uploads.length > 0 || uploading > 0) && (
        <ul aria-label="Attachments" className="flex flex-wrap gap-2 text-[12px]">
          {draft.uploads.map((u) => (
            <li key={u.id} className="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-foreground">
              {u.filename} <span className="text-muted-foreground">{fileSize(u.size)}</span>
              <button type="button" aria-label={`Remove ${u.filename}`} className="ml-1 text-muted-foreground hover:text-foreground"
                onClick={() => set({ uploads: draft.uploads.filter((x) => x.id !== u.id) })}>×</button>
            </li>
          ))}
          {uploading > 0 && <li className="px-2 py-1 text-muted-foreground">Attaching…</li>}
        </ul>
      )}
      {saveFailed && <p role="alert" className="text-[12px] text-destructive">This draft could not be saved. Copy your words before leaving this page.</p>}
      {(error || uploadError) && <p role="alert" className="text-[12px] text-destructive">{error || uploadError}</p>}
      <div className="flex flex-wrap items-center gap-2">
        <button type="submit" className={mailPrimary} disabled={!ready || busy}>{busy ? 'Sending…' : 'Send'}</button>
        <button type="button" className={mailButton} onClick={() => fileRef.current?.click()} disabled={busy}>Attach</button>
        <input ref={fileRef} type="file" multiple hidden aria-label="Attach files" onChange={(e) => void attach(e.target.files)} />
        <button type="button" className={mailButton} onClick={onClose} disabled={busy}>Close</button>
        <button type="button" className={mailButton} onClick={onDiscard} disabled={busy}>Discard</button>
        <span className="text-[11px] text-muted-foreground">
          {saved ? 'Draft saved. ' : ''}{note}<span className="hidden lg:inline"> Ctrl/⌘+Enter sends.</span>
        </span>
      </div>
    </form>
  )
}
