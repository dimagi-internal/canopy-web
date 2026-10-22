import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

export type RunnerAppPage = components['schemas']['RunnerAppPreferenceOut']['opens_on']

/** Normalized from the generated `RunnerAppPreferenceOut`, whose nested readonly
 *  array does not survive an inferred return type (TS2719 under `tsc -b`). */
export type RunnerAppPreference = {
  opens_on: RunnerAppPage
  choices: { value: RunnerAppPage; label: string }[]
}

function normalize(data: {
  opens_on: RunnerAppPage
  choices: ReadonlyArray<{ value: RunnerAppPage; label: string }>
}): RunnerAppPreference {
  return { opens_on: data.opens_on, choices: data.choices.map((c) => ({ value: c.value, label: c.label })) }
}

export async function getRunnerAppPreference(): Promise<RunnerAppPreference> {
  const res = await apiV2.GET('/api/me/runner-app/')
  const data = unwrap(res, 'Failed to load the runner app setting')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}

export async function setRunnerAppPreference(opensOn: RunnerAppPage): Promise<RunnerAppPreference> {
  const res = await apiV2.PATCH('/api/me/runner-app/', { body: { opens_on: opensOn } })
  const data = unwrap(res, 'Failed to save the runner app setting')
  return normalize(data as unknown as Parameters<typeof normalize>[0])
}
