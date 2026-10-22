import { useState } from 'react'

import type { SupervisorFrame, SupervisorRunnerLive } from '@/api/types.ws'
import { wsUrl } from '@/lib/wsUrl'

import { useReconnectingSocket } from './useReconnectingSocket'

export interface SupervisorState {
  runners: Record<string, SupervisorRunnerLive>
  waiting: Record<string, number>
  totalWaiting: number
}

export const EMPTY_SUPERVISOR_STATE: SupervisorState = {
  runners: {},
  waiting: {},
  totalWaiting: 0,
}

// Pure reducer: a snapshot seeds state; runner/waiting deltas patch it. Exported
// so it unit-tests without a socket.
export function applyFrame(state: SupervisorState, frame: SupervisorFrame): SupervisorState {
  switch (frame.type) {
    case 'supervisor.snapshot': {
      const runners: Record<string, SupervisorRunnerLive> = {}
      for (const r of frame.runners) runners[r.id] = r
      return {
        runners,
        waiting: { ...frame.waiting },
        totalWaiting: frame.total_waiting,
      }
    }
    case 'supervisor.runner':
      return { ...state, runners: { ...state.runners, [frame.runner.id]: frame.runner } }
    case 'supervisor.waiting': {
      const waiting = { ...state.waiting, [frame.agent]: frame.waiting_count }
      const totalWaiting = Object.values(waiting).reduce((a, b) => a + b, 0)
      return { ...state, waiting, totalWaiting }
    }
    default:
      return state
  }
}

export interface LiveSupervisor {
  runners: SupervisorRunnerLive[]
  waiting: Record<string, number>
  connected: boolean
  hasSnapshot: boolean
}

// Live /supervisor: snapshot on connect, then runner/waiting deltas, over one WS
// with backoff reconnect. SupervisorPage layers this over its mount fetch.
export function useLiveSupervisor(): LiveSupervisor {
  const [state, setState] = useState<SupervisorState>(EMPTY_SUPERVISOR_STATE)
  const [hasSnapshot, setHasSnapshot] = useState(false)
  const { connected } = useReconnectingSocket(wsUrl('ws/supervisor/'), (raw) => {
    const frame = raw as SupervisorFrame
    if (frame.type === 'supervisor.snapshot') setHasSnapshot(true)
    setState((prev) => applyFrame(prev, frame))
  })

  return {
    runners: Object.values(state.runners),
    waiting: state.waiting,
    connected,
    hasSnapshot,
  }
}
