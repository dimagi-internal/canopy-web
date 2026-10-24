/**
 * canopy's look, as the MCP Apps standard style variables (SEP-1865
 * `hostContext.styles.variables`), so a View draws in canopy's colours and font
 * rather than its own.
 *
 * The View runs in an opaque frame that cannot see canopy's CSS, so every value
 * is resolved here, from the page's own tokens (`--card`, `--primary`, …), to a
 * plain colour or font stack. A token the page does not define is left out, and
 * the View keeps its own default for it.
 */

/** Standard variable ← canopy token. The accent goes out as the `info` colours:
 *  the standard set has no "brand", and `info` is what a View uses for its
 *  selected and primary controls. */
const TOKENS: Record<string, string> = {
  "--color-background-primary": "--card",
  "--color-background-secondary": "--muted",
  "--color-background-tertiary": "--background",
  "--color-background-danger": "--destructive",
  "--color-background-success": "--success",
  "--color-background-warning": "--warning",
  "--color-text-primary": "--card-foreground",
  "--color-text-secondary": "--muted-foreground",
  "--color-text-tertiary": "--foreground-subtle",
  "--color-text-info": "--primary",
  "--color-text-danger": "--destructive",
  "--color-text-success": "--success",
  "--color-text-warning": "--warning",
  "--color-border-primary": "--border",
  "--color-ring-primary": "--ring",
  "--border-radius-md": "--radius",
};

const MONO = "ui-monospace, SFMono-Regular, Menlo, monospace";

export function hostStyleVariables(doc: Document | undefined = globalThis.document): Record<string, string> {
  if (!doc?.documentElement || typeof getComputedStyle !== "function") return {};
  const root = getComputedStyle(doc.documentElement);
  const read = (name: string) => root.getPropertyValue(name).trim();
  const out: Record<string, string> = {};
  for (const [standard, token] of Object.entries(TOKENS)) {
    const value = read(token);
    if (value) out[standard] = value;
  }
  // Derived: a soft wash of the accent for selected controls, and a fainter rule.
  const card = read("--card");
  const primary = read("--primary");
  const border = read("--border");
  if (card && primary) out["--color-background-info"] = `color-mix(in oklch, ${primary} 18%, ${card})`;
  if (card && border) out["--color-border-secondary"] = `color-mix(in oklch, ${border} 55%, ${card})`;
  const sans = read("--font-sans") || (doc.body ? getComputedStyle(doc.body).fontFamily : "");
  if (sans) out["--font-sans"] = `${sans}, system-ui, -apple-system, sans-serif`;
  out["--font-mono"] = read("--font-mono") || MONO;
  return out;
}
