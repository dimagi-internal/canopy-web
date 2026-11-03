// What an agent is for, in one line a person can scan: its description without
// the parts every factory-made agent repeats. `canopy create-agent` writes
// "<Name> — <mandate> Built on the canopy agent operating model: …", so the
// name (already in the row) and the operating-model sentence (true of every
// agent) are dropped and only the mandate is left.

const OPERATING_MODEL = /\s*Built on the canopy agent operating model\b.*$/s

export function agentSummary(name: string, description: string): string {
  let text = (description || '').trim().replace(OPERATING_MODEL, '').trim()
  const lead = `${name} — `
  if (name && text.toLowerCase().startsWith(lead.toLowerCase())) text = text.slice(lead.length).trim()
  return text
}
