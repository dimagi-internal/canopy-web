/**
 * Keep the previous builds' hashed assets in this build, so a page from the
 * previous build can still load its scripts.
 *
 * Every build renames its hashed files, and the container ships exactly one
 * `dist/`. During a rolling deploy the load balancer sends requests to old and
 * new tasks alike, so a page served by one task asks for `assets/index-<hash>.js`
 * and the request lands on the other, which never had that name: a 404 on the
 * entry script, and a black page with no app code running to recover. A tab left
 * open across a deploy meets the same 404 on its next lazy chunk.
 *
 * So each build also carries every asset a live build used in the last
 * KEEP_DAYS, and publishes the list as `asset-history.json` for the next build
 * to read. Two steps, because only the first one needs the network:
 *
 *   node scripts/carryAssets.mjs fetch <origin> <carriedDir>   (deploy workflow)
 *   node scripts/carryAssets.mjs merge <distDir> <carriedDir>  (Dockerfile, after `vite build`)
 *
 * Both fail open: with nothing fetched, the build is exactly what it was before.
 * `merge` runs after `vite build`, so carried files are never in the service
 * worker's precache — they are on the server only.
 */
import fs from 'node:fs'
import path from 'node:path'

export const HISTORY_FILE = 'asset-history.json'
export const KEEP_DAYS = 7

/** Every file under `<dist>/assets`, as `assets/<name>`. */
export function listAssets(distDir) {
  const dir = path.join(distDir, 'assets')
  if (!fs.existsSync(dir)) return []
  return fs.readdirSync(dir).map((name) => `assets/${name}`)
}

/**
 * The next history: this build's assets stamped `now`, plus every earlier entry
 * last used within KEEP_DAYS that we actually hold a copy of. A stamp is when a
 * live build last included the file, so a chunk that never changes stays as long
 * as builds keep shipping it.
 */
export function nextHistory({ current, previous, carried, now, keepDays = KEEP_DAYS }) {
  const cutoff = now - keepDays * 86_400
  const files = {}
  for (const [name, at] of Object.entries(previous ?? {})) {
    if (!isAssetPath(name) || !Number.isFinite(at) || at < cutoff) continue
    if (carried.has(name)) files[name] = at
  }
  for (const name of current) files[name] = now
  return { version: 1, files }
}

/** Only `assets/<one name>` — a history entry must never write elsewhere. */
export function isAssetPath(name) {
  return /^assets\/[A-Za-z0-9._-]+$/.test(name) && !name.includes('..')
}

/** Files a previous build's manifest names, for the first deploy with no history yet. */
export function manifestFiles(manifest) {
  const out = new Set()
  for (const entry of Object.values(manifest ?? {})) {
    for (const f of [entry.file, ...(entry.css ?? []), ...(entry.assets ?? [])]) {
      if (typeof f === 'string' && isAssetPath(f)) out.add(f)
    }
  }
  return out
}

async function getJson(url) {
  const res = await fetch(url, { signal: AbortSignal.timeout(15_000) })
  if (!res.ok) throw new Error(`${url}: ${res.status}`)
  return res.json()
}

async function fetchPrevious(origin, carriedDir) {
  const now = Math.floor(Date.now() / 1000)
  let previous = {}
  try {
    previous = (await getJson(`${origin}/${HISTORY_FILE}`)).files ?? {}
  } catch (e) {
    console.log(`no ${HISTORY_FILE} on ${origin} (${e.message}); falling back to its manifest`)
    try {
      for (const f of manifestFiles(await getJson(`${origin}/.vite/manifest.json`))) previous[f] = now
    } catch (e2) {
      console.log(`no manifest either (${e2.message}); carrying nothing`)
    }
  }
  const cutoff = now - KEEP_DAYS * 86_400
  const wanted = Object.entries(previous).filter(
    ([name, at]) => isAssetPath(name) && Number.isFinite(at) && at >= cutoff,
  )
  fs.mkdirSync(path.join(carriedDir, 'assets'), { recursive: true })
  const kept = {}
  let failed = 0
  // A few at a time: a few hundred small files, from the live site itself.
  for (let i = 0; i < wanted.length; i += 16) {
    await Promise.all(
      wanted.slice(i, i + 16).map(async ([name, at]) => {
        try {
          const res = await fetch(`${origin}/${name}`, { signal: AbortSignal.timeout(15_000) })
          if (!res.ok) throw new Error(String(res.status))
          fs.writeFileSync(path.join(carriedDir, name), Buffer.from(await res.arrayBuffer()))
          kept[name] = at
        } catch {
          failed += 1
        }
      }),
    )
  }
  fs.writeFileSync(path.join(carriedDir, HISTORY_FILE), JSON.stringify({ version: 1, files: kept }))
  console.log(`carried ${Object.keys(kept).length} earlier assets from ${origin} (${failed} unavailable)`)
}

function merge(distDir, carriedDir) {
  const now = Math.floor(Date.now() / 1000)
  const current = listAssets(distDir)
  const currentSet = new Set(current)
  let previous = {}
  try {
    previous = JSON.parse(fs.readFileSync(path.join(carriedDir, HISTORY_FILE), 'utf8')).files ?? {}
  } catch {
    /* nothing carried: a local build, or the fetch failed */
  }
  const carried = new Set()
  for (const name of Object.keys(previous)) {
    if (!isAssetPath(name) || currentSet.has(name)) continue
    const src = path.join(carriedDir, name)
    if (!fs.existsSync(src)) continue
    fs.copyFileSync(src, path.join(distDir, name))
    carried.add(name)
  }
  const history = nextHistory({ current, previous, carried, now })
  fs.writeFileSync(path.join(distDir, HISTORY_FILE), JSON.stringify(history))
  console.log(`${current.length} assets built, ${carried.size} carried from earlier builds`)
}

const [mode, a, b] = process.argv.slice(2)
if (mode === 'fetch') {
  await fetchPrevious(a.replace(/\/$/, ''), b).catch((e) => {
    console.log(`::warning::could not carry earlier assets: ${e.message}`)
  })
} else if (mode === 'merge') {
  merge(a, b)
}
