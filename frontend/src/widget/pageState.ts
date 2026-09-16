/**
 * The page's side of the state channel.
 *
 * `pageActions` says what the page can DO; this says what it currently SHOWS.
 * They are the two halves AG-UI splits a frontend into — frontend tools and
 * shared state — and this is the half canopy did not have.
 *
 * **Why this replaces `pageContext`'s snapshot.** `buildPageContext` was read
 * once, when the frame initialised, and pasted onto the first message as prose.
 * A user filters the page *after* opening the chat at least as often as before,
 * so the one reading we took was routinely of a view that no longer existed,
 * and nothing updated it for the rest of the conversation. State is pushed on
 * every change instead, exactly like the action registry, so the agent's view
 * of the screen cannot silently drift from the screen.
 *
 * A module-level registry for the same reason the action one is: the consumer
 * is not a React component. The widget mounts its own DOM outside the tree.
 */

/** What a page declares. Deliberately open — canopy does not know what any
 *  given host's page means — but see `describeSelection` for the shape that
 *  actually works, and `MAX_STATE_BYTES` on the server for the bound that
 *  stops a page sending its rows instead of its selection. */
export type PageState = Record<string, unknown>

type Contributor = () => PageState

let contributor: Contributor | null = null
const listeners = new Set<(state: PageState) => void>()

/** Assemble the current state, never throwing.
 *
 *  A contributor that throws must cost only its own contribution: the route
 *  layer beneath it is still true, and an agent with the path and no selection
 *  is far better off than an agent with nothing.
 */
export function currentPageState(base: PageState = {}): PageState {
  if (!contributor) return { ...base }
  try {
    return { ...base, ...contributor() }
  } catch {
    return { ...base }
  }
}

/** Whether any page has declared a view.
 *
 *  Distinct from `currentPageState()` returning `{}`: a page that declares
 *  nothing must not cause the widget to announce the user's screen as empty,
 *  which would overwrite a perfectly good declaration with an assertion that
 *  there is nothing to see.
 */
export function hasPageState(): boolean {
  return contributor !== null
}

/** Register the page's contributor. Last wins; returns a disposer. */
export function setPageStateContributor(fn: Contributor): () => void {
  contributor = fn
  notifyPageStateChanged()
  return () => {
    // Only if it is still OURS: React can run an old component's cleanup after
    // a new one registered, and clearing unconditionally would blank the state
    // of the page the user is actually on.
    if (contributor === fn) {
      contributor = null
      notifyPageStateChanged()
    }
  }
}

/** Tell the widget the view moved. Called by `usePageState` on every change. */
export function notifyPageStateChanged(): void {
  const state = currentPageState()
  listeners.forEach((l) => l(state))
}

export function onPageStateChanged(fn: (state: PageState) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

/**
 * The shape that actually works, as a named helper rather than a doc comment.
 *
 * The rule it encodes: **send the selection, not the data.** `ids` says which
 * rows are on screen; `backingTool` says which MCP tool resolves them. The
 * agent then reads the rows through that tool, live, with the user's own
 * permissions applied — instead of trusting a copy the page serialised, which
 * can go stale between render and send and is a second place an ACL could be
 * got wrong.
 *
 * This is the same rule contacts follow: the local copy is a cache, never the
 * authority.
 */
export function describeSelection(input: {
  /** The MCP tool that resolves these ids. */
  backingTool: string
  /** The MCP resource URI this page is showing, e.g. `insight://`.
   *
   *  This is what canopy keys INVALIDATION on: when the data behind it changes
   *  — by any actor, through any door — every page declaring this resource is
   *  told to re-read. A page that omits it still describes itself to the agent
   *  but will never be told its data moved, which is a worse page rather than a
   *  broken one. */
  resource?: string
  /** What is on screen, in display order. */
  ids: Array<string | number>
  /** The filters producing that selection, if they are not already in the URL. */
  filters?: Record<string, unknown>
  /** Anything else about the view that is not derivable from the URL. */
  extra?: Record<string, unknown>
}): PageState {
  const { backingTool, resource, ids, filters, extra } = input
  return {
    backing_tool: backingTool,
    ...(resource ? { resource } : {}),
    visible_ids: ids,
    visible_count: ids.length,
    ...(filters && Object.keys(filters).length ? { filters } : {}),
    ...extra,
  }
}
