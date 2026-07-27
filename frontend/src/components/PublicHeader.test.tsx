// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ThemeProvider } from '@/theme/ThemeProvider'
import { PublicHeader } from './PublicHeader'
import { AuthContext } from '@/auth/AuthProvider'

/**
 * The public surfaces were chrome-less because AppLayout's header fires authed
 * calls that bounce an anonymous visitor to Google. This header must carry the
 * brand without carrying that hazard.
 */
function renderAs(status: 'authenticated' | 'anonymous') {
  return render(
    <MemoryRouter>
      <ThemeProvider>
      <AuthContext.Provider
        value={
          status === 'authenticated'
            ? {
                status,
                user: { name: 'Jonathan Jackson', email: 'jj@dimagi.com', avatar_url: '' },
              }
            : { status, user: null }
        }
      >
        <PublicHeader trail="Proving a programme works" />
      </AuthContext.Provider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

describe('PublicHeader', () => {
  afterEach(cleanup)

  it('shows the Canopy mark to an anonymous reviewer', () => {
    renderAs('anonymous')
    expect(screen.getByText('Canopy')).toBeTruthy()
    expect(screen.getByText('Proving a programme works')).toBeTruthy()
  })

  it('offers a signed-out reviewer no link into the app', () => {
    // Every route behind it is a login wall, so the link would be a trapdoor.
    const { container } = renderAs('anonymous')
    expect(container.querySelectorAll('a')).toHaveLength(0)
  })

  it('links home for someone who has one', () => {
    renderAs('authenticated')
    expect(screen.getByText('Canopy').closest('a')?.getAttribute('href')).toBe('/')
  })

  it('lets either of them change the theme', () => {
    renderAs('anonymous')
    expect(screen.getByRole('button', { name: /Switch to/ })).toBeTruthy()
  })
})
