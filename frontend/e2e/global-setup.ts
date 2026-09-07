import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const here = path.dirname(fileURLToPath(import.meta.url))
const dir = path.join(here, '.auth')
const sessionFile = path.join(dir, 'session.txt')
const stateFile = path.join(dir, 'state.json')
// The SECOND identity. Multiplayer is only observable between two different
// users — "nobody else here", "Another teammate is editing…" and "take over"
// are all statements about somebody who is not you — so one storageState can
// open two browsers and still exercise none of it.
const session2File = path.join(dir, 'session2.txt')
const state2File = path.join(dir, 'state2.json')

// Wait for backend.sh to seed + mint the session, then write a Playwright
// storageState carrying the session cookie so API calls are authenticated.
const stateFor = (key: string) => ({
  cookies: [{
    name: 'sessionid', value: key, domain: 'localhost', path: '/',
    expires: Math.floor(Date.now() / 1000) + 86400,
    httpOnly: true, secure: false, sameSite: 'Lax' as const,
  }],
  origins: [],
})

async function waitForKey(file: string, label: string): Promise<string> {
  for (let i = 0; i < 120; i++) {
    if (fs.existsSync(file) && fs.readFileSync(file, 'utf8').trim()) break
    await new Promise((r) => setTimeout(r, 1000))
  }
  const key = fs.existsSync(file) ? fs.readFileSync(file, 'utf8').trim() : ''
  if (!key) throw new Error(`no ${label} minted by backend seed`)
  return key
}

export default async function globalSetup() {
  fs.writeFileSync(stateFile, JSON.stringify(stateFor(await waitForKey(sessionFile, 'session key'))))
  fs.writeFileSync(state2File, JSON.stringify(stateFor(await waitForKey(session2File, 'second session key'))))
}
