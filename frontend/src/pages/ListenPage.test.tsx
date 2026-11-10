// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ListenDeps } from '@/listen/controller'
import { memoryStore, socketFactory } from '@/listen/testing'
import { ListenPage, NOTICE } from './ListenPage'

afterEach(() => {
  cleanup()
  sessionStorage.clear()
})

function deps(store = memoryStore(), online = true): ListenDeps {
  return {
    openSession: vi.fn(async () => 'sid'),
    endSession: vi.fn(async () => undefined),
    listRunners: vi.fn(async () => [{ id: 'runner-1', name: 'jj-mbp', online }]),
    store,
    socket: socketFactory().make,
    wsUrl: (p) => `wss://x/${p}`,
    label: 'iPhone',
    startMic: vi.fn(async () => ({ stop: () => undefined })),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }
}

const PAIRING = { runner_id: 'runner-1', laptop_pub: 'BA3MdkjHijEY8mEoZv6D7xn0AwT2IzmeEhGhDys9HAXPMOgCdne8uRKUPpn_cRQOqbwkcTvht1wx_0BUckxPnQA', laptop_name: "Jonathan's MacBook" }

describe('ListenPage', () => {
  it('lists my computers to pair with when this browser is not paired, and pairs by code', async () => {
    const d = deps()
    render(<ListenPage deps={d} />)
    expect(await screen.findByText('Record meetings with Canopy')).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: /jj-mbp/ }))
    await vi.waitFor(() => expect(d.openSession).toHaveBeenCalledTimes(1))
    const [runner, kind, hello] = (d.openSession as ReturnType<typeof vi.fn>).mock.calls[0]
    expect([runner, kind, hello.phone_key]).toEqual(['runner-1', 'pair', null])
    expect(hello.commit).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(await screen.findByText('Pairing with jj-mbp')).toBeTruthy()
  })

  it('offers a call in another tab only where the browser can share one', async () => {
    const store = memoryStore(PAIRING)
    await store.acceptNotice()
    const withTab = { ...deps(store), startTab: vi.fn(async () => ({ stop: () => undefined })) }
    render(<ListenPage deps={withTab} />)
    const tab = await screen.findByRole('button', { name: /Record a call in another tab/ })
    await vi.waitFor(() => expect((tab as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(tab)
    expect(withTab.startTab).toHaveBeenCalledTimes(1)
    expect(withTab.startMic).not.toHaveBeenCalled()
    cleanup()
    render(<ListenPage deps={deps(store)} />)
    expect(await screen.findByText('Online')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /another tab/ })).toBeNull()
  })

  it('asks for the notice before the first recording, then lets it start', async () => {
    const d = deps(memoryStore(PAIRING))
    render(<ListenPage deps={d} />)
    expect(await screen.findByText("Jonathan's MacBook")).toBeTruthy()
    expect(await screen.findByText('Online')).toBeTruthy()
    const record = screen.getByRole('button', { name: 'Start recording' }) as HTMLButtonElement
    expect(record.disabled).toBe(true)
    expect(screen.getByText(NOTICE)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'I’ll tell them' }))
    await vi.waitFor(() => expect(record.disabled).toBe(false))
    expect(await d.store.noticeAccepted()).toBe(true)
    fireEvent.click(record)
    expect(d.startMic).toHaveBeenCalledTimes(1) // inside the tap, before anything is awaited
    expect(await screen.findByText(/Waiting for Jonathan's MacBook/)).toBeTruthy()
  })

  it('says the laptop is offline and keeps the button off', async () => {
    const store = memoryStore(PAIRING)
    await store.acceptNotice()
    render(<ListenPage deps={deps(store, false)} />)
    expect(await screen.findByText('Offline')).toBeTruthy()
    expect(screen.getByText(/make sure Canopy Runner is running/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Start recording' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('refuses an expired pairing link and says so', async () => {
    const enc = (o: unknown) => btoa(JSON.stringify(o)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const key = btoa(String.fromCharCode(4, ...new Array(64).fill(1))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const secret = btoa(String.fromCharCode(...new Array(32).fill(2))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    sessionStorage.setItem('canopy.listen.pair', '#pair=' + enc({ r: 'runner-1', k: key, s: secret, n: 'MBP', x: 1 }))
    render(<ListenPage deps={deps()} />)
    expect(await screen.findByText(/pairing code has expired/)).toBeTruthy()
  })
})
