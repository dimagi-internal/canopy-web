import { useCallback, useEffect, useState, type JSX } from 'react'
import { useOutletContext } from 'react-router-dom'
import { listWaitingTasks, type AgentTaskOut } from '@/api/agents'
import { listItems, type ItemOut } from '@/api/items'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { ITEM_BAND, ITEM_KIND_RANK, type ItemKind } from '@/lib/itemBands'
import { ItemCard } from '@/components/items/ItemCard'
import { describeSelection } from '@/widget/pageState'
import { usePageState } from '@/widget/usePageState'
import { useResource } from '@/widget/useResource'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

/** Whole days since `iso`. `NaN`-safe: an unparseable timestamp yields 0 rather
 *  than poisoning a Math.max over the whole list. */
function ageInDays(iso: string): number {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return 0
  return Math.floor((Date.now() - then) / 86_400_000)
}

// One agent's inbox: its OPEN items, ranked Review -> Question, each decidable in
// place. This is the per-agent counterpart of /supervisor's fleet ItemInbox; both
// render the same actionable ItemCard and share band identity (lib/itemBands), so
// they can't drift. The inbox is a pure query over items now — no projections.

// The "N waiting on you" badge — mirrors the board's "N queued".
function WaitingBadge({ count }: { count: number }): JSX.Element {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded border border-primary/30 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary"
      title="Items waiting on you to act"
    >
      <span className="h-1.5 w-1.5 rounded-full bg-primary" />
      {count} waiting on you
    </span>
  )
}

function Band({
  kind,
  items,
  reload,
}: {
  kind: ItemKind
  items: ItemOut[]
  reload: () => void
}): JSX.Element | null {
  const mine = items.filter((i) => i.kind === kind)
  if (mine.length === 0) return null
  const { label, blurb, dot } = ITEM_BAND[kind]
  return (
    <section data-testid={`inbox-band-${kind}`}>
      <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5">
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        <span className="text-[11px] font-bold uppercase tracking-[0.06em] text-foreground">{label}</span>
        <span className="text-[11px] text-muted-foreground">{blurb}</span>
        <span className="ml-auto text-[11px] text-muted-foreground">{mine.length}</span>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {mine.map((item) => (
          <ItemCard key={item.id} item={item} onActed={reload} />
        ))}
      </div>
    </section>
  )
}

// Tasks parked on you: no buttons, because there is no decision to make — the
// next step is yours to take, on the board or off it. The card says which
// project it belongs to, since "why am I being asked" is usually that.
function ParkedBand({ tasks }: { tasks: AgentTaskOut[] }): JSX.Element {
  return (
    <section data-testid="inbox-band-waiting">
      <div className="mb-2 flex items-center gap-2 border-b border-border pb-1.5">
        <span className="h-1.5 w-1.5 rounded-full bg-primary" />
        <span className="text-[11px] font-bold uppercase tracking-[0.06em] text-foreground">
          Waiting on you
        </span>
        <span className="text-[11px] text-muted-foreground">
          the next step is yours
        </span>
        <span className="ml-auto text-[11px] text-muted-foreground">{tasks.length}</span>
      </div>
      <ul className="space-y-2">
        {tasks.map((task) => (
          <li
            key={task.id}
            data-testid={`waiting-${task.ext_id}`}
            className="bg-card border border-border rounded-lg px-3 py-2"
          >
            <div className="flex items-baseline gap-2">
              <span className="text-[11px] text-muted-foreground shrink-0">{task.ext_id}</span>
              <span className="text-[13px] text-foreground truncate">{task.title}</span>
              {task.project_name && (
                <span className="ml-auto text-[11px] text-muted-foreground shrink-0">
                  {task.project_name}
                </span>
              )}
            </div>
            {task.next_action && (
              <p className="mt-0.5 text-[12px] text-foreground-secondary">{task.next_action}</p>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * The agent's inbox: "what does this agent need from me right now?" in one scan,
 * decidable inline. Consumes GET /api/agents/{slug}/items/?state=open.
 */
export function InboxSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [items, setItems] = useState<ItemOut[] | null>(null)
  const [waiting, setWaiting] = useState<AgentTaskOut[]>([])

  const reload = useCallback(() => {
    void listItems(agent.slug, { state: 'open' })
      .then(setItems)
      .catch(() => setItems([]))
    // Tasks parked on YOU. Most of what a board holds is this rather than a
    // formal ask — "waiting on Andrea for the numbers" is a wait even though
    // nothing is being asked — and it reached no inbox at all until tasks
    // learned to name a person.
    void listWaitingTasks(agent.slug)
      .then(setWaiting)
      .catch(() => setWaiting([]))
  }, [agent.slug])

  useEffect(() => {
    setItems(null)
    reload()
  }, [agent.slug, reload])

  // An ask that also names a person appears once, as the ask: it is the same
  // wait, and showing it twice would make the badge lie.
  const askIds = new Set((items ?? []).map((i) => i.id))
  const parked = waiting.filter((t) => !t.ask_kind || !askIds.has(t.uuid ?? ''))
  const count = (items?.length ?? 0) + parked.length

  // Re-read when canopy says the item collection moved — whoever moved it. An
  // inbox is the surface most likely to change under you: the fleet raises items
  // while you are reading it, a schedule nag arrives, a colleague decides one.
  useResource('item://', reload)

  // Tell the embedded agent WHICH items are on screen, not what they say.
  //
  // This sent the rows themselves until 2026-09-18 — id, kind, title, age — for
  // the honest reason that no tool could read an item back, so stripping them
  // would have left the agent unable to discuss the inbox at all. `list_items`
  // closed that hole, and the rows went with it: a serialised copy goes stale
  // between render and send, and is a second place the tenant gate has to be
  // right. The agent resolves these ids through `list_items`, live, under the
  // user's own permissions.
  //
  // The two aggregates stay, because they are not row data — they are claims
  // about the SET, and "this inbox has gone stale" is exactly the question the
  // URL cannot express. Ages in days rather than timestamps because staleness is
  // what is being asked, and a date makes the agent do arithmetic first.
  usePageState(
    () =>
      describeSelection({
        backingTool: 'list_items',
        resource: 'item://',
        ids: (items ?? []).map((i) => i.id),
        filters: { agent: agent.slug, state: 'open' },
        extra: {
          oldest_open_item_age_days: items?.length
            ? Math.max(...items.map((i) => ageInDays(i.created_at)))
            : null,
        },
      }),
    [items, agent.slug],
  )

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader
        title="Inbox"
        action={count > 0 ? <WaitingBadge count={count} /> : undefined}
      />
      {items === null ? (
        <WorkbenchSkeleton />
      ) : count === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          Nothing needs you right now — {agent.name} has the ball.
        </p>
      ) : (
        <div className="space-y-7">
          {ITEM_KIND_RANK.map((kind) => (
            <Band key={kind} kind={kind} items={items} reload={reload} />
          ))}
          {parked.length > 0 && <ParkedBand tasks={parked} />}
        </div>
      )}
    </div>
  )
}
