import { apiV2 } from './client.v2'
import type { components } from './generated'

export type PersonMe = components['schemas']['PersonMeOut']
export type PersonFactDetail = components['schemas']['PersonFactDetailOut']

/** Everything canopy holds about the signed-in user (fleet brain, canopy#804). */
export async function getMyPerson(): Promise<PersonMe> {
  const { data, error } = await apiV2.GET('/api/people/me/')
  if (error) throw new Error('Failed to load what agents know about you')
  return data as PersonMe
}

export async function retractFact(personId: number, factId: number): Promise<void> {
  const { error } = await apiV2.POST('/api/people/{person_id}/facts/{fact_id}/retract/', {
    params: { path: { person_id: personId, fact_id: factId } },
  })
  if (error) throw new Error('Failed to retract that fact')
}
