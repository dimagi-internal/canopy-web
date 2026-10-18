// Plain words for a machine on Settings > Security (canopy-web#1550).
import { describe, expect, it } from 'vitest'
import { canOpenText, keyPlace, machineDescription, machineShortName } from './machineText'

const base = { runner_name: 'r', runner_host: '', runner_kind: 'emdash', kind: 'secure-enclave' as const,
  ecdh_store: '', can_open: [] as { slug: string; name: string }[] }

describe('machineText', () => {
  it.each([
    ['acedimagi@Jonathans-MacBook-Pro.local', 'MacBook Pro, acedimagi account', 'MacBook (acedimagi)'],
    ['haldimagi@Jonathans-MBP.localdomain', 'MacBook Pro, haldimagi account', 'MacBook (haldimagi)'],
    ['sam@Sams-MacBook-Air.local', 'MacBook Air, sam account', 'MacBook (sam)'],
    ['jj@studio.local', 'Mac, jj account', 'Mac (jj)'],
    ['', 'Mac', 'r'],
  ])('names a Mac from its host %s', (host, long, short) => {
    expect(machineDescription({ ...base, runner_host: host })).toBe(long)
    expect(machineShortName({ ...base, runner_host: host })).toBe(short)
  })

  it('calls a cloud box an AWS server and uses its runner name', () => {
    const k = { ...base, runner_name: 'cloud-ec2-2', runner_host: 'cloud-ec2-2', runner_kind: 'cloud', kind: 'aws-iam' as const }
    expect(machineDescription(k)).toBe('AWS server')
    expect(machineShortName(k)).toBe('cloud-ec2-2')
  })

  it('says where the key lives, from what the box reported or its kind', () => {
    expect(keyPlace(base)).toBe('key locked in the security chip')
    expect(keyPlace({ ...base, kind: 'tpm' })).toBe('key locked in the TPM chip')
    expect(keyPlace({ ...base, kind: 'aws-iam', ecdh_store: 'aws-kms' })).toBe('key held by AWS')
    expect(keyPlace({ ...base, kind: 'aws-iam', ecdh_store: 'software' })).toBe('key stored in a file')
    expect(keyPlace({ ...base, kind: 'aws-iam' })).toBe('key kept on the server') // not said: no claim
    expect(keyPlace({ ...base, kind: 'software' })).toBe('key stored in a file')
  })

  it('lists what it can open, or says nothing', () => {
    expect(canOpenText(base)).toBe('')
    expect(canOpenText({ ...base, can_open: [{ slug: 'hal', name: 'Hal' }, { slug: 'eva', name: 'Eva' }] }))
      .toBe('can open Hal, Eva')
  })
})
