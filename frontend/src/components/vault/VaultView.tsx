import type { ReactNode } from 'react'

import { VaultSection, type VaultTone } from '@/components/vault/VaultSection'

// THE ONE WAY A VAULT IS SHOWN — an agent's and a workspace's alike. A summary line
// that answers "what is in it and who can open it" at a glance, then collapsible
// sections in a fixed order:
//
//   Secrets            — what it holds
//   Who can open them  — the people, and the machines
//   Storage & key      — its key and recovery (rarely touched)
//
// The page that owns the vault supplies each section's body and a one-line summary
// (its adapter); this decides the frame, the order and what opens by itself. Anything
// else about the object (an agent's canopy login, a workspace vault's agents) goes in
// `children`, as more `VaultSection`s, or beside the vault, not inside it.

export interface VaultFacts {
  secrets?: number
  people?: number
  machines?: number
  /** What needs the reader, in words; shown in warning colour. */
  attention?: string[]
}

export interface VaultPart {
  summary?: ReactNode
  tone?: VaultTone
  anchors?: string[]
  defaultOpen?: boolean
  body: ReactNode
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`

export function VaultSummary({ facts }: { facts: VaultFacts }) {
  const parts = [
    'Canopy vault',
    'canopy-web can’t read it',
    facts.secrets !== undefined ? plural(facts.secrets, 'secret') : null,
    facts.people !== undefined ? plural(facts.people, 'person', 'people') : null,
    facts.machines !== undefined ? plural(facts.machines, 'machine') : null,
  ].filter(Boolean)
  return (
    <div data-testid="vault-summary">
      <p className="text-[13px] text-foreground">{parts.join(' · ')}</p>
      {facts.attention && facts.attention.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-[12px] text-warning" data-testid="vault-attention">
          {facts.attention.map((a) => <li key={a}>{a}</li>)}
        </ul>
      )}
    </div>
  )
}

export function VaultView({
  facts,
  secrets,
  access,
  storage,
  children,
}: {
  facts: VaultFacts
  secrets: VaultPart
  access: VaultPart
  storage: VaultPart
  /** More sections after these, still about this vault. */
  children?: ReactNode
}) {
  return (
    <div data-testid="vault-view">
      <VaultSummary facts={facts} />
      <div className="mt-2">
        <VaultSection id="secrets" title="Secrets" {...rest(secrets)}>{secrets.body}</VaultSection>
        <VaultSection id="access-to-secrets" title="Who can open them" {...rest(access)}>{access.body}</VaultSection>
        <VaultSection id="vault-storage" title="Storage & key" {...rest(storage)}>{storage.body}</VaultSection>
        {children}
      </div>
    </div>
  )
}

/** A part's section props: everything but the body, which is the section's children. */
function rest(part: VaultPart) {
  const { summary, tone, anchors, defaultOpen } = part
  return { summary, tone, anchors, defaultOpen }
}
