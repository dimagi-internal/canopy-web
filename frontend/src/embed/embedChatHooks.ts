import { appResourceUpdate, createAppResourceUpdates } from 'canopy-ui/chat'
import { type CanopyClient } from 'canopy-client'
import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from 'react'
import { type JoinRequest } from '@/api/chat'
import { addPerson, type PeopleApi } from '@/components/chat/ChatPeople'
import { joinRequestsFrame, type JoinRequestActions } from '@/components/chat/JoinRequests'
import { type SessionAudience } from '@/components/SessionAudienceLine'
import { type SealedKey } from '@/vault/sealedChat'
import type { HostLink } from './hostLink'
import { type PageGrant } from './pageGrant'
import { declarePage, sessionBase } from './session'

// Pieces of `EmbedChat`, one hook per concern. Each is called where the inline
// code used to sit, so the component's hook order is unchanged.

export function usePageGrantActions({ pageGrant, client, link, isContact, sessionId }: {
  pageGrant: PageGrant | undefined
  client: CanopyClient
  link: HostLink
  isContact: boolean
  sessionId: string
}) {
  /** The person acted in this conversation: make sure this tab holds a grant, and
   *  when this is what asked for one, tell canopy the page acted here (a page
   *  declaration stamps the conversation with the new token's grant) — before the
   *  turn this send starts, so the turn reads through it. A send never waits for
   *  the passkey: canopy pushes `host_grant.requested` if the turn needs it. */
  const withGrant = useCallback(async () => {
    if (!pageGrant) return
    if (await pageGrant.ensure()) {
      await declarePage(client, link, sessionBase(isContact, sessionId))
    }
  }, [pageGrant, client, link, isContact, sessionId])
  /** A live card needs the grant now: ask for the passkey and wait for the
   *  person's answer (confirmed or declined) before it tries again. */
  const grantForCard = useCallback(async () => {
    if (!pageGrant) return
    await withGrant()
    pageGrant.need()
    await pageGrant.settled()
  }, [pageGrant, withGrant])
  return { withGrant, grantForCard }
}

export function usePanelFrames({ client, link, sessionId, isContact, pageGrant }: {
  client: CanopyClient
  link: HostLink
  sessionId: string
  isContact: boolean
  pageGrant: PageGrant | undefined
}) {
  // The agent asking this page to do something. Arrives on the session socket
  // as a DOORBELL only — the durable PageAction row is the mechanism, and the
  // result goes back over HTTP so it is an acknowledged write rather than a
  // second frame that could land nowhere.
  // The host's `resources/updated` for this panel's Views, relayed on the socket.
  const appUpdates = useMemo(() => createAppResourceUpdates(), [])
  // Who is asking to join this private conversation, as the socket last said.
  const [liveJoinRequests, setLiveJoinRequests] = useState<JoinRequest[] | null>(null)
  const onUnknownEvent = useCallback(
    (frame: { event: string; data?: unknown }) => {
      // A resource a View in this panel subscribed to changed at its host: the
      // View is told, and re-reads it as the visitor.
      const update = appResourceUpdate(frame)
      if (update) {
        appUpdates.emit(update.toolCallId, update.uri)
        return
      }
      const asking = joinRequestsFrame(frame)
      if (asking) {
        setLiveJoinRequests(asking)
        return
      }
      if (frame.event === 'host_grant.requested') {
        // The agent's turn needs this tab's act-as-you permission: re-read it
        // (now `requested`, with the agent's reason) and the prompt opens.
        void pageGrant?.refresh()
        return
      }
      if (frame.event === 'page.invalidate') {
        // Relayed straight out to the host page, which owns what "re-read"
        // means. The kit deliberately does not know this frame — it is canopy's
        // vocabulary, which is exactly what `onUnknownEvent` is for.
        const data = frame.data as { uri?: string } | undefined
        link.invalidate(String(data?.uri ?? ''))
        return
      }
      if (frame.event !== 'session.page_action') return
      const action = frame.data as { id: string; name: string; args: Record<string, unknown> }
      void (async () => {
        let body: { result?: unknown; error?: string }
        try {
          body = { result: await link.runAction(action.name, action.args) }
        } catch (error) {
          // A host refusal must reach the agent AS a refusal. Swallowing it
          // here would leave the action pending until it timed out, and the
          // agent would be told the page was closed when in fact it said no.
          body = { error: error instanceof Error ? error.message : 'the page refused the action' }
        }
        await client.rest
          .json(`${sessionBase(isContact, sessionId)}/page-actions/${action.id}/result`, {
            method: 'POST',
            body: JSON.stringify(body),
          })
          .catch(() => undefined)
      })()
    },
    [client, link, sessionId, isContact, appUpdates, pageGrant],
  )
  return { appUpdates, liveJoinRequests, onUnknownEvent }
}

export function useSessionDetail({ client, sessionId, isContact, link, sealedRows, setSensitive }: {
  client: CanopyClient
  sessionId: string
  isContact: boolean
  link: HostLink
  sealedRows: boolean
  setSensitive: Dispatch<SetStateAction<boolean | null>>
}) {
  // A sensitive conversation arrives sealed to its people's vault keys: the panel
  // opens it here, in the frame, with the visitor's own vault key — unlocked from the
  // keyset the conversation carries (`sealed_key.keyset`), since the frame has no
  // browser session for the vault's own routes. A contact holds no key, ever.
  const [sealedKey, setSealedKey] = useState<SealedKey | null>(null)
  // Its owner answers a request to join (`can_share`); its other people see it.
  const [myRole, setMyRole] = useState<string | null>(null)
  // Who can see this conversation (`audience`, the same pieces as the session ACL).
  const [audience, setAudience] = useState<SessionAudience | null>(null)
  useEffect(() => {
    if (isContact) return
    let live = true
    void client.rest.json<{
      sealed_key?: SealedKey | null; sensitive?: boolean; my_role?: string | null; origin_resource?: string
      audience?: SessionAudience | null
    }>(
      `/api/canopy-sessions/${encodeURIComponent(sessionId)}`)
      .then((d) => {
        if (!live) return
        // The page around the panel learns which conversation is open and where it
        // began — canopy's record of it, not the page's — and nothing it said.
        link.reportSession({
          id: sessionId, resource: d.origin_resource ?? '', private: Boolean(d.sensitive || d.sealed_key),
        })
        setSealedKey(d.sealed_key ?? null)
        setMyRole(d.my_role ?? null)
        setAudience(d.audience ?? null)
        setSensitive(Boolean(d.sensitive || d.sealed_key))
      })
      .catch(() => live && setSensitive((v) => v ?? false))
    return () => {
      live = false
    }
  }, [client, sessionId, isContact, sealedRows, link, setSensitive])
  // A contact's conversation is never private (a contact holds no key); its report
  // comes from the contact door.
  useEffect(() => {
    if (!isContact) return
    let live = true
    void client.rest.json<{ origin_resource?: string }>(sessionBase(true, sessionId))
      .then((d) => live && link.reportSession({ id: sessionId, resource: d.origin_resource ?? '', private: false }))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [client, sessionId, isContact, link])
  return { sealedKey, myRole, audience }
}

export function useJoinActions({ client, sessionId, unlockVault, confirmPerson }: {
  client: CanopyClient
  sessionId: string
  unlockVault: Parameters<typeof addPerson>[3]
  confirmPerson: Parameters<typeof addPerson>[5]
}) {
  // Someone asking to join: the panel adds them itself — the same re-wrap canopy's
  // chat page does (unlock from the keyset, open the conversation key, wrap it to
  // them, `POST participants`), through the frame's own token.
  return useMemo<JoinRequestActions>(() => {
    const base = `/api/canopy-sessions/${encodeURIComponent(sessionId)}`
    const people: PeopleApi = {
      getSession: (id) => client.rest.json(`/api/canopy-sessions/${encodeURIComponent(id)}`),
      participantRecipient: (id, email) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/participants/recipient?email=${encodeURIComponent(email)}`),
      addParticipant: (id, email, role, wrap) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/participants`,
        { method: 'POST', body: JSON.stringify(wrap ? { email, role, wrap } : { email, role }) }),
      giveKeyCopy: (id, userId, wrap) => client.rest.json(
        `/api/canopy-sessions/${encodeURIComponent(id)}/key-copies`,
        { method: 'POST', body: JSON.stringify({ user_id: userId, wrap }) }),
    }
    return {
      list: () => client.rest.json<JoinRequest[]>(`${base}/join-requests`),
      add: (email) => addPerson(sessionId, email, 'editor', unlockVault, people, confirmPerson),
      decline: (requestId) => client.rest.json(
        `/api/canopy-sessions/join-requests/${encodeURIComponent(requestId)}/decline`, { method: 'POST' }),
    }
  }, [client, sessionId, unlockVault, confirmPerson])
}

export function usePageDeclarations(client: CanopyClient, link: HostLink, sessionId: string) {
  // Tell canopy what this page can do, so the agent's tool list includes it.
  // Re-sent whenever the host's set changes — a page the user navigated to
  // offers different things, and a stale declaration is one the agent would
  // call into nothing.
  useEffect(() => {
    // The pre-send declaration already covered the opening message; this keeps
    // it true as the user navigates mid-conversation. Both paths go through
    // `declarePage`, so there is one description of the page rather than two
    // that can disagree.
    return link.onActionsChanged(() => void declarePage(client, link, sessionId))
  }, [client, link, sessionId])

  // Tell canopy what this page is SHOWING, and keep telling it.
  //
  // The other half of the page contract, and the one that was missing. Context
  // used to be read once when the frame initialised and pasted onto the first
  // message as prose, so a user who filtered the page after opening the chat
  // left the agent acting on a screen that no longer existed. Here the view is
  // pushed on every change and the agent re-reads it with `page_state`, so
  // "close the ones I'm looking at" is answerable on turn nine, not only turn
  // one.
  useEffect(() => {
    return link.onPageStateChanged(() => void declarePage(client, link, sessionId))
  }, [client, link, sessionId])
}

export function useMenuAnswer(client: CanopyClient, sessionId: string) {
  // Answering the agent's dialog. Posted with the frame's own bearer token
  // rather than through `@/api/chat`, which authenticates with the app's
  // session cookie — the frame has none for this origin.
  const [answering, setAnswering] = useState(false)
  const [answerError, setAnswerError] = useState<string | null>(null)
  const onAnswerMenu = useCallback(
    (option: number | null, selections?: number[][] | null, texts?: (string | null)[] | null) => {
      setAnswering(true)
      setAnswerError(null)
      void client.rest
        .json<{ ok: boolean; reason: string }>(
          `/api/canopy-sessions/${encodeURIComponent(sessionId)}/answer-menu`,
          {
            method: 'POST',
            body: JSON.stringify({ option, selections, texts }),
          },
        )
        .then((r) => {
          // `ok:true` only means the tap was RELAYED. The runner's refusal
          // arrives later, on the menu itself (`answer_error`), which is why
          // this does not clear the dialog.
          if (!r.ok) setAnswerError(r.reason || 'the runner refused that answer')
        })
        .catch((e: unknown) =>
          setAnswerError(e instanceof Error ? e.message : 'could not send that answer'),
        )
        .finally(() => setAnswering(false))
    },
    [client, sessionId],
  )
  return { answering, answerError, onAnswerMenu }
}
