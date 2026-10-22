const DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

const NUMERIC = /^\d+$/;

/** Friendly rendering for the common cron shapes; falls back to the raw
 * expression, which is always correct if not always pretty. Naming a cadence
 * wrongly would be worse than not naming it. This is a LABEL, never a schedule
 * computation — "when does this actually fire?" is always answered by the
 * server (see the editor's debounced preview).
 *
 * TOTAL by contract: never throws for ANY string. The server accepts far more
 * than the shapes named here (`@daily`, steps, ranges, lists, 6-field), all of
 * which reach this function via a saved row — and a throw here white-screens
 * the app with no ErrorBoundary to catch it and no way to delete the row.
 * Anything not matched exactly falls back. */
export function describeCron(cron: string, tz: string): string {
  const zone = tz.split("/").pop()?.replace(/_/g, " ") ?? tz;
  const fallback = `${cron} · ${zone}`;

  // Arity first: `@daily` is one field, and a 6-field expression means the
  // fields aren't where we think. Either way, don't guess.
  const fields = cron.trim().split(/\s+/);
  if (fields.length !== 5) return fallback;
  const [min, hour, dom, mon, dow] = fields;

  // Every named shape fires at exactly ONE time of day, so both min and hour
  // must be plain numbers. A step/range/list in either ("*/15", "0,30") means
  // the cadence is not the one we'd be about to name.
  if (!NUMERIC.test(min) || !NUMERIC.test(hour)) return fallback;
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;

  if (dom === "*" && mon === "*" && /^[0-6]$/.test(dow)) {
    return `${DAYS[Number(dow)]} at ${time} · ${zone}`;
  }
  if (dow === "*" && mon === "*" && NUMERIC.test(dom)) {
    return `Day ${dom} monthly at ${time} · ${zone}`;
  }
  if (dom === "*" && mon === "*" && dow === "*") return `Daily at ${time} · ${zone}`;
  return fallback;
}
