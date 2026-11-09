// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ListenDeps } from '@/listen/controller'
import { memoryStore, socketFactory } from '@/listen/testing'
import { LAPTOP_PAGE, ListenPage, NOTICE } from './ListenPage'

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
  it('explains how to pair when this phone is not paired', async () => {
    render(<ListenPage deps={deps()} />)
    expect(await screen.findByText('Record meetings from your phone')).toBeTruthy()
    expect(screen.getByText(LAPTOP_PAGE)).toBeTruthy()
    expect(screen.getByText('“Pair a phone”')).toBeTruthy()
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
