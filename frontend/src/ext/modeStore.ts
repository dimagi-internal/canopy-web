/**
 * The person's mode for one app in the extension's panel: `server` until they switch, then what they
 * chose, kept on this browser (docs/architecture/local-and-server-modes.md). The panel's switch and a
 * page's `canopy.mode.set` (from a click) both write here; every page of the app hears the change.
 */
import type { Mode } from '@/dynamicViews/protocol'

export const DEFAULT_MODE: Mode = 'server'
const PREFIX = 'canopy.mode:'

export function modeKey(workspace: string, appKey: string): string {
  return `${PREFIX}${workspace}/${appKey}`
}

export class ModeStore {
  private listeners = new Set<(mode: Mode) => void>()
  private mode: Mode
  private key: string
  private storage: Pick<Storage, 'getItem' | 'setItem'> | null

  constructor(key: string, storage: Pick<Storage, 'getItem' | 'setItem'> | null = safeStorage()) {
    this.key = key
    this.storage = storage
    const kept = this.storage?.getItem(key)
    this.mode = kept === 'local' || kept === 'server' ? kept : DEFAULT_MODE
  }

  get(): Mode {
    return this.mode
  }

  set(mode: Mode): void {
    if (mode !== 'local' && mode !== 'server') throw new Error('A mode is "local" or "server".')
    if (mode === this.mode) return
    this.mode = mode
    try {
      this.storage?.setItem(this.key, mode)
    } catch {
      // A browser that won't keep it still switches for this panel.
    }
    this.listeners.forEach((l) => l(mode))
  }

  subscribe(listener: (mode: Mode) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

function safeStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}
