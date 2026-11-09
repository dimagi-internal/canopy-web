/**
 * What this phone keeps for Canopy Listen, in IndexedDB: its static key (made once, the
 * private half non-extractable, so it can be used here and never read out), which laptop
 * it is paired with, and whether its person has acknowledged the recording notice.
 */
import { newKeyPair } from './relayCrypto'

export interface Pairing {
  runner_id: string
  laptop_pub: string
  laptop_name: string
}

export interface ListenStore {
  phoneKey(): Promise<CryptoKeyPair>
  pairing(): Promise<Pairing | null>
  setPairing(pairing: Pairing | null): Promise<void>
  noticeAccepted(): Promise<boolean>
  acceptNotice(): Promise<void>
}

const DB = 'canopy-listen'
const KV = 'kv'

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(KV)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error ?? new Error('IndexedDB unavailable'))
  })
}

async function tx<T>(mode: IDBTransactionMode, run: (store: IDBObjectStore) => IDBRequest<T> | null): Promise<T | undefined> {
  const db = await open()
  try {
    return await new Promise<T | undefined>((resolve, reject) => {
      const t = db.transaction(KV, mode)
      const req = run(t.objectStore(KV))
      t.oncomplete = () => resolve(req ? req.result : undefined)
      t.onerror = () => reject(t.error ?? new Error('IndexedDB write failed'))
      t.onabort = () => reject(t.error ?? new Error('IndexedDB write aborted'))
    })
  } finally {
    db.close()
  }
}

const get = <T>(key: string) => tx<T>('readonly', (s) => s.get(key) as IDBRequest<T>)
const put = (key: string, value: unknown) => tx('readwrite', (s) => s.put(value, key)).then(() => undefined)
const del = (key: string) => tx('readwrite', (s) => s.delete(key)).then(() => undefined)

let making: Promise<CryptoKeyPair> | null = null

export const browserStore: ListenStore = {
  phoneKey() {
    making ??= (async () => {
      const kept = await get<CryptoKeyPair>('phoneKey')
      if (kept?.privateKey && kept.publicKey) return kept
      const fresh = await newKeyPair()
      await put('phoneKey', fresh)
      return fresh
    })().catch((e: unknown) => {
      making = null
      throw e
    })
    return making
  },
  async pairing() {
    return (await get<Pairing>('pairing')) ?? null
  },
  setPairing(pairing) {
    return pairing ? put('pairing', pairing) : del('pairing')
  },
  async noticeAccepted() {
    return (await get<boolean>('notice')) === true
  },
  acceptNotice() {
    return put('notice', true)
  },
}

/** A device label the laptop shows for this phone ("iPhone", "Android phone"). */
export function deviceLabel(ua: string = navigator.userAgent): string {
  if (/iPhone/.test(ua)) return 'iPhone'
  if (/iPad/.test(ua)) return 'iPad'
  if (/Android/.test(ua)) return /Mobile/.test(ua) ? 'Android phone' : 'Android tablet'
  if (/Macintosh/.test(ua)) return 'Mac browser'
  if (/Windows/.test(ua)) return 'Windows browser'
  return 'Phone'
}
