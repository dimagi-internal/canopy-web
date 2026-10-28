// One open job and its application form (?id, ?src). Public: it reads the job
// through the public projection and submits `apply`, which only creates. The
// country an applicant lives in sets their privacy region, which decides the
// retention rule; US applicants may answer the voluntary self-ID questions,
// which only a compliance role can ever read. Questions the workspace adds to
// `apply` appear in the form by themselves.
const BUILT_IN = ['job', 'name', 'email', 'phone', 'location', 'country', 'region', 'linkedin', 'resume', 'resume_url',
  'cover_letter', 'source_tag', 'privacy_notice', 'consent_pool', 'eeo_gender', 'eeo_race', 'eeo_veteran', 'eeo_disability']
const EEO = [
  ['eeo_gender', 'Gender'],
  ['eeo_race', 'Race / ethnicity'],
  ['eeo_veteran', 'Veteran status'],
  ['eeo_disability', 'Disability'],
]
const EU_UK = new Set(('Austria|Belgium|Bulgaria|Croatia|Cyprus|Czechia|Denmark|Estonia|Finland|France|Germany|Greece|Hungary|'
  + 'Ireland|Italy|Latvia|Lithuania|Luxembourg|Malta|Netherlands|Poland|Portugal|Romania|Slovakia|Slovenia|Spain|Sweden|'
  + 'United Kingdom|Iceland|Liechtenstein|Norway|Switzerland').split('|'))
const COUNTRIES = ('Afghanistan|Albania|Algeria|Andorra|Angola|Antigua and Barbuda|Argentina|Armenia|Australia|Austria|Azerbaijan|'
  + 'Bahamas|Bahrain|Bangladesh|Barbados|Belarus|Belgium|Belize|Benin|Bhutan|Bolivia|Bosnia and Herzegovina|Botswana|Brazil|'
  + 'Brunei|Bulgaria|Burkina Faso|Burundi|Cabo Verde|Cambodia|Cameroon|Canada|Central African Republic|Chad|Chile|China|'
  + 'Colombia|Comoros|Congo|Costa Rica|Côte d\'Ivoire|Croatia|Cuba|Cyprus|Czechia|Democratic Republic of the Congo|Denmark|'
  + 'Djibouti|Dominica|Dominican Republic|Ecuador|Egypt|El Salvador|Equatorial Guinea|Eritrea|Estonia|Eswatini|Ethiopia|Fiji|'
  + 'Finland|France|Gabon|Gambia|Georgia|Germany|Ghana|Greece|Grenada|Guatemala|Guinea|Guinea-Bissau|Guyana|Haiti|Honduras|'
  + 'Hungary|Iceland|India|Indonesia|Iran|Iraq|Ireland|Israel|Italy|Jamaica|Japan|Jordan|Kazakhstan|Kenya|Kiribati|Kosovo|'
  + 'Kuwait|Kyrgyzstan|Laos|Latvia|Lebanon|Lesotho|Liberia|Libya|Liechtenstein|Lithuania|Luxembourg|Madagascar|Malawi|'
  + 'Malaysia|Maldives|Mali|Malta|Marshall Islands|Mauritania|Mauritius|Mexico|Micronesia|Moldova|Monaco|Mongolia|Montenegro|'
  + 'Morocco|Mozambique|Myanmar|Namibia|Nauru|Nepal|Netherlands|New Zealand|Nicaragua|Niger|Nigeria|North Korea|'
  + 'North Macedonia|Norway|Oman|Pakistan|Palau|Palestine|Panama|Papua New Guinea|Paraguay|Peru|Philippines|Poland|Portugal|'
  + 'Qatar|Romania|Russia|Rwanda|Saint Kitts and Nevis|Saint Lucia|Saint Vincent and the Grenadines|Samoa|San Marino|'
  + 'São Tomé and Príncipe|Saudi Arabia|Senegal|Serbia|Seychelles|Sierra Leone|Singapore|Slovakia|Slovenia|Solomon Islands|'
  + 'Somalia|South Africa|South Korea|South Sudan|Spain|Sri Lanka|Sudan|Suriname|Sweden|Switzerland|Syria|Taiwan|Tajikistan|'
  + 'Tanzania|Thailand|Timor-Leste|Togo|Tonga|Trinidad and Tobago|Tunisia|Turkey|Turkmenistan|Tuvalu|Uganda|Ukraine|'
  + 'United Arab Emirates|United Kingdom|United States|Uruguay|Uzbekistan|Vanuatu|Vatican City|Venezuela|Vietnam|Yemen|'
  + 'Zambia|Zimbabwe').split('|')
const FOCUS = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2'

function regionOf(country) {
  if (!country) return 'Other'
  if (country === 'United States') return 'US'
  if (EU_UK.has(country)) return 'EU/UK'
  if (['India', 'Kenya', 'South Africa'].includes(country)) return country
  return 'Other'
}

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

function choices(app, name) {
  const opts = app.options('application', name)
  if (opts.length) return opts
  return (app.field('application', name)?.options || []).map((v) => ({ value: v, label: v }))
}

function Fact({ label, children }) {
  if (!children) return null
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm text-foreground">{children}</dd>
    </div>
  )
}

function View({ app }) {
  const id = Number(app.params?.id)
  const src = app.params?.src ? String(app.params.src).slice(0, 80) : ''
  const s = app.settings || {}
  const [job, setJob] = useState(undefined)
  const [form, setForm] = useState({ consent_pool: false })
  const [trap, setTrap] = useState('')
  const [state, setState] = useState({ kind: 'idle' })
  const doneRef = useRef(null)

  useEffect(() => {
    if (!id) { setJob(null); return }
    app.query('job', { where: { id } }).then((rows) => setJob(rows[0] || null), () => setJob(null))
  }, [id])
  useEffect(() => { if (state.kind === 'sent') doneRef.current?.focus() }, [state.kind])

  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const usSelfId = form.country === 'United States' && s.ask_self_id !== false
  const allowed = String(job?.country || '').split(',').map((c) => c.trim()).filter(Boolean)
  const countryOptions = [
    { value: '', label: 'Choose a country…' },
    ...allowed.filter((c) => COUNTRIES.includes(c)).map((c) => ({ value: c, label: c })),
    ...(allowed.length ? [{ value: '—', label: '──────────' }] : []),
    ...COUNTRIES.filter((c) => !allowed.includes(c)).map((c) => ({ value: c, label: c })),
  ]

  async function submit() {
    // The hidden "website" field is a trap for bots: a person never sees it. Pretend it worked.
    if (trap) { setState({ kind: 'sent' }); return }
    setState({ kind: 'sending' })
    const input = { job: id, region: regionOf(form.country), privacy_notice: s.privacy_notice_version, consent_pool: form.consent_pool === true }
    if (src) input.source_tag = src
    for (const [k, v] of Object.entries(form)) {
      if (v === '' || v == null || k === 'consent_pool') continue
      if (k.startsWith('eeo_') && !usSelfId) continue
      input[k] = v && typeof v === 'object' && 'id' in v ? v.id : v
    }
    try {
      await app.act('apply', { input })
      setState({ kind: 'sent' })
    } catch (err) {
      setState({ kind: 'failed', message: err?.message || 'Your application could not be sent. Please try again.' })
    }
  }

  if (job === undefined) return <div className="mx-auto max-w-3xl px-4 py-10"><Loading /></div>
  const closed = job && job.valid_through && String(job.valid_through) < new Date().toISOString().slice(0, 10)
  if (job === null) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-10">
        <h1 className="text-2xl font-semibold">This role is no longer open</h1>
        <p className="mt-2 text-muted-foreground">It may have been filled or taken down.</p>
        <div className="mt-4"><Button onClick={() => app.go('careers', { src: src || undefined })}>See open roles at {s.company_name}</Button></div>
      </main>
    )
  }
  const pay = payRange(job, app)
  const paragraphs = String(job.description || '').split(/\n\s*\n/).map((p) => p.trim()).filter(Boolean)

  return (
    <div className="min-h-full bg-background">
      <div aria-hidden="true" className="h-1.5 w-full" style={{ background: s.brand_color }} />
      <main className="mx-auto max-w-3xl px-4 py-8 sm:py-10">
        <button type="button" className={`mb-4 rounded text-sm text-muted-foreground hover:text-foreground ${FOCUS}`}
          onClick={() => app.go('careers', { src: src || undefined })}>← All roles at {s.company_name}</button>
        <h1 className="text-3xl font-semibold text-foreground">{job.title}</h1>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 rounded-lg border border-border bg-card p-4 sm:grid-cols-3">
          <Fact label="Team">{job.department}</Fact>
          <Fact label="Location">{[job.location, job.office].filter(Boolean).join(' · ')}</Fact>
          <Fact label="Countries you may live in">{allowed.join(', ')}</Fact>
          <Fact label="Workplace">{job.workplace && app.optionLabel('job', 'workplace', job.workplace)}</Fact>
          <Fact label="Type">{job.employment_type && app.optionLabel('job', 'employment_type', job.employment_type)}</Fact>
          <Fact label="Pay range">{pay}</Fact>
          <Fact label="Applications close">{job.valid_through}</Fact>
        </dl>
        <FieldValues collection="job" record={job} only="extensions" />

        <section aria-label="About the role" className="mt-6 space-y-4 leading-relaxed text-foreground">
          {paragraphs.map((p, i) => <p key={i} className="whitespace-pre-line">{p}</p>)}
        </section>
        <Slot name="below_description" job={job} />
        <p className="mt-6 rounded-lg border border-border bg-muted p-3 text-sm">
          Need an accommodation to apply? Email <strong className="select-all font-medium">{s.accommodation_contact}</strong>.
        </p>

        <section aria-labelledby="apply-h" className="mt-10 rounded-lg border border-border bg-card p-4 sm:p-6">
          <h2 id="apply-h" className="mb-4 text-xl font-semibold">Apply for this role</h2>
          {closed ? (
            <p className="text-muted-foreground">Applications for this role closed on {job.valid_through}.</p>
          ) : state.kind === 'sent' ? (
            <div role="status" className="py-6 text-center">
              <h3 ref={doneRef} tabIndex={-1} className="text-lg font-semibold focus:outline-none">Thank you — your application is in.</h3>
              <p className="mt-1 text-sm text-muted-foreground">We read every application for {job.title} and will be in touch by email.</p>
              <div className="mt-4"><Button onClick={() => app.go('careers', { src: src || undefined })}>See other roles</Button></div>
            </div>
          ) : (
            <Form onSubmit={submit}>
              <Stack gap={4}>
                <p className="text-sm text-muted-foreground">Fields marked * are required.</p>
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <Input label="Full name *" required value={form.name} onChange={set('name')} />
                  <Input label="Email *" type="email" required value={form.email} onChange={set('email')} />
                  <Input label="Phone" type="tel" value={form.phone} onChange={set('phone')} />
                  <Input label="Where are you based? (city)" value={form.location} onChange={set('location')} />
                  <Select label="Country you live in" value={form.country}
                    onChange={(v) => v !== '—' && set('country')(v)} options={countryOptions} />
                  <Input label="LinkedIn profile" type="url" placeholder="https://linkedin.com/in/…" value={form.linkedin} onChange={set('linkedin')} />
                </div>
                <fieldset className="rounded-md border border-border p-3">
                  <legend className="px-1 text-sm font-medium">Résumé — upload one or give a link (optional)</legend>
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <FileInput label="Upload (PDF, Word, text or RTF, up to 5 MB)" collection="application" field="resume"
                      value={form.resume} onChange={set('resume')} />
                    <Input label="Or a link to your résumé" type="url" placeholder="https://" value={form.resume_url} onChange={set('resume_url')} />
                  </div>
                </fieldset>
                <TextArea label="Why this role? (cover letter)" rows={6} value={form.cover_letter} onChange={set('cover_letter')} />
                <FieldInputs collection="application" action="apply" exclude={BUILT_IN} values={form} onChange={setForm} />

                {usSelfId && (
                  <fieldset className="rounded-md border border-border bg-muted p-3">
                    <legend className="px-1 text-sm font-semibold">Voluntary self-identification (optional)</legend>
                    <p className="mb-3 text-sm text-muted-foreground">
                      US employers are asked to invite applicants to share this so they can check their hiring is fair. Answering
                      is entirely voluntary and choosing not to will not affect your application. Your answers are kept apart
                      from your application and are <strong className="text-foreground">not seen by the people who decide on your application</strong> — only
                      a compliance team, and only as totals.
                    </p>
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      {EEO.map(([name, label]) => (
                        <Select key={name} label={label} value={form[name]} onChange={set(name)}
                          options={[{ value: '', label: 'Not answered' }, ...choices(app, name)]} />
                      ))}
                    </div>
                  </fieldset>
                )}

                <section aria-labelledby="privacy-h" className="rounded-md border border-border p-3">
                  <h3 id="privacy-h" className="text-sm font-semibold">How we use your information</h3>
                  <p className="mt-1 whitespace-pre-line text-sm text-muted-foreground">{s.privacy_notice}</p>
                  {s.privacy_notice_version && <p className="mt-1 text-xs text-muted-foreground">Notice version {s.privacy_notice_version}</p>}
                  <label className="mt-3 flex items-start gap-2 text-sm">
                    <input type="checkbox" className={`mt-0.5 ${FOCUS}`} checked={form.consent_pool === true}
                      onChange={(e) => set('consent_pool')(e.target.checked)} />
                    <span>Keep me in your talent pool: you may keep my details for longer and contact me about other roles. (Optional.)</span>
                  </label>
                </section>

                <div aria-hidden="true" style={{ position: 'absolute', left: '-10000px', width: 1, height: 1, overflow: 'hidden' }}>
                  <label>Website<input tabIndex={-1} autoComplete="off" value={trap} onChange={(e) => setTrap(e.target.value)} /></label>
                </div>
                {state.kind === 'failed' && <p role="alert" className="text-sm text-destructive">{state.message}</p>}
                <p className="text-xs text-muted-foreground">By submitting you confirm you have read how we use your information above.</p>
                <div>
                  <Button type="submit" tone="primary" disabled={state.kind === 'sending'}>
                    {state.kind === 'sending' ? 'Sending…' : 'Submit application'}
                  </Button>
                </div>
              </Stack>
            </Form>
          )}
        </section>
      </main>
    </div>
  )
}
