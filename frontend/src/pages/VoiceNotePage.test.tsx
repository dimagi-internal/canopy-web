// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ListenDeps } from '@/listen/controller'
import { memoryStore, socketFactory } from '@/listen/testing'
import type { Pairing } from '@/listen/store'
import { VoiceNotePage, type NoteSender } from './VoiceNotePage'

afterEach(() => cleanup())

const PAIRING: Pairing = { runner_id: 'runner-1', laptop_pub: 'BA3MdkjHijEY8mEoZv6D7xn0AwT2IzmeEhGhDys9HAXPMOgCdne8uRKUPpn_cRQOqbwkcTvht1wx_0BUckxPnQA', laptop_name: "Jonathan's MacBook" }

function deps(pairing: Pairing | null = PAIRING): ListenDeps {
  return {
    openSession: vi.fn(async () => 'sid'),
    endSession: vi.fn(async () => undefined),
    listRunners: vi.fn(async () => []),
    store: memoryStore(pairing),
    socket: socketFactory().make,
    wsUrl: (p) => `wss://x/${p}`,
    label: 'iPhone',
    startMic: vi.fn(async () => ({ stop: () => undefined, resume: async () => true })),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }
}

const sender: NoteSender = {
  send: vi.fn(async () => ({ id: 's-1', workspace: 'jonathan' })),
  agentName: vi.fn(async () => 'Hal'),
}

function show(d: ListenDeps, url = '/listen/voice?agent=hal&workspace=jonathan') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/listen/voice" element={<VoiceNotePage deps={d} sender={sender} />} />
        <Route path="/listen" element={<p>Listen home</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('VoiceNotePage', () => {
  it('names the agent and starts the microphone from the tap', async () => {
    const d = deps()
    show(d)
    expect(await screen.findByRole('heading', { name: 'Talk to Hal' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Start recording' }))
    expect(d.startMic).toHaveBeenCalledTimes(1)
    expect(await screen.findByRole('button', { name: /Done/ })).toBeTruthy()
    await vi.waitFor(() => expect(d.openSession).toHaveBeenCalledWith('runner-1', 'meeting', expect.anything()))
  })

  it('sends an unpaired phone to pair first', async () => {
    show(deps(null))
    fireEvent.click(await screen.findByRole('link', { name: 'Pair with your laptop' }))
    expect(await screen.findByText('Listen home')).toBeTruthy()
  })

  it('asks for an agent when opened without one', async () => {
    show(deps(), '/listen/voice')
    expect(await screen.findByText(/Pick an agent from your account menu/)).toBeTruthy()
  })
})
