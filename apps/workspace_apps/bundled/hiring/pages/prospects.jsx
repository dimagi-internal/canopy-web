// Talent pools and the prospects in them: people a recruiter has sourced who are
// not (yet) applying for a job. Recruiters add pools and prospects, and "consider
// for a job" turns a prospect into an applicant on an open job. Each prospect
// shows whether they agreed to stay in the pool and when their record is due to
// be anonymized under the pool retention rule.
const PROSPECT_BUILT_IN = ['name', 'email', 'phone', 'location', 'country', 'region', 'linkedin', 'resume_url', 'cover_letter', 'pool', 'tags', 'consent_pool']

function addDays(iso, days) {
  if (!iso || !days) return ''
  const d = new Date(String(iso).slice(0, 10) + 'T00:00:00Z')
  d.setUTCDate(d.getUTCDate() + Number(days))
  return d.toISOString().slice(0, 10)
}

function PoolForm({ app, pool, onDone }) {
  const [form, setForm] = useState({ name: pool?.name || '', description: pool?.description || '', owner: pool?.owner || '' })
  const [state, setState] = useState({ busy: false, error: null })
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  async function save() {
    setState({ busy: true, error: null })
    try {
      if (pool) await app.act('update_pool', { record: pool.id, input: form })
      else await app.act('add_pool', { input: { name: form.name, description: form.description } })
      app.refresh()
      onDone()
    } catch (err) {
      setState({ busy: false, error: err.message })
    }
  }
  return (
    <Form onSubmit={save}>
      <Stack gap={3}>
        <Input label="Pool name" required value={form.name} onChange={set('name')} />
        <TextArea label="Who belongs here" rows={2} value={form.description} onChange={set('description')} />
        {pool && <Input label="Owner" type="email" value={form.owner} onChange={set('owner')} />}
        {state.error && <p role="alert" className="text-sm text-destructive">{state.error}</p>}
        <div className="flex gap-2">
          <Button type="submit" tone="primary" disabled={state.busy}>{pool ? 'Save pool' : 'Add pool'}</Button>
          <Button onClick={onDone}>Cancel</Button>
        </div>
      </Stack>
    </Form>
  )
}

function ProspectForm({ app, pools, defaultPool, onDone }) {
  const [form, setForm] = useState({ pool: defaultPool || '', consent_pool: false, region: 'Other' })
  const [state, setState] = useState({ busy: false, error: null })
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const regions = app.options('application', 'region').length ? app.options('application', 'region')
    : (app.field('application', 'region')?.options || []).map((v) => ({ value: v, label: v }))
  async function save() {
    setState({ busy: true, error: null })
    const input = {}
    for (const [k, v] of Object.entries(form)) if (v !== '' && v != null) input[k] = k === 'pool' ? Number(v) : v
    try {
      await app.act('add_prospect', { input })
      app.refresh()
      onDone()
    } catch (err) {
      setState({ busy: false, error: err.message })
    }
  }
  return (
    <Form onSubmit={save}>
      <Stack gap={3}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Input label="Full name" required value={form.name} onChange={set('name')} />
          <Input label="Email" type="email" required value={form.email} onChange={set('email')} />
          <Input label="Phone" value={form.phone} onChange={set('phone')} />
          <Input label="LinkedIn" type="url" value={form.linkedin} onChange={set('linkedin')} />
          <Input label="Based in" value={form.location} onChange={set('location')} />
          <Input label="Country" value={form.country} onChange={set('country')} />
          <Select label="Privacy region" value={form.region} onChange={set('region')} options={regions} />
          <Select label="Talent pool" value={String(form.pool)} onChange={set('pool')}
            options={[{ value: '', label: 'No pool' }, ...pools.map((p) => ({ value: String(p.id), label: p.name }))]} />
          <Input label="Résumé link" type="url" value={form.resume_url} onChange={set('resume_url')} />
          <Input label="Tags (comma-separated)" value={form.tags} onChange={set('tags')} />
        </div>
        <TextArea label="Notes on why they're interesting" rows={3} value={form.cover_letter} onChange={set('cover_letter')} />
        <FieldInputs collection="application" action="add_prospect" exclude={PROSPECT_BUILT_IN} values={form} onChange={setForm} />
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={form.consent_pool === true} onChange={(e) => set('consent_pool')(e.target.checked)} />
          <span>They agreed to be kept in our talent pool and contacted about roles. Only tick this if they said so.</span>
        </label>
        {state.error && <p role="alert" className="text-sm text-destructive">{state.error}</p>}
        <div className="flex gap-2">
          <Button type="submit" tone="primary" disabled={state.busy}>{state.busy ? 'Adding…' : 'Add prospect'}</Button>
          <Button onClick={onDone}>Cancel</Button>
        </div>
      </Stack>
    </Form>
  )
}

function Consider({ app, prospect, jobs }) {
  const [jobId, setJobId] = useState('')
  const [state, setState] = useState({ busy: false, error: null })
  if (jobs.length === 0) return <span className="text-xs text-muted-foreground">No open jobs</span>
  async function go() {
    setState({ busy: true, error: null })
    try {
      await app.act('convert_prospect', { record: prospect.id, input: { job: Number(jobId) } })
      app.refresh()
    } catch (err) {
      setState({ busy: false, error: err.message })
    }
  }
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1">
        <select aria-label={`Job to consider ${prospect.name} for`} value={jobId} onChange={(e) => setJobId(e.target.value)}
          className="max-w-[12rem] rounded-md border border-border bg-background px-1.5 py-0.5 text-xs">
          <option value="">Consider for…</option>
          {jobs.map((j) => <option key={j.id} value={String(j.id)}>{j.title}</option>)}
        </select>
        <Button size="sm" disabled={!jobId || state.busy} onClick={go}>Move</Button>
      </div>
      {state.error && <span role="alert" className="text-xs text-destructive">{state.error}</span>}
    </div>
  )
}

function View({ data, app }) {
  const pools = Array.isArray(data.pools) ? data.pools : []
  const prospects = Array.isArray(data.prospects) ? data.prospects : []
  const jobs = Array.isArray(data.jobs) ? data.jobs : []
  const recruiter = app.roles.includes('recruiter')
  const keepDays = Number(app.settings?.retention_pool_days) || 0
  const [poolId, setPoolId] = useState('all')
  const [q, setQ] = useState('')
  const [panel, setPanel] = useState(null) // 'pool' | 'edit-pool' | 'prospect'
  const poolName = Object.fromEntries(pools.map((p) => [p.id, p.name]))
  const count = (id) => prospects.filter((p) => (id === 'none' ? !p.pool || !poolName[p.pool] : p.pool === id)).length
  const current = typeof poolId === 'number' ? pools.find((p) => p.id === poolId) : null

  const shown = prospects.filter((p) => {
    if (poolId === 'none' && p.pool && poolName[p.pool]) return false
    if (typeof poolId === 'number' && p.pool !== poolId) return false
    if (!q) return true
    return `${p.name} ${p.email} ${p.location} ${p.country} ${p.tags}`.toLowerCase().includes(q.toLowerCase())
  })
  const tab = (id, label, n) => (
    <button key={String(id)} type="button" onClick={() => { setPoolId(id); setPanel(null) }}
      aria-pressed={poolId === id}
      className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${poolId === id ? 'bg-muted font-medium' : 'hover:bg-muted'}`}>
      <span className="truncate">{label}</span><Badge>{n}</Badge>
    </button>
  )

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-[14rem_1fr]">
      <Card title="Talent pools" actions={recruiter && <Button size="sm" onClick={() => setPanel('pool')}>+ Pool</Button>}>
        <Stack gap={1}>
          {tab('all', 'All prospects', prospects.length)}
          {pools.slice().sort((a, b) => String(a.name).localeCompare(String(b.name))).map((p) => tab(p.id, p.name, count(p.id)))}
          {count('none') > 0 && tab('none', 'Not in a pool', count('none'))}
        </Stack>
      </Card>

      <Stack gap={4}>
        {panel === 'pool' && <Card title="New talent pool"><PoolForm app={app} onDone={() => setPanel(null)} /></Card>}
        {panel === 'edit-pool' && current && <Card title={`Edit ${current.name}`}><PoolForm app={app} pool={current} onDone={() => setPanel(null)} /></Card>}
        {panel === 'prospect' && (
          <Card title="Add a prospect">
            <ProspectForm app={app} pools={pools} defaultPool={current ? String(current.id) : ''} onDone={() => setPanel(null)} />
          </Card>
        )}

        <Card
          title={current ? current.name : poolId === 'none' ? 'Prospects not in a pool' : 'All prospects'}
          actions={recruiter && (
            <>
              {current && <Button size="sm" onClick={() => setPanel('edit-pool')}>Edit pool</Button>}
              <Button size="sm" tone="primary" onClick={() => setPanel('prospect')}>+ Prospect</Button>
            </>
          )}>
          {current && (current.description || current.owner) && (
            <p className="mb-3 text-sm text-muted-foreground">
              {current.description}{current.owner && <span className="block text-xs">Owner: {current.owner}</span>}
            </p>
          )}
          <div className="mb-3 max-w-sm"><Input label="Search" value={q} onChange={setQ} placeholder="Name, email, place or tag" /></div>
          <Table
            rows={shown}
            empty={prospects.length ? 'No prospects match.' : 'No prospects yet. Add someone you have sourced.'}
            columns={[
              { key: 'name', label: 'Prospect', render: (p) => (
                <div>
                  <button type="button" className="font-medium hover:underline" onClick={() => app.go('candidate', { id: p.id })}>{p.name}</button>
                  <div className="text-xs text-muted-foreground">{p.email}</div>
                </div>
              ) },
              { key: 'pool', label: 'Pool', render: (p) => poolName[p.pool] || <span className="text-muted-foreground">—</span> },
              { key: 'location', label: 'Based in', render: (p) => [p.location, p.country].filter(Boolean).join(', ') },
              { key: 'tags', label: 'Tags', render: (p) => (
                <div className="flex flex-wrap gap-1">{String(p.tags || '').split(',').map((t) => t.trim()).filter(Boolean).map((t) => <Badge key={t}>{t}</Badge>)}</div>
              ) },
              { key: 'consent_pool', label: 'Consent', render: (p) => (
                <div>
                  {p.consent_pool ? <Badge tone="success">Agreed to stay</Badge> : <Badge tone="warning">No consent recorded</Badge>}
                  {keepDays > 0 && <div className="mt-0.5 text-xs text-muted-foreground">Anonymized after {addDays(p.updated_at || p.created_at, keepDays)}{p.legal_hold ? ' (on legal hold)' : ''}</div>}
                </div>
              ) },
              { key: 'credited_to', label: 'Sourced by' },
              { key: 'created_at', label: 'Added', render: (p) => String(p.created_at || '').slice(0, 10) },
              ...(recruiter ? [{ key: 'consider', label: 'Consider for a job', render: (p) => <Consider app={app} prospect={p} jobs={jobs} /> }] : []),
            ]}
          />
          <p className="mt-3 text-xs text-muted-foreground">
            Prospects are kept for {keepDays || '—'} days after their record last changed, then anonymized automatically unless on legal hold.
            Considering a prospect for a job makes them an applicant in that job's pipeline.
          </p>
        </Card>
      </Stack>
    </div>
  )
}
