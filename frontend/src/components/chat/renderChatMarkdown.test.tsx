// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'
import { renderChatMarkdown } from './renderChatMarkdown'

afterEach(cleanup)

describe('renderChatMarkdown', () => {
  it.each([
    ['a markdown link', '[the PR](https://github.com/x/y/pull/1)', 'https://github.com/x/y/pull/1'],
    ['a bare URL', 'see https://canopy.dimagi.com/agents/ada', 'https://canopy.dimagi.com/agents/ada'],
    ['an in-app path', '[Ada](/agents/ada)', '/agents/ada'],
  ])('opens %s in a new tab', (_label, text, href) => {
    const { container } = render(renderChatMarkdown(text))
    const a = container.querySelector('a')!
    expect(a.getAttribute('href')).toBe(href)
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })
})
