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
