import { useEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Tabs, TabsList, TabsTrigger } from 'canopy-ui/ui'

import { VaultSection, type VaultTone } from '@/components/vault/VaultSection'

// THE ONE WAY A VAULT IS SHOWN — an agent's and a workspace's alike. A summary line
// that answers "what is in it and who can open it" at a glance, then collapsible
// sections in a fixed order:
//
//   Secrets         — what it holds
//   Access          — who and what gets it, one tab per kind: People, Agents,
//                     Projects, Workspaces, Machines (a vault shows the kinds it has)
//   Encryption key  — the one key everything is locked with, and who holds a copy
//   Log             — what changed and who read what
//
// The page that owns the vault supplies each section's body and a one-line summary
// (its adapter); this decides the frame, the order, the tab names and what opens by
// itself. A step that must come before anything else (a new vault's key) goes in
// `firstStep`, above the sections. Anything else about the object goes in `children`,
// as more `VaultSection`s, or beside the vault, not inside it.

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

/** The kinds of thing a vault can be open to, in the order every vault shows them. */
export const ACCESS_KINDS = ['people', 'agents', 'projects', 'workspaces', 'machines'] as const
export type AccessKind = (typeof ACCESS_KINDS)[number]
const KIND_LABEL: Record<AccessKind, string> = {
  people: 'People', agents: 'Agents', projects: 'Projects', workspaces: 'Workspaces', machines: 'Machines',
}

export interface AccessTab {
  kind: AccessKind
  /** How many there are, shown on the tab; left out while it loads. */
  count?: number
  /** Something on this tab needs the reader (a machine waiting for the key). */
  attention?: boolean
  /** Old addresses (`#cred-device-keys`) that mean "this tab". */
  anchors?: string[]
  body: ReactNode
}

export interface VaultAccessPart extends Omit<VaultPart, 'body'> {
  /** One per kind this vault can be open to; drawn in ACCESS_KINDS order. */
  tabs: AccessTab[]
  /** A line above the tabs (what Access means for this vault). */
  intro?: ReactNode
  /** Shown under the tabs whatever is chosen (a "Who can see this" note). */
  footer?: ReactNode
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

/** What the Encryption key section is for, in one sentence every vault shows first. */
export const KEY_EXPLAINER =
  'Everything in this vault is locked with one key. Each person and machine under Access holds its own copy, ' +
  'and canopy-web never does. Change the key after removing someone, so the copy they kept opens nothing new.'

/** Access: one tab per kind. Every tab stays mounted (hidden when not chosen) so its data
 *  loads for the counts and an old address into it still lands. */
export function VaultAccessTabs({ tabs }: { tabs: AccessTab[] }) {
  const { hash } = useLocation()
  const target = decodeURIComponent(hash.replace(/^#/, ''))
  const ordered = ACCESS_KINDS.map((k) => tabs.find((t) => t.kind === k)).filter((t): t is AccessTab => Boolean(t))
  const pointed = ordered.find((t) => t.anchors?.includes(target) || `access-${t.kind}` === target)
  const [chosen, setChosen] = useState<AccessKind>(pointed?.kind ?? ordered[0]?.kind ?? 'people')
  useEffect(() => {
    if (pointed) setChosen(pointed.kind)
  }, [pointed])
  if (ordered.length === 0) return null
  return (
    <div data-testid="vault-access-tabs">
      <Tabs value={chosen} onValueChange={(v) => setChosen(v as AccessKind)}>
        <TabsList>
          {ordered.map((t) => (
            <TabsTrigger key={t.kind} value={t.kind} data-testid={`vault-access-tab-${t.kind}`}>
              {KIND_LABEL[t.kind]}
              {t.count !== undefined && <span className="ml-1 text-muted-foreground">{t.count}</span>}
              {t.attention && <span className="ml-1 text-warning" aria-label="needs you">●</span>}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>
      {ordered.map((t) => (
        <div key={t.kind} id={`access-${t.kind}`} hidden={t.kind !== chosen} className="mt-3 scroll-mt-6"
          data-testid={`vault-access-panel-${t.kind}`}>
          {t.body}
        </div>
      ))}
    </div>
  )
}

export function VaultView({
  facts,
  secrets,
  access,
  encryptionKey,
  log,
  firstStep,
  children,
}: {
  facts: VaultFacts
  secrets: VaultPart
  access: VaultAccessPart
  encryptionKey: VaultPart
  /** What changed and who read what. Left out where nobody may see it. */
  log?: VaultPart
  /** What the reader must do before anything else, shown above the sections. */
  firstStep?: ReactNode
  /** More sections after these, still about this vault. */
  children?: ReactNode
}) {
  const { tabs, intro, footer, ...accessProps } = access
  const tabAnchors = tabs.flatMap((t) => [`access-${t.kind}`, ...(t.anchors ?? [])])
  return (
    <div data-testid="vault-view">
      <VaultSummary facts={facts} />
      {firstStep && <div className="mt-3" data-testid="vault-first-step">{firstStep}</div>}
      <div className="mt-2">
        <VaultSection id="secrets" title="Secrets" {...sectionProps(secrets)}>{secrets.body}</VaultSection>
        <VaultSection id="access" title="Access" {...sectionProps({ ...accessProps, body: null })}
          anchors={['access-to-secrets', ...(accessProps.anchors ?? []), ...tabAnchors]}>
          {intro && <p className="mb-3 text-[12px] text-muted-foreground">{intro}</p>}
          <VaultAccessTabs tabs={tabs} />
          {footer}
        </VaultSection>
        <VaultSection id="encryption-key" title="Encryption key" {...sectionProps(encryptionKey)}
          anchors={['vault-storage', ...(encryptionKey.anchors ?? [])]}>
          <p className="mb-3 text-[12px] text-muted-foreground" data-testid="vault-key-explainer">{KEY_EXPLAINER}</p>
          {encryptionKey.body}
        </VaultSection>
        {log && <VaultSection id="vault-log" title="Log" {...sectionProps(log)}>{log.body}</VaultSection>}
        {children}
      </div>
    </div>
  )
}

/** A part's section props: everything but the body, which is the section's children. */
function sectionProps(part: VaultPart) {
  const { summary, tone, anchors, defaultOpen } = part
  return { summary, tone, anchors, defaultOpen }
}
