import { useEffect, useState } from 'react'

import { listSlashCommands, type ChatSessionDetail, type SlashCommand } from '@/api/chat'

// The list is the agent's launchable skills plus what the session's runner reported
// (`slash_commands.for_session`), empty for someone who cannot send — so it is the
// same for every chat with one agent, runner and role. Kept for the life of the tab,
// as the request itself so a chat's own reloads share one read: it runs to hundreds
// of rows (~120 KB), and a chat opened from /ide read it up to three times per click.
const byTarget = new Map<string, Promise<SlashCommand[]>>()

/** What the composer's `/` menu offers in this session: the agent's launchable
 *  skills, and what the session's runner reported it can run. Read once the
 *  session's detail is in (so it never competes with the transcript), and again
 *  when `runnerKey` changes — a new chat has no runner until its first send is
 *  claimed, so its first list holds only the agent's own commands. A chat bound to
 *  a runner shares the list of every chat with its agent, runner and role. A
 *  failed fetch leaves `/` as plain text. */
export function useSlashCommands(
  sessionId: string,
  meta: ChatSessionDetail | null,
  runnerKey: string,
): SlashCommand[] {
  const target = meta?.runner_name ? `${meta.agent_slug ?? ''}|${meta.runner_name}|${meta.my_role ?? ''}` : null
  // An unbound chat's list can change on its first claim, so it is keyed to the chat.
  const key = target ?? `${sessionId}|${runnerKey}`
  const [commands, setCommands] = useState<SlashCommand[]>([])
  const loaded = meta !== null
  useEffect(() => {
    if (!sessionId || !loaded) return
    let read = target ? byTarget.get(target) : undefined
    if (!read) {
      read = listSlashCommands(sessionId)
      if (target) {
        const t = target
        byTarget.set(t, read)
        // A failed read is not kept: the next chat asks again.
        read.catch(() => byTarget.delete(t))
      }
    }
    let live = true
    read
      .then((rows) => { if (live) setCommands(rows) })
      .catch(() => { if (live) setCommands([]) })
    return () => { live = false }
  }, [sessionId, key, target, loaded])
  return commands
}
