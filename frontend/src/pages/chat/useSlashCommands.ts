import { useEffect, useState } from 'react'

import { listSlashCommands, type SlashCommand } from '@/api/chat'

/** What the composer's `/` menu offers in this session: the agent's launchable
 *  skills, and what the session's runner reported it can run. Fetched per
 *  session, and again when `runnerKey` changes — a new chat has no runner until
 *  its first send is claimed, so its first list holds only the agent's own
 *  commands. A failed fetch leaves `/` as plain text. */
export function useSlashCommands(sessionId: string, runnerKey: string): SlashCommand[] {
  const [commands, setCommands] = useState<SlashCommand[]>([])
  useEffect(() => {
    if (!sessionId) return
    let live = true
    listSlashCommands(sessionId)
      .then((rows) => { if (live) setCommands(rows) })
      .catch(() => { if (live) setCommands([]) })
    return () => { live = false }
  }, [sessionId, runnerKey])
  return commands
}
