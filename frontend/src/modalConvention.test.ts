import { describe, expect, it } from 'vitest'

/**
 * One modal implementation (docs/architecture/frontend.md § Modals): an overlay on a scrim is the
 * shared `Dialog` from `canopy-ui/ui` (focus trap, Escape, scrim click, aria wiring), not a
 * hand-built `<div className="fixed inset-0 …"><div role="dialog" aria-modal="true">`.
 *
 * The check finds hand-built overlays (`fixed inset-0`, or `role="dialog"` with `aria-modal` on the
 * same element) in app sources. The files below keep one on purpose; each says why in a
 * `shared-dialog-exception:` comment, which this test requires. A new hand-built overlay fails;
 * a migrated one must be deleted from the list so it never goes stale.
 *
 * Not overlays, so not covered: an inline `role="dialog"` panel (no `fixed inset-0`, no
 * `aria-modal`) such as the Emergency Kit, the recovery code, the vault unlock form, and the
 * anchored memory popovers.
 */
const EXCEPTIONS: Record<string, string> = {
  'src/components/work/CommandPalette.tsx': 'a combobox that owns its keys and focus, no title or close button',
  'src/embed/PageGrantConfirm.tsx': 'an act-as-you consent that only its own buttons answer',
  'src/pages/StoryboardPage.tsx': 'a full-screen media lightbox',
}

const raw = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const sources = Object.entries(raw)
  .map(([key, text]) => [`src/${key.slice(2)}`, text] as const)
  .filter(([path]) => !path.endsWith('.test.tsx'))

const FIXED_OVERLAY = /fixed\s+inset-0/
const HAND_BUILT_MODAL = /role="dialog"[^>]{0,120}aria-modal|aria-modal[^>]{0,120}role="dialog"/

function handBuilt(text: string): boolean {
  return FIXED_OVERLAY.test(text) || HAND_BUILT_MODAL.test(text)
}

describe('modal convention', () => {
  it('scans the app sources', () => {
    expect(sources.some(([p]) => p.startsWith('src/pages/'))).toBe(true)
  })

  it('has no hand-built overlay outside the documented exceptions', () => {
    const out = sources
      .filter(([path, text]) => handBuilt(text) && !(path in EXCEPTIONS))
      .map(([path]) => `${path}: hand-built modal — use Dialog/DialogContent from 'canopy-ui/ui'`)
    expect(out).toEqual([])
  })

  it('keeps each exception honest: still hand-built, and says why', () => {
    const bySrc = new Map<string, string>(sources)
    const out: string[] = []
    for (const path of Object.keys(EXCEPTIONS)) {
      const text = bySrc.get(path)
      if (text === undefined) out.push(`${path}: gone — delete its entry`)
      else if (!handBuilt(text)) out.push(`${path}: no longer hand-built — delete its entry`)
      else if (!text.includes('shared-dialog-exception:')) out.push(`${path}: add a "shared-dialog-exception: <why>" comment`)
    }
    expect(out).toEqual([])
  })

  it('catches what it claims to', () => {
    expect(handBuilt('<div className="fixed inset-0 z-50 bg-background/70">')).toBe(true)
    expect(handBuilt('<div role="dialog" aria-modal="true" aria-label="x">')).toBe(true)
    expect(handBuilt('<DialogContent aria-modal="true" className="block">')).toBe(false)
    expect(handBuilt('<div role="dialog" aria-label="Emergency Kit" className="rounded-lg">')).toBe(false)
  })
})
