import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // React Compiler rules, from react-hooks v7's recommended set. This app
      // does not run the compiler, and they flag patterns used on purpose
      // throughout it: the latest-value ref (`ref.current = fn` in render), a
      // reset-on-prop-change `setState` in an effect, a reconnect callback that
      // names itself. None of what they reported here was a bug.
      'react-hooks/set-state-in-effect': 'off',
      'react-hooks/immutability': 'off',
      'react-hooks/refs': 'off',
      'react-hooks/purity': 'off',
      // A Vite fast-refresh hint (a module exporting a helper beside a
      // component reloads fully in dev); nothing to do with correctness.
      'react-refresh/only-export-components': 'off',
    },
  },
])
