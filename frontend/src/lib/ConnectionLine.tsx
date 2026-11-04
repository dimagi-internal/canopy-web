import type { LineWords } from './connectionLines'

/** One declared call in plain words; one nothing recognises keeps its own spelling, as code. */
export function ConnectionLine({ line }: { line: LineWords }) {
  if (!line.code) return <>{line.words}</>
  return <>{line.words} <span className="font-mono">{line.code}</span></>
}
