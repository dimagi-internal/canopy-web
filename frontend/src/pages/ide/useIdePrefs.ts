import { useEffect, useState } from 'react'

import { DEFAULT_FILTERS, type IdeFilters } from './ideTree'

export const PREFS_KEY = 'canopy.ide.v1'
// Rail width, dragged by its right edge; double-click the edge to reset.
export const RAIL_DEFAULT = 400
export const RAIL_MIN = 260
export const RAIL_MAX = 900
export const clampRail = (w: number) => Math.round(Math.min(RAIL_MAX, Math.max(RAIL_MIN, w)))

export type IdePrefs = {
  filters: Omit<IdeFilters, 'query'>
  collapsed: string[]
  /** Sessions whose links (the sessions started from them) are folded away. */
  collapsedLinks: string[]
  railWidth: number
  railHidden: boolean
}

/** The saved prefs, merged over the defaults so a pref added later (or a
 *  corrupt entry) never leaves a field undefined. */
export function loadPrefs(): IdePrefs {
  const fallback: IdePrefs = {
    filters: { ...DEFAULT_FILTERS },
    collapsed: [],
    collapsedLinks: [],
    railWidth: RAIL_DEFAULT,
    railHidden: false,
  }
  try {
    const raw = window.localStorage.getItem(PREFS_KEY)
    if (!raw) return fallback
    const p = JSON.parse(raw) as Partial<IdePrefs>
    return {
      filters: {
        ...DEFAULT_FILTERS,
        ...p.filters,
        states: { ...DEFAULT_FILTERS.states, ...p.filters?.states },
        modes: { ...DEFAULT_FILTERS.modes, ...p.filters?.modes },
      },
      collapsed: Array.isArray(p.collapsed) ? p.collapsed : [],
      collapsedLinks: Array.isArray(p.collapsedLinks) ? p.collapsedLinks : [],
      railWidth: typeof p.railWidth === 'number' ? clampRail(p.railWidth) : RAIL_DEFAULT,
      railHidden: p.railHidden === true,
    }
  } catch {
    return fallback
  }
}

function savePrefs(p: IdePrefs) {
  try {
    window.localStorage.setItem(PREFS_KEY, JSON.stringify(p))
  } catch {
    // storage disabled — prefs last for this tab only
  }
}

/** /ide's view prefs — filters (minus the search text), collapsed folders and
 *  links, rail width and visibility — read once from localStorage and written
 *  back on every change. */
export function useIdePrefs() {
  const [initial] = useState(loadPrefs)
  const [filters, setFilters] = useState<IdeFilters>({ ...initial.filters, query: '' })
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set(initial.collapsed))
  const [collapsedLinks, setCollapsedLinks] = useState<Set<string>>(() => new Set(initial.collapsedLinks))
  const [railWidth, setRailWidth] = useState(initial.railWidth)
  const [railHidden, setRailHidden] = useState(initial.railHidden)

  useEffect(() => {
    const { hiddenRunners, states, modes, archived, mineOnly } = filters
    savePrefs({
      filters: { hiddenRunners, states, modes, archived, mineOnly },
      collapsed: [...collapsed],
      collapsedLinks: [...collapsedLinks],
      railWidth,
      railHidden,
    })
  }, [filters, collapsed, collapsedLinks, railWidth, railHidden])

  return {
    filters,
    setFilters,
    collapsed,
    setCollapsed,
    collapsedLinks,
    setCollapsedLinks,
    railWidth,
    setRailWidth,
    railHidden,
    setRailHidden,
  }
}
