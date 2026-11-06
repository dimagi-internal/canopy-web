/**
 * A dynamic view's side of canopy's panel: the SDK's page calls (`setPageState`,
 * `registerAction`, `ask`), for a view drawn in a sandbox with no network.
 *
 * canopy's own panel (`widget/CanopyWidget.tsx`) already reads what a canopy page
 * shows (`pageState.ts`) and offers the agent what it can do (`pageActions.ts`). A
 * view is AI-written code in an opaque-origin frame, so it cannot register there
 * itself: it asks over the frame protocol, and this registers on its behalf, for
 * as long as the view is on screen.
 *
 * What an action can do is what the view can do: it runs in the view's frame, as the
 * viewer, with the view's own calls (reads, its declared agent tasks, its connected
 * account — where a write still needs a real click). So the agent cannot make a view
 * do anything its person could not make it do; a view keeps a step the person must
 * take themselves (sending, applying changes) as a button and does not offer it.
 *
 * Only on a view's own page (`/w/<ws>/views/<slug>`): a project page draws several
 * views, and the page state is one per page.
 */
import { registerPageAction } from '@/widget/pageActions'
import { setPageStateContributor, notifyPageStateChanged } from '@/widget/pageState'

/** How long the agent waits for the view to answer an action. */
export const ACTION_TIMEOUT_MS = 30_000
/** Enough for a screen of rows; a view that needs more should describe, not dump. */
export const MAX_STATE_BYTES = 64 * 1024
export const MAX_ACTIONS = 20
const NAME = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/

export interface ViewIdentity {
  slug: string
  title: string
  workspace: string
}

interface Spec {
  name: string
  description?: string
  parameters?: Record<string, unknown>
}

function plain(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v)
}

/** A copy that is JSON and nothing else, or a refusal. */
function jsonCopy(v: unknown, what: string, cap = MAX_STATE_BYTES): { ok: true; value: unknown } | { ok: false; error: string } {
  let text: string
  try {
    text = JSON.stringify(v ?? null)
  } catch {
    return { ok: false, error: `${what} must be plain JSON` }
  }
  if (text.length > cap) return { ok: false, error: `${what} is ${text.length} bytes; the limit is ${cap}` }
  return { ok: true, value: JSON.parse(text) }
}

export function checkSpec(raw: unknown): Spec | string {
  if (!plain(raw)) return 'an action needs {name, description, parameters}'
  const name = raw.name
  if (typeof name !== 'string' || !NAME.test(name)) return 'an action name is a letter then up to 63 letters, digits, _ . or -'
  const description = raw.description
  if (description !== undefined && (typeof description !== 'string' || description.length > 500)) {
    return 'an action description is text of at most 500 characters'
  }
  const parameters = raw.parameters
  if (parameters !== undefined && !plain(parameters)) return 'an action\'s parameters are a JSON-Schema object'
  const params = parameters === undefined ? undefined : jsonCopy(parameters, 'parameters', 8 * 1024)
  if (params && !params.ok) return params.error
  return {
    name,
    ...(description ? { description } : {}),
    ...(params ? { parameters: params.value as Record<string, unknown> } : {}),
  }
}

export class ViewPagePanel {
  private state: Record<string, unknown> | null = null
  private disposeState: (() => void) | null = null
  private actions = new Map<string, () => void>()
  private waiting = new Map<number, { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> }>()
  private nextId = 1

  private readonly send: (id: number, name: string, args: Record<string, unknown>) => void
  private readonly view: () => ViewIdentity
  private readonly timeoutMs: number

  constructor(
    /** Ask the frame to run one of its actions. */
    send: (id: number, name: string, args: Record<string, unknown>) => void,
    view: () => ViewIdentity,
    timeoutMs = ACTION_TIMEOUT_MS,
  ) {
    this.send = send
    this.view = view
    this.timeoutMs = timeoutMs
  }

  /** The view's `canopy.page.setState(state)`. Returns a refusal, or null. */
  setState(raw: unknown): string | null {
    if (!plain(raw)) return 'page state is an object'
    const copy = jsonCopy(raw, 'page state')
    if (!copy.ok) return copy.error
    this.state = copy.value as Record<string, unknown>
    if (!this.disposeState) {
      this.disposeState = setPageStateContributor(() => {
        const v = this.view()
        return { dynamic_view: { slug: v.slug, title: v.title, workspace: v.workspace }, ...(this.state ?? {}) }
      })
    } else {
      notifyPageStateChanged()
    }
    return null
  }

  /** The view's `canopy.page.registerAction(name, run, options)`. */
  register(raw: unknown): string | null {
    const spec = checkSpec(raw)
    if (typeof spec === 'string') return spec
    if (!this.actions.has(spec.name) && this.actions.size >= MAX_ACTIONS) return `a view offers at most ${MAX_ACTIONS} actions`
    this.actions.get(spec.name)?.()
    this.actions.set(spec.name, registerPageAction(spec, (args) => this.call(spec.name, args)))
    return null
  }

  unregister(name: unknown) {
    if (typeof name !== 'string') return
    this.actions.get(name)?.()
    this.actions.delete(name)
  }

  /** The frame's answer to `send`. */
  answer(id: number, ok: boolean, data: unknown, error: string) {
    const w = this.waiting.get(id)
    if (!w) return
    this.waiting.delete(id)
    clearTimeout(w.timer)
    if (ok) w.resolve(data)
    else w.reject(new Error(error || 'the view refused'))
  }

  private call(name: string, args: Record<string, unknown>): Promise<unknown> {
    const copy = jsonCopy(plain(args) ? args : {}, 'arguments')
    if (!copy.ok) return Promise.reject(new Error(copy.error))
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiting.delete(id)
        reject(new Error(`the view did not answer "${name}" within ${Math.round(this.timeoutMs / 1000)}s`))
      }, this.timeoutMs)
      this.waiting.set(id, { resolve, reject, timer })
      this.send(id, name, copy.value as Record<string, unknown>)
    })
  }

  /** The view left (or its frame did): withdraw everything, refuse what is waiting. */
  dispose() {
    for (const off of this.actions.values()) off()
    this.actions.clear()
    this.disposeState?.()
    this.disposeState = null
    this.state = null
    for (const w of this.waiting.values()) {
      clearTimeout(w.timer)
      w.reject(new Error('the view is no longer on screen'))
    }
    this.waiting.clear()
  }
}
