// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { copyText, useCopy } from '../lib/copy'
import { toneClasses, toneDot, toneText, type StatusTone } from '../lib/tone'
import { CopyButton } from './copy-button'
import { IconButton } from './icon-button'
import { Segmented } from './segmented'
import { StatusBadge } from './status-badge'

const writeText = vi.fn()

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined)
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('tone', () => {
  const tones: StatusTone[] = ['neutral', 'primary', 'success', 'warning', 'info', 'special', 'destructive']
  it('is the documented tinted recipe for every semantic tone', () => {
    for (const t of tones.filter((x) => x !== 'neutral')) {
      expect(toneClasses(t)).toBe(`bg-${t}/10 text-${t} border-${t}/30`)
      expect(toneText(t)).toBe(`text-${t}`)
      expect(toneDot(t)).toBe(`bg-${t}`)
    }
    expect(toneClasses('neutral')).toBe('bg-muted text-muted-foreground border-border')
  })
})

describe('StatusBadge', () => {
  it('renders its tone and an optional pulsing dot', () => {
    render(<StatusBadge tone="warning" pulse>stuck</StatusBadge>)
    const badge = screen.getByText('stuck')
    expect(badge.getAttribute('data-tone')).toBe('warning')
    expect(badge.className).toContain('text-warning')
    expect(badge.querySelector('.animate-pulse')).not.toBeNull()
  })
  it('defaults to neutral with no dot', () => {
    render(<StatusBadge>idle</StatusBadge>)
    expect(screen.getByText('idle').className).toContain('text-muted-foreground')
    expect(screen.getByText('idle').querySelector('span')).toBeNull()
  })
})

describe('Segmented', () => {
  const options = [
    { value: 'a', label: 'Alpha' },
    { value: 'b', label: 'Beta' },
  ] as const

  it('is a labelled radiogroup with the current value checked', () => {
    render(<Segmented label="Pick one" value="a" options={options} onChange={() => {}} />)
    expect(screen.getByRole('radiogroup', { name: 'Pick one' })).toBeTruthy()
    expect(screen.getByRole('radio', { name: 'Alpha' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('radio', { name: 'Beta' }).getAttribute('aria-checked')).toBe('false')
  })

  it('reports a new pick and ignores the current one', () => {
    const onChange = vi.fn()
    render(<Segmented label="Pick one" value="a" options={options} onChange={onChange} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Alpha' }))
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('radio', { name: 'Beta' }))
    expect(onChange).toHaveBeenCalledWith('b')
  })
})

describe('IconButton', () => {
  it('names itself by its label and is a plain button', () => {
    const onClick = vi.fn()
    render(<IconButton label="Remove" danger onClick={onClick}>x</IconButton>)
    const b = screen.getByRole('button', { name: 'Remove' })
    expect(b.getAttribute('title')).toBe('Remove')
    expect(b.getAttribute('type')).toBe('button')
    expect(b.className).toContain('hover:text-destructive')
    fireEvent.click(b)
    expect(onClick).toHaveBeenCalled()
  })
})

describe('copy', () => {
  it('copyText resolves false without a clipboard or when refused', async () => {
    expect(await copyText('x')).toBe(true)
    writeText.mockRejectedValueOnce(new Error('denied'))
    expect(await copyText('x')).toBe(false)
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    expect(await copyText('x')).toBe(false)
  })

  it('useCopy marks the copied key, then clears it', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useCopy(1000))
    await act(async () => { await result.current.copy('hello', 'row-1') })
    expect(writeText).toHaveBeenCalledWith('hello')
    expect(result.current.copied).toBe('row-1')
    act(() => { vi.advanceTimersByTime(1000) })
    expect(result.current.copied).toBeNull()
  })

  it('CopyButton says Copied after a copy, and reports a refusal', async () => {
    const onCopyFailed = vi.fn()
    render(<CopyButton text="v" onCopyFailed={onCopyFailed} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })) })
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()

    cleanup()
    writeText.mockRejectedValueOnce(new Error('denied'))
    render(<CopyButton text="v" onCopyFailed={onCopyFailed} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })) })
    expect(onCopyFailed).toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Copy' })).toBeTruthy()
  })
})
