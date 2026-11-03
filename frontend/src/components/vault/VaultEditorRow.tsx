import type { ReactNode } from 'react'
import { Lock } from 'lucide-react'

import { Button, CopyButton, TableCell, TableRow } from 'canopy-ui/ui'
import { toneClasses } from 'canopy-ui/lib'
import { relativeAge } from '@/lib/relativeAge'

import type { VaultEntry } from './VaultEditor'

// One row of the vault table, and the badge that says how values are held. The table (search, add,
// the draft being edited) is `VaultEditor`; this only draws what a row shows and the buttons on it.

export function VaultRow({ entry: e, lastUsed, revealed, canEdit, busy, detailsOpen, onChange, onReveal, onDelete, onDetails }: {
  entry: VaultEntry
  lastUsed: ReactNode
  revealed: string | null
  canEdit: boolean
  busy: boolean
  /** Whether this row's activity panel is open; undefined when it has none. */
  detailsOpen?: boolean
  onChange: () => void
  onReveal?: () => void
  onDelete?: () => void
  onDetails?: () => void
}) {
  // The row is the way in to the secret: a stored one opens (reveal), one with no value
  // takes its value. A click on a button, or one that ends a text selection, is not that.
  const activate = canEdit ? (onReveal ?? (e.set || e.sealed ? undefined : onChange)) : undefined
  return (
    <>
      <TableRow
        data-testid={`vault-row-${e.name}`}
        className={activate && !busy ? 'cursor-pointer' : undefined}
        onClick={activate && ((ev) => {
          if (busy || (ev.target as HTMLElement).closest('button, a, input, label, textarea')) return
          if (window.getSelection()?.toString()) return
          activate()
        })}
      >
        <TableCell className="font-mono text-foreground">
          {e.name}
          {e.flag && (
            <span className={`ml-2 rounded border px-1.5 py-0.5 font-sans text-[10px] ${toneClasses(e.flag.tone)}`}>
              {e.flag.label}
            </span>
          )}
        </TableCell>
        <TableCell className="hidden text-muted-foreground sm:table-cell">{e.set ? '••••••••' : '—'}</TableCell>
        <TableCell className="hidden text-muted-foreground sm:table-cell">
          {e.updatedAt ? relativeAge(e.updatedAt) : ''}
          {e.updatedBy ? ` · ${e.updatedBy.split('@')[0]}` : ''}
        </TableCell>
        <TableCell className="hidden text-muted-foreground sm:table-cell">{lastUsed}</TableCell>
        <TableCell className="text-right">
          {canEdit && (
            <div className="flex justify-end gap-1">
              {onDetails && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={onDetails}
                  aria-expanded={detailsOpen}
                  data-testid={`vault-details-${e.name}`}
                  className="text-muted-foreground"
                >
                  Activity
                </Button>
              )}
              {!e.sealed && (
                <Button size="sm" variant="outline" onClick={onChange} disabled={busy} aria-label={`${e.set ? 'Change' : 'Set'} ${e.name}`}>
                  {e.set ? 'Change' : 'Set'}
                </Button>
              )}
              {onReveal && (
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onReveal}
                  disabled={busy}
                  aria-label={revealed !== null ? `Hide the value of ${e.name}` : `Reveal the value of ${e.name}`}
                  title="Shows the stored value for a minute — needs your passkey, and is recorded"
                  data-testid={`reveal-${e.name}`}
                >
                  {revealed !== null ? 'Hide' : 'Reveal'}
                </Button>
              )}
              {onDelete && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={onDelete}
                  disabled={busy}
                  aria-label={`Delete the stored value for ${e.name}`}
                  title={`Delete the stored value for "${e.name}" (cannot be undone)`}
                  className="text-muted-foreground hover:text-destructive"
                >
                  ✕
                </Button>
              )}
            </div>
          )}
        </TableCell>
      </TableRow>
      {revealed !== null && (
        <TableRow data-testid={`revealed-${e.name}`}>
          <TableCell colSpan={5}>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 select-all whitespace-normal break-all rounded border border-warning/30 bg-warning/10 px-2 py-1 font-mono text-[12px] text-foreground">
                {revealed}
              </code>
              <CopyButton text={revealed} variant="outline" />
            </div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

/** "Encrypted in your browser" — for a vault whose values canopy-web never sees. */
export function EncryptedBadge({ children = 'Encrypted in your browser' }: { children?: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] ${toneClasses('success')}`} data-testid="vault-encrypted">
      <Lock className="h-3 w-3" aria-hidden="true" />
      {children}
    </span>
  )
}
