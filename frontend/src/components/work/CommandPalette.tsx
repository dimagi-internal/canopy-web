// The command palette (⌘K / Ctrl+K, anywhere in the app): one search box over
// the places you go (the header's pages), the workspace's projects and agents,
// its tasks (searched as you type), and a few commands — create a task from
// what you typed, start a project, go to your tasks. A key-shaped query
// (DIM-142) jumps straight to that task. Arrow keys and Enter pick; Esc closes.
// Mounted once, in the app shell; it reads the active workspace from there.
import { useCallback, useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { useNavigate } from 'react-router-dom'
import { Bot, CornerDownLeft, FolderKanban, Hash, Plus, Search, SquareArrowOutUpRight } from 'lucide-react'
import { cn } from 'canopy-ui/lib'

import { createTask, listTasks, type Task } from '@/api/work'
import { ACCOUNT_ITEMS, HELP_ITEMS, type ResolvedNavEntry } from '@/components/AppLayout/nav'
import { scopedPath } from '@/lib/scopedLinks'

import { StatusIcon } from './bits'
import { displayAgent } from './meta'
import { useDirectory } from './useDirectory'

/** How long typing pauses before the task search runs. */
const SEARCH_DEBOUNCE_MS = 200

/** A task key as people type it: a prefix, a dash, a number (DIM-142). */
const KEY_SHAPE = /^[A-Za-z][A-Za-z0-9]{0,9}-\d+$/

type Group = 'Jump' | 'Commands' | 'Tasks' | 'Projects' | 'Agents' | 'Go to'

interface Item {
  id: string
  group: Group
  label: string
  hint?: string
  icon?: JSX.Element
  /** Always listed whatever the query (it is built from the query). */
  pinned?: boolean
  run: () => void | Promise<void>
}

/** "new task fix the login" → "fix the login"; anything else → itself. */
function titleFrom(query: string): string {
  return query.replace(/^\s*new task\b[:\s]*/i, '').trim()
}

function matches(item: Item, q: string): boolean {
  if (item.pinned || !q) return true
  const hay = `${item.label} ${item.hint ?? ''}`.toLowerCase()
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((w) => hay.includes(w))
}

export function CommandPalette({ workspace, nav }: { workspace: string; nav: ResolvedNavEntry[] }): JSX.Element | null {
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // ⌘K is a chord, not a letter, so it opens even from a text box.
      if ((e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((o) => !o)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (!open || !workspace) return null
  return <Palette workspace={workspace} nav={nav} onClose={() => setOpen(false)} />
}

function Palette({ workspace, nav, onClose }: { workspace: string; nav: ResolvedNavEntry[]; onClose: () => void }): JSX.Element {
  const navigate = useNavigate()
  const directory = useDirectory(workspace)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const [tasks, setTasks] = useState<Task[]>([])
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const restore = useRef<Element | null>(typeof document !== 'undefined' ? document.activeElement : null)

  useEffect(() => {
    inputRef.current?.focus()
    const back = restore.current
    return () => {
      if (back instanceof HTMLElement) back.focus()
    }
  }, [])

  // Tasks matching the query, debounced; the newest answer wins.
  const q = query.trim()
  useEffect(() => {
    if (!q || KEY_SHAPE.test(q)) {
      setTasks([])
      return
    }
    let cancelled = false
    const t = setTimeout(() => {
      listTasks({ q, limit: 20 }, workspace)
        .then((rows) => !cancelled && setTasks(rows))
        .catch(() => !cancelled && setTasks([]))
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(t)
    }
  }, [q, workspace])

  const go = useCallback(
    (to: string) => {
      onClose()
      navigate(to)
    },
    [navigate, onClose],
  )

  const items = useMemo((): Item[] => {
    const out: Item[] = []
    if (KEY_SHAPE.test(q)) {
      const key = q.toUpperCase()
      out.push({
        id: `jump:${key}`,
        group: 'Jump',
        label: `Open ${key}`,
        icon: <Hash aria-hidden="true" className="size-3.5" />,
        pinned: true,
        run: () => go(scopedPath(workspace, `/tasks/${key}`)),
      })
    }
    const title = titleFrom(q)
    out.push({
      id: 'cmd:new-task',
      group: 'Commands',
      label: title ? `New task “${title}”` : 'New task…',
      hint: 'create',
      icon: <Plus aria-hidden="true" className="size-3.5" />,
      pinned: Boolean(title) && !KEY_SHAPE.test(q),
      run: async () => {
        if (!title) return go(scopedPath(workspace, '/tasks?new=1'))
        try {
          const task = await createTask({ title }, workspace)
          go(scopedPath(task.workspace || workspace, `/tasks/${task.key}`))
        } catch (e) {
          setError(e instanceof Error ? e.message : 'Could not create the task')
        }
      },
    })
    out.push({
      id: 'cmd:new-project',
      group: 'Commands',
      label: 'New project…',
      hint: 'create',
      icon: <Plus aria-hidden="true" className="size-3.5" />,
      run: () => go(scopedPath(workspace, '/projects?new=1')),
    })
    out.push({
      id: 'cmd:my-tasks',
      group: 'Commands',
      label: 'Go to my tasks',
      icon: <SquareArrowOutUpRight aria-hidden="true" className="size-3.5" />,
      run: () => go(scopedPath(workspace, '/tasks?tab=mine')),
    })
    for (const t of tasks) {
      out.push({
        id: `task:${t.key}`,
        group: 'Tasks',
        label: t.title,
        hint: t.key,
        icon: <StatusIcon status={t.status} />,
        pinned: true, // the server already matched it
        run: () => go(scopedPath(t.workspace || workspace, `/tasks/${t.key}`)),
      })
    }
    for (const p of directory.projects) {
      out.push({
        id: `project:${p.address}`,
        group: 'Projects',
        label: p.name,
        hint: 'project',
        icon: <FolderKanban aria-hidden="true" className="size-3.5" />,
        run: () => go(p.url || scopedPath(workspace, `/projects/${p.handle}`)),
      })
    }
    for (const a of directory.agents) {
      out.push({
        id: `agent:${a.slug}`,
        group: 'Agents',
        label: a.name || displayAgent(a.slug),
        hint: 'agent',
        icon: <Bot aria-hidden="true" className="size-3.5" />,
        run: () => go(scopedPath(a.workspace || workspace, `/agents/${a.slug}`)),
      })
    }
    const pages = [
      ...nav.flatMap((e) => (e.kind === 'link' ? [e.item] : e.items)),
      ...ACCOUNT_ITEMS.map((i) => ({ href: i.path, label: i.label })),
      ...HELP_ITEMS.map((i) => ({ href: i.path, label: i.label })),
    ]
    const seen = new Set<string>()
    for (const p of pages) {
      if (seen.has(p.href)) continue
      seen.add(p.href)
      out.push({
        id: `page:${p.href}`,
        group: 'Go to',
        label: p.label,
        hint: 'page',
        icon: <CornerDownLeft aria-hidden="true" className="size-3.5" />,
        run: () => go(p.href),
      })
    }
    const ORDER: Group[] = ['Jump', 'Tasks', 'Go to', 'Projects', 'Agents', 'Commands']
    // With nothing typed, lead with the commands and the pages.
    const order: Group[] = q ? ORDER : ['Commands', 'Go to', 'Projects', 'Agents']
    return order.flatMap((g) => out.filter((i) => i.group === g && matches(i, q)))
  }, [q, tasks, directory, nav, workspace, go])

  const current = Math.min(active, Math.max(items.length - 1, 0))
  useEffect(() => setActive(0), [q])
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${current}"]`)?.scrollIntoView?.({ block: 'nearest' })
  }, [current])

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((i) => Math.min(i + 1, items.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      void items[current]?.run()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onClose()
    }
  }

  let lastGroup: Group | null = null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-background/70 px-4 pt-[12vh] backdrop-blur-[1px]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        data-testid="command-palette"
        onMouseDown={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-lg border border-border bg-card shadow-xl"
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search aria-hidden="true" className="size-4 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setError(null)
            }}
            onKeyDown={onKeyDown}
            role="combobox"
            aria-expanded="true"
            aria-controls="command-palette-list"
            aria-activedescendant={items[current] ? `cp-${current}` : undefined}
            aria-autocomplete="list"
            aria-label="Search or type a command"
            placeholder="Search tasks, projects, agents, pages — or type a new task’s title"
            className="h-11 min-w-0 flex-1 bg-transparent text-[14px] text-foreground outline-none placeholder:text-placeholder"
          />
          <kbd className="rounded border border-border px-1 font-mono text-[10.5px] text-muted-foreground">Esc</kbd>
        </div>
        {error && <p className="border-b border-border px-3 py-1.5 text-[12px] text-destructive">{error}</p>}
        <ul ref={listRef} id="command-palette-list" role="listbox" aria-label="Results" className="max-h-[60vh] overflow-y-auto py-1">
          {items.length === 0 && <li className="px-3 py-4 text-[13px] text-muted-foreground">Nothing matches.</li>}
          {items.map((item, i) => {
            const heading = item.group !== lastGroup ? item.group : null
            lastGroup = item.group
            return (
              <li key={item.id} role="presentation">
                {heading && (
                  <div role="presentation" className="px-3 pb-0.5 pt-2 text-[10.5px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {heading}
                  </div>
                )}
                <div
                  id={`cp-${i}`}
                  role="option"
                  aria-selected={i === current}
                  data-index={i}
                  onMouseMove={() => i !== current && setActive(i)}
                  onClick={() => void item.run()}
                  className={cn(
                    'mx-1 flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[13px]',
                    i === current ? 'bg-primary/10 text-foreground' : 'text-foreground-secondary',
                  )}
                >
                  <span className="flex w-4 shrink-0 justify-center text-muted-foreground">{item.icon}</span>
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  {item.hint && <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{item.hint}</span>}
                </div>
              </li>
            )
          })}
        </ul>
        <div className="flex gap-3 border-t border-border px-3 py-1.5 text-[11px] text-muted-foreground">
          <span>↑↓ move</span>
          <span>↵ open</span>
          <span>? all shortcuts</span>
        </div>
      </div>
    </div>
  )
}
