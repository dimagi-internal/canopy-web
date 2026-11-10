// Fakes for the listen tests: a socket the test drives, and an in-memory store.
import type { SocketLike } from './relay'
import { newKeyPair } from './relayCrypto'
import type { ListenStore, Pairing } from './store'

export class FakeSocket implements SocketLike {
  binaryType = 'blob'
  readyState = 0
  sent: Uint8Array[] = []
  closedWith: number | null = null
  onopen: ((ev: unknown) => void) | null = null
  onmessage: ((ev: { data: unknown }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  readonly url: string

  constructor(url: string) {
    this.url = url
  }

  send(data: Uint8Array | ArrayBuffer) {
    this.sent.push(data instanceof Uint8Array ? data : new Uint8Array(data))
  }

  close(code = 1000) {
    this.closedWith = code
    this.readyState = 3
  }

  // -- the test's side --
  open() {
    this.readyState = 1
    this.onopen?.({})
  }

  text(note: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(note) })
  }

  binary(frame: Uint8Array) {
    this.onmessage?.({ data: frame.slice().buffer })
  }

  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
}

export function socketFactory() {
  const sockets: FakeSocket[] = []
  return {
    sockets,
    make: (url: string) => {
      const s = new FakeSocket(url)
      sockets.push(s)
      return s
    },
  }
}

export function memoryStore(pairing: Pairing | null = null): ListenStore & { kept: { pairing: Pairing | null } } {
  const kept = { pairing }
  let key: Promise<CryptoKeyPair> | null = null
  let notice = false
  return {
    kept,
    phoneKey: () => (key ??= newKeyPair()),
    pairing: async () => kept.pairing,
    setPairing: async (p) => {
      kept.pairing = p
    },
    noticeAccepted: async () => notice,
    acceptNotice: async () => {
      notice = true
    },
  }
}

/** Let pending promises (WebCrypto included) settle. */
export async function settle(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0))
}
