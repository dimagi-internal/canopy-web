// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { compile } from './compile'

afterEach(cleanup)

describe('compile', () => {
  it('turns JSX defining View into a component that gets the kit and hooks as globals', () => {
    const View = compile(`
      function View({ data }) {
        const [n] = useState(data.items.length);
        return <Card title="Open"><Stat label="Tasks" value={n} /><Badge tone="success">ok</Badge></Card>;
      }
    `)
    render(<View data={{ items: [1, 2, 3] }} />)
    expect(screen.getByText('Open')).toBeTruthy()
    expect(screen.getByText('3')).toBeTruthy()
    expect(screen.getByText('ok')).toBeTruthy()
  })

  it('accepts TypeScript annotations and `const View =`', () => {
    const View = compile(`const View = ({ scope }: { scope: { workspace: { name: string } } }) => <p>{scope.workspace.name}</p>;`)
    render(<View scope={{ workspace: { name: 'Dimagi' } }} />)
    expect(screen.getByText('Dimagi')).toBeTruthy()
  })

  it('refuses code that defines no View, and reports a syntax error', () => {
    expect(() => compile('function Other() { return null }')).toThrow(/does not define/)
    expect(() => compile('function View( { return <div> }')).toThrow()
  })
})
