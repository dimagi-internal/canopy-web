import { useEffect, useState } from 'react'

import { addMySessionReviewer, listMySessionReviewers, removeMySessionReviewer } from '@/api/people'
import type { SessionReviewers as SessionReviewersData } from '@/api/people'

/** The one coach the detailed consent below was checked against. Its weekly limit,
 *  address, record and replies are that agent's own behaviour, so another coach
 *  (`COACHING_AGENT_SLUG`) gets only what canopy-web itself guarantees until its
 *  copy has been checked the same way. */
const CHECKED_COACH = 'trellis'

type CoachNames = { agent: string; name: string }

/** The words every coaching opt-in uses, so the first-run checkbox and this
 *  switch promise the same thing. */
export function coachLabel({ agent, name }: CoachNames): string {
  if (agent !== CHECKED_COACH) {
    return `Have ${name} coach me: it reads my canopy sessions, read-only, and my computer uploads them in full so it can. The people who run ${name} can read its working logs. Turn off any time.`
  }
  return `Have ${name} coach me: it reads my canopy sessions and emails me short feedback about them. The people who run ${name} can read its working logs, and members of its workspace can read its mail, including notes to me. Turn off any time.`
}

// Each paragraph is a claim about code. What it rests on, so a change to one
// changes the other:
// - reads: apps/canopy_sessions/access.py (`_review_grant_q`, `sessions_of_q`), behind the
//   tenant gate on the agent login's memberships (`_visible_slugs`, audience.pieces).
// - upload: apps/canopy_sessions/coaching.py, apps/harness/api_session_sync.py::list_streams
//   (active sessions are listed for everyone; no workspace filter on the coached set).
// - no pull: access.may_pull.
// - logs: apps/harness/turn_access.py; the turn's session: access.visible_session_q legs 3, 4, 6.
// - mail: apps/mail/api.py (`@gate("member")` on threads; a sent copy is stored).
// - deleting: no route deletes a session's messages; `reset` re-ships them, `archive` keeps them.
// - weekly limit, record, replies, forget: the coach's own repo (config/coach.json,
//   bin/trellis-coach-ledger, bin/trellis-friction, bin/trellis-email).
const CONSENT: { title: string; body: (name: string) => string }[] = [
  {
    title: 'What it reads',
    body: (n) =>
      `The canopy sessions you already have, in workspaces ${n} belongs to: chats you started, and the Claude Code sessions on your own computer for the repos you switched on under Settings, Runners, Opted-in repos. It reads whole sessions: what you typed, what anyone else wrote in that chat, what the agent answered, and what the tools returned. It never reads a conversation marked sensitive, and a computer set up as sensitive-data uploads nothing. This box covers only you. ${n} reads nobody who has not let it in; the other ways in are a person approving one look with their passkey, and an admin giving it the sessions shared in a Slack channel.`,
  },
  {
    title: 'What your computer does',
    body: (n) =>
      `Turning this on tells your own canopy runner to upload those sessions in full, including ones that have finished, and including any filed in a workspace ${n} cannot read. ${n} cannot ask your computer for anything; it reads only what your runner sent.`,
  },
  {
    title: 'How much, and how often',
    body: (n) =>
      `Once a week ${n} looks at what is new since its last note, at most twenty sessions. That limit is ${n}'s own rule; canopy itself lets ${n} read any of those sessions, however old.`,
  },
  {
    title: 'Who gets the feedback',
    body: () =>
      'It is addressed only to you: a short email from trellis@dimagi.ai (or a reply in a chat you start, if your access is more than read-only). One thing that went well, and at most three suggestions, each pointing at the moment it is about. If there is nothing to say, or nothing of yours can be read, it tells you that, not every week.',
  },
  {
    title: 'Who else can see it',
    body: (n) =>
      `Its mail: ${n}'s mailbox is hosted on canopy, and any member of its workspace can open it, so each note and your reply stay there for them to read. Its work log: each time ${n} reads your sessions it is a "turn" on canopy, and these people can open a turn's log: whoever started it, ${n}'s owner and admins, the owners and admins of its workspace, and the owner of the machine that ran it. Anyone that owner made an admin of the machine can read the turn's session. A turn inside a chat is read by that chat's people instead. That log can contain lines quoted from your sessions and the note itself. In manual mode a person who runs ${n} approves a note before it goes. Anyone allowed to edit ${n} can change its instructions, which can change what a later run does with what it reads. Your uploaded sessions follow the rule for all your sessions: people you added, that machine's admins, and project teams you shared the repo with can read them. Each session's "Who can see this" line says who.`,
  },
  {
    title: 'What it keeps',
    body: (n) =>
      `A small record per person on the machine that runs ${n}: the date of each note, which habits it was about, where in which session (a session id and message number, never the words), the mail thread, and how you replied (useful, wrong, already knew, pause). Nothing you wrote is copied into it. ${n} may also note, without your name, which kinds of trouble many people run into, such as a step canopy explains badly. Those notes are checked to hold no names, quotes or session ids, and nothing is proposed to anyone from fewer than three different people.`,
  },
]

const CONSENT_ACTIONS: ((name: string) => string)[] = [
  () =>
    'Reply "pause" to any note: no notes for four weeks. Reply "wrong" or "already knew" and that habit is not raised with you again. Replies are read at the next weekly pass.',
  (n) =>
    `Untick the box (Settings, Account, Who can review your sessions): ${n}'s access stops at once. On its next check your computer stops uploading finished sessions, unless another agent still reviews you. Sessions still active keep syncing, as they do for everyone in the repos you switched on. Notes stay in your inbox and in ${n}'s mailbox, and its logs and record stay.`,
  () =>
    'Sessions already uploaded stay on canopy. You cannot delete one yourself today: archiving only takes it off your active list, and it stays stored and readable. Only a workspace retention rule, where one is on, can remove them.',
  (n) =>
    `Have ${n} forget you ("forget my coaching record") in a full session with it, not read mode, or ask one of its admins: that deletes your record and the pattern notes that came from you. It cannot unsend email, clear its logs or mailbox, or delete your sessions from canopy.`,
]

/** "What does this mean?" beside the box. Outside the box's `<label>`, so opening
 *  it never ticks anything. Renders nothing for a coach this copy was not checked for. */
export function CoachConsentDetails({ agent, name }: CoachNames) {
  if (agent !== CHECKED_COACH) return null
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer text-foreground-secondary">What does this mean?</summary>
      <div className="mt-2 space-y-2">
        {CONSENT.map((s) => (
          <p key={s.title}>
            <span className="font-medium text-foreground">{s.title}.</span> {s.body(name)}
          </p>
        ))}
        <div>
          <span className="font-medium text-foreground">What you can do.</span>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {CONSENT_ACTIONS.map((line, i) => (
              <li key={i}>{line(name)}</li>
            ))}
          </ul>
        </div>
      </div>
    </details>
  )
}

/** "Who can review your sessions": the coach canopy offers (one switch), and any
 *  other agent you let read your sessions, each with Stop. Read-only grants,
 *  enforced by the session ACL (apps/canopy_sessions/access.py, leg 5). */
export function SessionReviewersPanel() {
  const [data, setData] = useState<SessionReviewersData | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listMySessionReviewers()
      .then(setData)
      .catch(() => {
        // Only a person has this (an agent's login is
        // refused): a failed load just leaves the panel out.
      })
  }, []) // once, on mount

  const act = async (work: () => Promise<SessionReviewersData>) => {
    setBusy(true)
    setError(null)
    try {
      setData(await work())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return null
  const coach = data.coach?.member ? data.coach : null
  const others = data.reviewers.filter((r) => r.agent !== coach?.agent)
  if (!coach && others.length === 0) return null
  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3">
      <h2 className="text-sm font-semibold text-foreground">Who can review your sessions</h2>
      <p className="text-xs text-muted-foreground">
        An agent you let in reads the chats you start and the sessions on your own runners — never a
        private (sensitive) one — and changes nothing. Turning it on or off is on your memory audit.
      </p>
      {coach && (
        <div className="space-y-2">
          <label className="flex items-start gap-2 text-sm text-foreground">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={coach.on}
              disabled={busy}
              onChange={() =>
                void act(() => (coach.on ? removeMySessionReviewer(coach.agent) : addMySessionReviewer(coach.agent)))
              }
            />
            <span>{coachLabel(coach)}</span>
          </label>
          <CoachConsentDetails {...coach} />
        </div>
      )}
      {others.length > 0 && (
        <ul className="space-y-2">
          {others.map((r) => (
            <li key={r.agent} className="flex items-center justify-between gap-3 text-sm">
              <span>
                <span className="font-medium text-foreground">{r.name}</span>{' '}
                <span className="text-xs text-muted-foreground">
                  since {new Date(r.granted_at).toLocaleDateString()}
                </span>
              </span>
              <button
                className="text-xs text-destructive hover:underline disabled:opacity-50"
                disabled={busy}
                onClick={() => void act(() => removeMySessionReviewer(r.agent))}
              >
                Stop
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  )
}
