import { describe, expect, it } from 'vitest'
import { sessionDisplay } from './sessionDisplay'

describe('sessionDisplay — status', () => {
  it('puts a parked runner first, naming why', () => {
    expect(
      sessionDisplay({ running: true, waiting_on_you: true, runner_online: false, runner_status: 'paused' }),
    ).toMatchObject({ tone: 'held', label: 'runner paused' })
    expect(sessionDisplay({ running: true, runner_online: false, runner_status: 'disconnected' })).toMatchObject({
      tone: 'held',
      label: 'runner offline',
    })
  })

  it('then a question waiting on you, over running', () => {
    expect(sessionDisplay({ running: true, waiting_on_you: true, runner_online: true })).toMatchObject({
      tone: 'asking',
      label: 'waiting on you',
    })
  })

  it('then running, then idle', () => {
    expect(sessionDisplay({ running: true, runner_online: true })).toMatchObject({ tone: 'running', label: 'running' })
    expect(sessionDisplay({})).toMatchObject({ tone: 'idle', label: 'idle' })
  })

  it('does not hold an unbound session: it has no runner to be offline', () => {
    expect(sessionDisplay({ running: true, runner_online: null }).tone).toBe('running')
  })
})

describe('sessionDisplay — runner', () => {
  it("names the bound runner, else the last turn's, else the requested one", () => {
    expect(sessionDisplay({ runner_name: 'a', turn_runner_name: 'b', requested_runner_name: 'c' })).toMatchObject({
      runner: 'a',
      runnerRequested: false,
    })
    expect(sessionDisplay({ turn_runner_name: 'b', requested_runner_name: 'c' })).toMatchObject({
      runner: 'b',
      runnerRequested: false,
    })
    expect(sessionDisplay({ requested_runner_name: 'c' })).toMatchObject({ runner: '→ c', runnerRequested: true })
  })

  it('is null with no runner at all', () => {
    expect(sessionDisplay({ runner_name: null })).toMatchObject({ runner: null, runnerRequested: false })
  })
})
