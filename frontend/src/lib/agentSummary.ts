// What an agent is for, in one line a person can scan: its description without
// the parts every factory-made agent repeats.
//
// MIRRORS canopy's agent factory template (canopy
// packages/canopy_agent_factory/canopy_agent_factory/_factory.py, `_PLUGIN_JSON`
// "description"): "{{AGENT_NAME}} — {{MANDATE}} Built on the canopy agent
// operating model: …". If that template is reworded, update FACTORY_TEMPLATE in
// agentSummary.test.ts and this file together. A description that doesn't
// match is shown unchanged: the fallback is the raw text, never a guess.
//
// The operating-model sentence is the template's last; everything from it to
// the end is dropped (the full text stays available on hover).

const OPERATING_MODEL = /\s*Built on the canopy agent operating model\b.*$/s
const LEAD_SEPARATOR = /^\s*[—–-]\s*/

export function agentSummary(name: string, description: string): string {
  let text = (description ?? '').trim().replace(OPERATING_MODEL, '').trim()
  if (name && text.slice(0, name.length).toLowerCase() === name.toLowerCase()) {
    const rest = text.slice(name.length)
    const sep = rest.match(LEAD_SEPARATOR)
    if (sep) text = rest.slice(sep[0].length).trim()
  }
  return text
}
