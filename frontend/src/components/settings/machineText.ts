import type { RunnerDeviceKeyOut } from '@/api/deviceKeys'

/**
 * Plain words for a machine on Settings > Security (canopy-web#1550): which
 * machine it is, where its key lives, which agents it can open. People see a
 * runner as "the MacBook" or "the AWS server", never as a key kind or a
 * thumbprint.
 */

type Key = Pick<RunnerDeviceKeyOut, 'runner_name' | 'runner_host' | 'runner_kind' | 'kind' | 'ecdh_store' | 'can_open'>

/** The model, from a Mac's host name ("Jonathans-MacBook-Pro.local"). */
function macModel(machine: string): string {
  const m = machine.toLowerCase()
  if (/macbook-?pro|\bmbp\b|-mbp\b|^mbp/.test(m)) return 'MacBook Pro'
  if (/macbook-?air|\bmba\b/.test(m)) return 'MacBook Air'
  if (/macbook/.test(m)) return 'MacBook'
  if (/imac/.test(m)) return 'iMac'
  if (/mac-?mini|macmini/.test(m)) return 'Mac mini'
  if (/mac-?studio/.test(m)) return 'Mac Studio'
  return 'Mac'
}

function isCloud(k: Key): boolean {
  return k.runner_kind === 'cloud' || k.kind === 'aws-iam'
}

function hostParts(k: Key): { account: string; machine: string } {
  const host = k.runner_host || ''
  const at = host.indexOf('@')
  return at < 0 ? { account: '', machine: host } : { account: host.slice(0, at), machine: host.slice(at + 1) }
}

function family(k: Key): string {
  const { machine } = hostParts(k)
  if (k.kind === 'tpm') return 'Windows PC'
  if (k.kind === 'secure-enclave' || /mac|mbp|imac/i.test(machine)) return macModel(machine)
  return 'Computer'
}

/** "MacBook Pro, acedimagi account" / "AWS server". */
export function machineDescription(k: Key): string {
  if (isCloud(k)) return 'AWS server'
  const { account } = hostParts(k)
  return account ? `${family(k)}, ${account} account` : family(k)
}

/** Short, for a list of who can open an agent: "MacBook (acedimagi)" / "cloud-ec2-2". */
export function machineShortName(k: Key): string {
  if (isCloud(k)) return k.runner_name
  const { account } = hostParts(k)
  const word = family(k).replace(/ (Pro|Air)$/, '')
  return account ? `${word} (${account})` : k.runner_name
}

/** Where the machine's key lives, which decides how hard it is to steal. */
export function keyPlace(k: Key): string {
  return storePlace(k.ecdh_store || (k.kind === 'aws-iam' ? '' : k.kind))
}

/** The same, from a key's store alone (a vault's runner row). */
export function storePlace(store: string): string {
  switch (store) {
    case 'secure-enclave':
      return 'key locked in the security chip'
    case 'tpm':
      return 'key locked in the TPM chip'
    case 'aws-kms':
    case 'kms-broker':  // a box of units: each unit's key, served by the box's KMS broker
      return 'key held by AWS'
    case 'software':
      return 'key stored in a file'
    default:
      return 'key kept on the server'
  }
}

/** "can open Hal, Eva" — or nothing to say. */
export function canOpenText(k: Key): string {
  const names = (k.can_open ?? []).map((a) => a.name)
  return names.length ? `can open ${names.join(', ')}` : ''
}
