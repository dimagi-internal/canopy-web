/**
 * The five ways into canopy. Rendered by BOTH the public explainer and the
 * in-app guide, so the public promise cannot drift from the internal docs —
 * paths.test.ts asserts every referenced surface is one the guide describes.
 *
 * Two notes on this taxonomy, because it differs from the obvious one.
 * "Watch a turn in the browser" and "watch a turn on your laptop" are NOT two
 * paths: they are one job with a deployment choice inside it, and the choice is
 * the interesting part. And "you were sent a link" is first, because it is
 * almost certainly the highest-volume interaction anyone has with canopy.
 */
export interface UserPath {
  id: string
  title: string
  /** Who you are, if this is your path. */
  who: string
  /** The literal first thing to do. */
  startHere: string
  /** Route paths (from surfaces.ts) or external entry points. */
  surfaces: string[]
  note?: string
}

export const USER_PATHS: UserPath[] = [
  {
    id: 'audience',
    title: 'You were sent a link',
    who: 'An audience. No login, no install, nothing to set up.',
    startHere: 'Open the link. That is the whole path.',
    surfaces: ['/storyboard/:slug', '/narrative/:slug', '/walkthrough/:id', '/share/:token'],
    note: 'Storyboards, narratives, demo walkthroughs and shared transcripts are all public when shared.',
  },
  {
    id: 'operator',
    title: 'You want an agent to do work',
    who: 'An operator with a job to hand off.',
    startHere: 'Open Chats in a workspace and start a chat with an agent.',
    surfaces: ['/w/:workspace/chat', '/w/:workspace/chat/:id', '/w/:workspace/activity'],
    note: 'Then choose where it runs: a cloud runner needs nothing from you, or pair your own laptop so you can jump into the terminal mid-turn.',
  },
  {
    id: 'supervisor',
    title: 'You keep agents unblocked',
    who: 'Someone who owns agents and is not driving every turn.',
    startHere: 'Open Supervisor, then install it as a phone app.',
    surfaces: ['/supervisor', '/schedules'],
    note: 'It pushes a notification when any agent you own starts waiting on you.',
  },
  {
    id: 'skills',
    title: "You want canopy's skills in your own sessions",
    who: 'A Claude Code user who does not want to run a fleet.',
    startHere: 'Mint a token in Settings, then install the canopy plugin.',
    surfaces: ['/settings', '/system'],
    note: 'No agents, no runners. Just the capability library in your own Claude Code.',
  },
  {
    id: 'builder',
    title: 'You want to build an agent',
    who: 'A builder who needs a persona of their own.',
    startHere: 'Run /canopy:create-agent, then register it in the workspace.',
    surfaces: ['/w/:workspace/agents'],
    note: 'The scaffold is a skeleton — persona and domain skills are yours to write.',
  },
]
