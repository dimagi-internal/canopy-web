// The work screens' keyboard: one test for "is the person typing?" (a single
// key never fires then), and the one list of shortcuts the `?` help shows, so
// the help cannot drift from what the keys do.

/** Is focus somewhere a key is text — an input, a textarea, a select, or
 *  anything contenteditable? A plain-letter shortcut must not fire there. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || !(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.closest('[contenteditable="true"]') !== null
}

/** A key pressed on its own (shift allowed, for `?`): no ⌘, Ctrl or Alt. */
export function isPlainKey(e: KeyboardEvent): boolean {
  return !e.metaKey && !e.ctrlKey && !e.altKey
}

export interface Shortcut {
  keys: string[]
  what: string
}

export interface ShortcutGroup {
  title: string
  shortcuts: Shortcut[]
}

export const SHORTCUTS: ShortcutGroup[] = [
  {
    title: 'Anywhere',
    shortcuts: [
      { keys: ['⌘', 'K'], what: 'Search and jump: tasks, projects, agents, pages; create a task' },
      { keys: ['?'], what: 'This list' },
      { keys: ['Esc'], what: 'Close the open panel or dialog' },
    ],
  },
  {
    title: 'Task lists',
    shortcuts: [
      { keys: ['C'], what: 'Create a task' },
      { keys: ['J'], what: 'Next row' },
      { keys: ['K'], what: 'Previous row' },
      { keys: ['X'], what: 'Select the row (for a change to many)' },
      { keys: ['Space'], what: 'Open the task beside the list' },
    ],
  },
  {
    title: 'Triage',
    shortcuts: [
      { keys: ['A'], what: 'Accept into Todo' },
      { keys: ['S'], what: 'Start now (its agent starts on it)' },
      { keys: ['B'], what: 'Backlog: accepted, not now' },
      { keys: ['D'], what: 'Decline' },
      { keys: ['H'], what: 'Snooze a week' },
      { keys: ['M'], what: 'Mark a duplicate of another task' },
      { keys: ['Enter'], what: 'Open the task beside the list' },
    ],
  },
]
