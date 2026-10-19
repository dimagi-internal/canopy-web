import { apiV2 } from './client.v2'
import type { components } from './generated'

export type RunnerAppPreference = components['schemas']['RunnerAppPreferenceOut']
export type RunnerAppPage = RunnerAppPreference['opens_on']

export async function getRunnerAppPreference(): Promise<RunnerAppPreference> {
  const { data, error } = await apiV2.GET('/api/me/runner-app/')
  if (error) throw new Error('Failed to load the runner app setting')
  return data
}

export async function setRunnerAppPreference(opensOn: RunnerAppPage): Promise<RunnerAppPreference> {
  const { data, error } = await apiV2.PATCH('/api/me/runner-app/', { body: { opens_on: opensOn } })
  if (error) throw new Error('Failed to save the runner app setting')
  return data
}
