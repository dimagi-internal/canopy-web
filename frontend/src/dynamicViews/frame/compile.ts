/**
 * Turn a view's code (JSX defining `function View(props)`) into a component.
 *
 * Runs ONLY inside the frame: an opaque origin with `connect-src 'none'`, so the
 * `new Function` below runs code that has no cookies, no canopy DOM and no network.
 * Sucrase strips JSX (and any TypeScript annotations) to `React.createElement`
 * calls; the result is evaluated with React, its hooks and the kit passed in as
 * parameters — never as globals the page around it could share.
 */
import * as React from 'react'
import { transform } from 'sucrase'
import { KIT } from './kit'

export type ViewComponent = React.ComponentType<Record<string, unknown>>

const HOOKS = {
  useState: React.useState,
  useEffect: React.useEffect,
  useMemo: React.useMemo,
  useCallback: React.useCallback,
  useRef: React.useRef,
  useReducer: React.useReducer,
  Fragment: React.Fragment,
}

export function transpile(code: string): string {
  return transform(code, {
    transforms: ['jsx', 'typescript'],
    jsxRuntime: 'classic',
    production: true,
  }).code
}

export function compile(code: string, name: 'View' | 'Fill' = 'View'): ViewComponent {
  const js = transpile(code)
  const names = ['React', ...Object.keys(HOOKS), ...Object.keys(KIT)]
  const values = [React, ...Object.values(HOOKS), ...Object.values(KIT)]
  const body = `"use strict";\n${js}\n;return typeof ${name} === "undefined" ? undefined : ${name};`
  const factory = new Function(...names, body) as (...args: unknown[]) => unknown
  const View = factory(...values)
  if (typeof View !== 'function') {
    throw new Error(`the code does not define \`function ${name}(props)\``)
  }
  return View as ViewComponent
}

/** A workspace's slot fill (`function Fill(props)`), for the kit's <Slot>. */
export function compileFill(code: string): ViewComponent {
  return compile(code, 'Fill')
}
