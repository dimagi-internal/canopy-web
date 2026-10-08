import { createContext, useContext } from 'react'

/** The huddle's priorities brief, for components deep in the page (a reply's
 * idea card) that say "Serves priority 2: …". '' for a huddle without one. */
export const BriefContext = createContext('')

export function useBrief(): string {
  return useContext(BriefContext)
}
