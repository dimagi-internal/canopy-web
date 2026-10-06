import { describe, expect, it } from 'vitest'
import { UPLOADED_CONTENT_SANDBOX } from './uploadedContentSandbox'

describe('uploaded content sandbox', () => {
  it('never grants the frame canopy’s own origin', () => {
    const tokens = UPLOADED_CONTENT_SANDBOX.split(/\s+/)
    expect(tokens).toContain('allow-scripts')
    expect(tokens).not.toContain('allow-same-origin')
    expect(tokens).not.toContain('allow-top-navigation')
    expect(tokens).not.toContain('allow-forms')
  })

  // Every iframe in the app either uses the constant or is not showing uploaded
  // bytes. A hand-written `allow-same-origin` next to `allow-scripts` is how the
  // three viewers ended up running decks as the viewer, so it fails here.
  const sources = import.meta.glob('../**/*.tsx', {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>

  it('reads the source tree at all', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50)
  })

  it('no app iframe is sandboxed with allow-same-origin', () => {
    const offenders = Object.entries(sources)
      .filter(([, src]) => /sandbox=["'{][^>]*allow-same-origin/.test(src))
      .map(([path]) => path)
    expect(offenders).toEqual([])
  })

  it('the walkthrough, run-package and review frames use the shared sandbox', () => {
    for (const file of [
      '../pages/WalkthroughViewerPage.tsx',
      '../components/ddd/RunPackage.tsx',
      '../pages/ReviewPage.tsx',
    ]) {
      expect(sources[file], file).toBeDefined()
      expect(sources[file]).toContain('sandbox={UPLOADED_CONTENT_SANDBOX}')
    }
  })
})
