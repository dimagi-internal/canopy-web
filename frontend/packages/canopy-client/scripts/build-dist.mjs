// Build the PUBLISHED form of canopy-client into dist/, and publish from there.
//
// Why a build at all: until 0.2.0 this package shipped its TypeScript source
// (`exports` -> ./src/index.ts), which only works for a consumer whose bundler
// compiles TypeScript inside node_modules. Vite does, which is why ace-web and
// canopy-web never noticed. connect-labs' webpack does not — babel runs with
// `exclude: /node_modules/` — and connect-labs is the host this package was
// extracted FOR. So 0.1.0 was unusable by exactly its intended user.
//
// Why publish from dist/ rather than point package.json at it: the workspace
// keeps consuming src/ directly, so canopy-web needs no build step before its
// own dev server, tests or type check. Only the artifact on npm changes.
//
// Usage: node scripts/build-dist.mjs [outDir]   (default: dist)
import { execFileSync } from 'node:child_process'
import { copyFileSync, existsSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PKG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUT = resolve(PKG_DIR, process.argv[2] ?? 'dist')
const TSC = resolve(PKG_DIR, '..', '..', 'node_modules', 'typescript', 'bin', 'tsc')

rmSync(OUT, { recursive: true, force: true })
execFileSync(process.execPath, [TSC, '-p', join(PKG_DIR, 'tsconfig.build.json'), '--outDir', OUT], {
  stdio: 'inherit',
})

// Plain ESM needs explicit extensions on relative imports; a bundler does not
// care either way, and Node refuses without them. tsc emits the specifier as
// written in the source ('./token'), so add `.js` here — in both the JS and the
// declarations, or a TypeScript consumer on `moduleResolution: node16` cannot
// follow the types.
const RELATIVE = /(from\s+['"])(\.{1,2}\/[^'"]+?)(['"])/g
for (const file of readdirSync(OUT)) {
  if (!file.endsWith('.js') && !file.endsWith('.d.ts')) continue
  const path = join(OUT, file)
  const text = readFileSync(path, 'utf8')
  writeFileSync(path, text.replace(RELATIVE, (_m, a, spec, b) => `${a}${spec.endsWith('.js') ? spec : `${spec}.js`}${b}`))
}

// The published manifest: same identity, exports pointed at the build.
const source = JSON.parse(readFileSync(join(PKG_DIR, 'package.json'), 'utf8'))
const entry = (name) => ({ types: `./${name}.d.ts`, import: `./${name}.js`, default: `./${name}.js` })
const published = {
  ...source,
  exports: { '.': entry('index'), './bridge': entry('bridge'), './package.json': './package.json' },
  main: './index.js',
  types: './index.d.ts',
  sideEffects: false,
}
delete published.files // everything in dist/ is the package
delete published.scripts
writeFileSync(join(OUT, 'package.json'), `${JSON.stringify(published, null, 2)}\n`)

for (const doc of ['README.md']) {
  if (existsSync(join(PKG_DIR, doc))) copyFileSync(join(PKG_DIR, doc), join(OUT, doc))
}
console.log(`canopy-client ${source.version} built into ${OUT}`)
