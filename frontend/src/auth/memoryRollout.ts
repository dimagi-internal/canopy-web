import { useAuth } from './AuthProvider'

/** Memory (HCP) is on only for the people `settings.HCP_PEOPLE` names, for now
 *  (Jonathan, 2026-10-11: "only for me right now"). Everyone else sees no memory
 *  surface at all — unless canopy already holds some of theirs (`memory_paused`),
 *  in which case "Your memory" stays reachable so they can download or delete it.
 *  The server enforces the gate; this only decides what is shown. */
export function useMemoryVisible(): boolean {
  const { user } = useAuth()
  return Boolean(user?.memory_enabled || user?.memory_paused)
}
