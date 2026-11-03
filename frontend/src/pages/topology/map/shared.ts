
export type Selection =
  | { kind: 'agent'; slug: string }
  | { kind: 'runner'; id: string }
  | { kind: 'add'; workspace: string; owner: string }
  | { kind: 'order'; workspace: string }
  | null

export const HEALTH = {
  ok: null,
  down: { text: 'No live runner', className: 'text-destructive' },
  unrouted: { text: 'Unrouted', className: 'text-destructive' },
} as const
