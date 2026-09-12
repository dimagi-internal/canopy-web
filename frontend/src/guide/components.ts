/**
 * The five components of canopy, and the one sentence that connects them.
 * This is the "component view" the public explainer is built around — the thing
 * that was actually missing from /system, which catalogues the plugin's
 * capabilities and never says what the system is made of.
 */
export interface SystemComponent {
  name: string
  what: string
}

export const COMPONENTS: SystemComponent[] = [
  {
    name: 'canopy-web',
    what: 'The system of record and every human surface. Owns workspaces, agents, turns, items, schedules, sessions and published artifacts.',
  },
  {
    name: 'The canopy plugin',
    what: 'The capability library — skills, agents and commands, installed into Claude Code. What an agent knows how to do.',
  },
  {
    name: 'Runners',
    what: 'The execution substrate. A paired laptop or a cloud box. Claims turns and runs Claude Code.',
  },
  {
    name: 'Agents',
    what: 'Persona repos stamped from a factory — identity, domain skills, hooks.',
  },
  {
    name: 'The harness',
    what: 'The control plane joining them: turn lifecycle, claim and lease, the event ledger, routing.',
  },
]

export const ONE_SENTENCE =
  'A turn is the unit of work: canopy-web owns the record of turns, runners execute them wherever your compute happens to live, and the plugin is the library of what an agent knows how to do.'
