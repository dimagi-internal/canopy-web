import type { CapabilityKind } from '@/api/system'

/**
 * Curated "how the pieces fit together" chains for the /system Workflows view.
 *
 * Canopy has no machine-readable composition metadata, so these are hand-authored
 * here as DATA (not JSX) — edit this list to add/adjust a workflow without
 * touching render code. Each step references a capability by (kind, name); the
 * renderer resolves it against the live catalog and links to its detail, so a
 * renamed/removed capability degrades to a non-linked step instead of breaking.
 */
export interface WorkflowStep {
  kind: CapabilityKind
  name: string
  note?: string
}

export interface Workflow {
  id: string
  title: string
  description: string
  steps: WorkflowStep[]
}

export const WORKFLOWS: Workflow[] = [
  {
    id: 'ddd',
    title: 'Demo-driven development (DDD)',
    description:
      'Turn an idea into a verified, narrated demo: gather evidence, agree on the story, render + judge it, then publish the package.',
    steps: [
      { kind: 'agent', name: 'ddd', note: 'Orchestrates the whole loop' },
      { kind: 'skill', name: 'ddd-evidence-audit', note: 'Gather docs / code / research' },
      { kind: 'skill', name: 'ddd-why-brief', note: 'Draft the grounded "why"' },
      { kind: 'skill', name: 'ddd-spec', note: 'Author the unified spec' },
      { kind: 'skill', name: 'ddd-spec-qa', note: 'Structural QA gate' },
      { kind: 'skill', name: 'ddd-narrative-review', note: 'Human sign-off on the story' },
      { kind: 'skill', name: 'ddd-run', note: 'Render + dual-judge' },
      { kind: 'skill', name: 'ddd-findings-review', note: 'Triage product findings' },
      { kind: 'skill', name: 'ddd-upload', note: 'Publish the run package to canopy-web' },
    ],
  },
  {
    id: 'pm',
    title: 'Autonomous product management',
    description:
      'Scout a codebase for improvements, propose + implement them, and learn across sprints — supervised or fully autonomous.',
    steps: [
      { kind: 'agent', name: 'pm-supervisor', note: 'The supervising agent' },
      { kind: 'command', name: 'pm-scout', note: 'Scout one lens, propose changes' },
      { kind: 'skill', name: 'product-management', note: 'The full scout → propose → implement → learn loop' },
      { kind: 'command', name: 'pm-status', note: 'Where the project stands' },
      { kind: 'command', name: 'pm-autonomous', note: 'Run a hands-off sprint' },
    ],
  },
  {
    id: 'walkthrough',
    title: 'Demo walkthroughs & evals',
    description:
      'Run a demo against the live app into a scored slideshow, measure it against fixtures, and share the result.',
    steps: [
      { kind: 'agent', name: 'walkthrough', note: 'Orchestrate improvement cycles' },
      { kind: 'skill', name: 'walkthrough', note: 'Run a spec → screenshots + AI scores' },
      { kind: 'skill', name: 'walkthrough-eval', note: 'Score against known-defect fixtures' },
      { kind: 'skill', name: 'walkthrough-defect-creator', note: 'Generate eval fixtures' },
      { kind: 'skill', name: 'walkthrough-share', note: 'Upload + share the artifact' },
    ],
  },
  {
    id: 'portfolio',
    title: 'Portfolio sweep',
    description:
      'Survey every project for per-project next steps and roll it into a strategic brief.',
    steps: [
      { kind: 'skill', name: 'portfolio-guide', note: '"What to do next" per project' },
      { kind: 'skill', name: 'brief', note: 'Strategic brief from recent activity' },
    ],
  },
]
