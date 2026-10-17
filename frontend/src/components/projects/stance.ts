import type { BackingStance } from '@/api/agents'

/** A backing's stance in plain words — "Eva backs with changes: …". */
export const STANCE_WORDS: Record<BackingStance, string> = {
  backs: 'backs',
  backs_with_changes: 'backs with changes',
  passes: 'passes',
}
