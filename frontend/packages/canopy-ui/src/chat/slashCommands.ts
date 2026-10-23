/** A command the composer offers as `/name`, from the host (canopy-web's
 *  `GET /canopy-sessions/{id}/slash-commands`). The kit never fetches it. */
export interface SlashCommand {
  name: string;
  description?: string;
  /** What the command takes, shown once its name is typed. */
  args_hint?: string;
  /** Where it came from: agent, user, plugin or acp. */
  source?: string;
}

/** How many rows the menu shows at once. */
export const SLASH_MENU_LIMIT = 8;

/** The name being typed, when the whole message so far is `/` plus a name and
 *  nothing after it — the only time the menu opens. Null otherwise. */
export function slashQuery(body: string): string | null {
  const m = /^\/(\S*)$/.exec(body);
  return m ? m[1] : null;
}

/** The commands matching `query`, best first: a name that starts with it, then
 *  one whose part after a `:` does (`turn` finds `hal:turn`), then one that
 *  contains it. Within each, the host's order is kept — it ranks the agent's own
 *  commands first. */
export function matchSlashCommands(
  commands: readonly SlashCommand[],
  query: string,
  limit = SLASH_MENU_LIMIT,
): SlashCommand[] {
  const q = query.toLowerCase();
  const tiers: SlashCommand[][] = [[], [], []];
  for (const c of commands) {
    const name = c.name.toLowerCase();
    if (name.startsWith(q)) tiers[0].push(c);
    else if (name.split(":").slice(1).some((part) => part.startsWith(q))) tiers[1].push(c);
    else if (q && name.includes(q)) tiers[2].push(c);
  }
  return tiers.flat().slice(0, limit);
}

/** The command a message invokes with nothing typed after it yet (`/hal:turn `),
 *  so the composer can show what it takes. */
export function pendingArgsCommand(
  body: string,
  commands: readonly SlashCommand[],
): SlashCommand | null {
  const m = /^\/(\S+) $/.exec(body);
  if (!m) return null;
  return commands.find((c) => c.name === m[1] && c.args_hint) ?? null;
}
