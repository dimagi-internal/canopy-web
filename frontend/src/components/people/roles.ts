// Shared, non-component pieces of the people/role controls (kept out of the
// .tsx files so React fast refresh keeps working on them).

export interface RoleOption {
  value: string
  label: string
}

/** Turn bare role strings into options (labels are capitalised by CSS). */
export function roleOptions(values: readonly string[]): RoleOption[] {
  return values.map((v) => ({ value: v, label: v }))
}
