// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { Markdown } from './Markdown'

afterEach(cleanup)

// An agent shows a picture in chat with markdown image syntax (e.g. a Labs coach
// chart in the Connect Labs embed panel). It must render as a real <img>, and a
// `data:` URL must not become a src — react-markdown's default urlTransform is
// what guarantees that, and this pins it.

describe('Markdown images', () => {
  it('renders an http(s) image with its src and alt, linked to the full image', () => {
    const { container } = render(<Markdown>{'![x](https://example.com/a.png)'}</Markdown>)
    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('https://example.com/a.png')
    expect(img!.getAttribute('alt')).toBe('x')
    expect(img!.getAttribute('loading')).toBe('lazy')
    expect(img!.getAttribute('referrerpolicy')).toBe('no-referrer')
    const link = img!.closest('a')
    expect(link?.getAttribute('href')).toBe('https://example.com/a.png')
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toContain('noopener')
  })

  it('does not render a data: URL as an image src', () => {
    const { container } = render(
      <Markdown>{'![x](data:image/png;base64,iVBORw0KGgo=)'}</Markdown>,
    )
    for (const img of container.querySelectorAll('img')) {
      expect(img.getAttribute('src') ?? '').not.toMatch(/^data:/)
    }
    expect(container.innerHTML).not.toContain('data:image')
  })
})

describe('Markdown links', () => {
  it('follows a link in place by default', () => {
    const { container } = render(<Markdown>{'[the explorer](https://labs.example.org/pmc/)'}</Markdown>)
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://labs.example.org/pmc/')
    expect(a.getAttribute('target')).toBeNull()
  })

  it('opens a link in a new tab inside an embedded panel, which a framed page would refuse', () => {
    const { container } = render(
      <Markdown linksInNewTab>{'[the explorer](https://labs.example.org/pmc/)'}</Markdown>,
    )
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe('https://labs.example.org/pmc/')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toContain('noopener')
  })
})

describe('Markdown for data', () => {
  it('wraps a table in its own scroll box with a sticky header', () => {
    const { container } = render(<Markdown>{'| a | b |\n|---|---|\n| 1 | 2 |'}</Markdown>)
    const box = container.querySelector('[data-slot="md-table"]')
    expect(box).not.toBeNull()
    expect(box!.className).toContain('overflow-auto')
    expect(box!.querySelector('table th')?.textContent).toBe('a')
  })

  it('gives a fenced code block a copy button carrying its text', () => {
    const { container } = render(<Markdown>{'```sql\nselect 1;\n```'}</Markdown>)
    const block = container.querySelector('[data-slot="md-code"]')
    expect(block?.querySelector('pre code')?.textContent).toBe('select 1;\n')
    expect(block?.querySelector('button[aria-label="Copy code"]')).not.toBeNull()
  })

  it('leaves inline code alone', () => {
    const { container } = render(<Markdown>{'use `x` here'}</Markdown>)
    expect(container.querySelector('[data-slot="md-code"]')).toBeNull()
  })
})
