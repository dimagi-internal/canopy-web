// The public application portal: every open, live, non-confidential job (the
// job collection's public projection), grouped by team, with search and filters
// for location, workplace and type. Each card shows the pay range, which several
// US states require on every posting. A `?src=` tag on the portal's own link is
// carried into the job page so the application records where it came from.
const FOCUS = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2'

function payRange(j, app) {
  const min = j.pay_min == null || j.pay_min === '' ? null : Number(j.pay_min)
  const max = j.pay_max == null || j.pay_max === '' ? null : Number(j.pay_max)
  if (min == null && max == null) return ''
  const n = (v) => v.toLocaleString('en-US')
  const range = min != null && max != null && min !== max ? `${n(min)}–${n(max)}` : n(min ?? max)
  const period = j.pay_period || 'YEAR'
  const label = app.optionLabel('job', 'pay_period', period)
  return `${j.pay_currency || 'USD'} ${range} / ${label && label !== period ? label : period.toLowerCase()}`
}

function uniq(list) {
  return [...new Set(list.filter(Boolean))].sort((a, b) => String(a).localeCompare(String(b)))
}

function View({ data, app }) {
  const s = app.settings || {}
  const today = new Date().toISOString().slice(0, 10)
  const all = Array.isArray(data.jobs) ? data.jobs : []
  // A job whose closing date has passed is no longer advertised.
  const jobs = all.filter((j) => !j.valid_through || String(j.valid_through) >= today)
  const src = app.params?.src
  const [q, setQ] = useState('')
  const [loc, setLoc] = useState('')
  const [workplace, setWorkplace] = useState('')
  const [type, setType] = useState('')

  const shown = jobs.filter((j) => {
    if (loc && j.location !== loc) return false
    if (workplace && j.workplace !== workplace) return false
    if (type && j.employment_type !== type) return false
    if (!q) return true
    const hay = `${j.title} ${j.department} ${j.office} ${j.location} ${j.country}`.toLowerCase()
    return q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => hay.includes(w))
  })
  const teams = {}
  for (const j of shown) (teams[j.department || 'Other roles'] ||= []).push(j)
  const filtered = q || loc || workplace || type
  const choice = (label, values, collection, field) => [
    { value: '', label: `Any ${label}` },
    ...values.map((v) => ({ value: v, label: field ? app.optionLabel(collection, field, v) || v : v })),
  ]

  return (
    <div className="min-h-full bg-background">
      <div aria-hidden="true" className="h-1.5 w-full" style={{ background: s.brand_color }} />
      <main className="mx-auto max-w-3xl px-4 py-8 sm:py-12">
        <header className="mb-8">
          <p className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">{s.company_name}</p>
          <h1 className="mt-1 text-3xl font-semibold text-foreground">Careers at {s.company_name}</h1>
          {s.careers_intro && <p className="mt-3 whitespace-pre-line leading-relaxed text-muted-foreground">{s.careers_intro}</p>}
        </header>
        <Slot name="header" jobs={jobs} />

        {jobs.length === 0 ? (
          <div className="rounded-lg border border-border bg-card p-6 text-center">
            <h2 className="text-lg font-semibold">No open roles right now</h2>
            <p className="mt-1 text-sm text-muted-foreground">Check back soon — new roles are posted here first.</p>
          </div>
        ) : (
          <>
            <section aria-label="Find a role" className="mb-6 rounded-lg border border-border bg-card p-4">
              <Stack gap={3}>
                <Input label="Search roles" value={q} onChange={setQ} placeholder="Title, team, location or country" />
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                  <Select label="Location" value={loc} onChange={setLoc} options={choice('location', uniq(jobs.map((j) => j.location)))} />
                  <Select label="Workplace" value={workplace} onChange={setWorkplace}
                    options={choice('workplace', uniq(jobs.map((j) => j.workplace)), 'job', 'workplace')} />
                  <Select label="Type" value={type} onChange={setType}
                    options={choice('type', uniq(jobs.map((j) => j.employment_type)), 'job', 'employment_type')} />
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-sm text-muted-foreground" role="status" aria-live="polite">
                    {shown.length === jobs.length
                      ? `${jobs.length} open role${jobs.length === 1 ? '' : 's'}`
                      : `${shown.length} of ${jobs.length} open roles match`}
                  </p>
                  {filtered && (
                    <button type="button" className={`rounded text-sm text-primary underline-offset-2 hover:underline ${FOCUS}`}
                      onClick={() => { setQ(''); setLoc(''); setWorkplace(''); setType('') }}>Clear filters</button>
                  )}
                </div>
              </Stack>
            </section>

            {shown.length === 0 && (
              <p className="rounded-lg border border-border bg-card p-6 text-center text-sm text-muted-foreground">
                No roles match these filters.
              </p>
            )}
            <Stack gap={8}>
              {Object.keys(teams).sort().map((team) => (
                <section key={team} aria-labelledby={`team-${team}`}>
                  <h2 id={`team-${team}`} className="mb-2 flex items-center gap-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                    <span aria-hidden="true" className="inline-block h-3 w-1 rounded" style={{ background: s.brand_color }} />
                    {team} <span className="font-normal normal-case">({teams[team].length})</span>
                  </h2>
                  <ul className="overflow-hidden rounded-lg border border-border bg-card">
                    {teams[team].sort((a, b) => String(a.title).localeCompare(String(b.title))).map((j) => {
                      const pay = payRange(j, app)
                      const meta = uniq([j.location, j.workplace && app.optionLabel('job', 'workplace', j.workplace),
                        j.employment_type && app.optionLabel('job', 'employment_type', j.employment_type)])
                      return (
                        <li key={j.id} className="border-b border-border last:border-0">
                          <button type="button" onClick={() => app.go('job', { id: j.id, src })}
                            className={`flex w-full flex-col gap-1 px-4 py-3 text-left hover:bg-muted sm:flex-row sm:items-center sm:justify-between sm:gap-4 ${FOCUS}`}>
                            <span className="min-w-0">
                              <span className="block font-medium text-foreground">{j.title}</span>
                              <span className="block text-sm text-muted-foreground">{meta.join(' · ')}</span>
                            </span>
                            {pay && <span className="shrink-0 text-sm tabular-nums text-foreground">{pay}</span>}
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </section>
              ))}
            </Stack>
          </>
        )}
        <p className="mt-10 text-sm text-muted-foreground">
          Need an accommodation to apply? Email <strong className="select-all font-medium text-foreground">{s.accommodation_contact}</strong>.
        </p>
      </main>
    </div>
  )
}
