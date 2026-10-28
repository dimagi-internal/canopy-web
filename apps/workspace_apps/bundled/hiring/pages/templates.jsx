// Email templates recruiters pick from when they reject, ask for availability,
// confirm an interview or send an offer. Each has a kind, a subject and a body
// with {{tokens}} filled in per candidate when it is sent. The editor lists the
// tokens, flags any it doesn't know, and previews the email with sample values.
const TOKENS = [
  ['{{name}}', "The candidate's full name", 'Amina Otieno'],
  ['{{job.title}}', 'The job they applied for', 'Senior Software Engineer'],
  ['{{settings.company_name}}', 'Your company name (from the app settings)', null],
]

function fill(text, company) {
  const sample = { name: TOKENS[0][2], 'job.title': TOKENS[1][2], 'settings.company_name': company }
  return String(text || '').replace(/\{\{\s*([\w.]+)\s*\}\}/g, (m, k) => (k in sample ? sample[k] : m))
}

function unknownTokens(...texts) {
  const known = new Set(TOKENS.map(([t]) => t.slice(2, -2)))
  const out = new Set()
  for (const t of texts) for (const m of String(t || '').matchAll(/\{\{\s*([^}]*?)\s*\}\}/g)) if (!known.has(m[1])) out.add(`{{${m[1]}}}`)
  return [...out]
}

function Editor({ app, template, onDone }) {
  const kinds = app.options('email_template', 'kind').length ? app.options('email_template', 'kind')
    : (app.field('email_template', 'kind')?.options || []).map((v) => ({ value: v, label: v }))
  const [form, setForm] = useState({
    name: template?.name || '', kind: template?.kind || 'General', subject: template?.subject || '', body: template?.body || '',
    ...Object.fromEntries(Object.entries(template || {}).filter(([k]) => k.startsWith('x_'))),
  })
  const [state, setState] = useState({ busy: false, error: null })
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  const company = app.settings?.company_name || 'Your company'
  const unknown = unknownTokens(form.subject, form.body)

  async function save() {
    setState({ busy: true, error: null })
    const action = template ? 'update_template' : 'add_template'
    const allowed = app.dataModel?._actions?.[action]?.input || ['name', 'kind', 'subject', 'body']
    const input = {}
    for (const [k, v] of Object.entries(form)) if (allowed.includes(k) && v !== '' && v != null) input[k] = v
    try {
      await app.act(action, template ? { record: template.id, input } : { input })
      app.refresh()
      onDone()
    } catch (err) {
      setState({ busy: false, error: err.message })
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Form onSubmit={save}>
        <Stack gap={3}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_12rem]">
            <Input label="Name" required value={form.name} onChange={set('name')} placeholder="e.g. Rejection after screen" />
            <Select label="Used for" value={form.kind} onChange={set('kind')} options={kinds} />
          </div>
          <Input label="Subject" required value={form.subject} onChange={set('subject')} />
          <TextArea label="Body" required rows={12} value={form.body} onChange={set('body')} />
          <FieldInputs collection="email_template" action={template ? 'update_template' : 'add_template'}
            exclude={['name', 'kind', 'subject', 'body']} values={form} onChange={setForm} />
          {unknown.length > 0 && (
            <p role="alert" className="text-sm text-warning">
              {unknown.join(', ')} {unknown.length === 1 ? "isn't a token" : "aren't tokens"} this app fills in — it would be sent exactly as typed.
            </p>
          )}
          {state.error && <p role="alert" className="text-sm text-destructive">{state.error}</p>}
          <div className="flex gap-2">
            <Button type="submit" tone="primary" disabled={state.busy}>{template ? 'Save template' : 'Add template'}</Button>
            <Button onClick={onDone}>Cancel</Button>
          </div>
        </Stack>
      </Form>
      <Stack gap={3}>
        <div className="rounded-lg border border-border bg-muted p-3">
          <h3 className="mb-2 text-sm font-semibold">Tokens you can use</h3>
          <ul className="space-y-1 text-sm">
            {TOKENS.map(([t, what, ex]) => (
              <li key={t} className="flex flex-wrap items-baseline gap-x-2">
                <button type="button" title="Add to the body" className="rounded bg-card px-1 font-mono text-xs hover:underline"
                  onClick={() => set('body')(`${form.body}${form.body && !form.body.endsWith(' ') && !form.body.endsWith('\n') ? ' ' : ''}${t}`)}>{t}</button>
                <span className="text-muted-foreground">{what} — e.g. “{ex ?? company}”</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted-foreground">Click a token to add it to the end of the body.</p>
        </div>
        <div className="rounded-lg border border-border bg-card">
          <div className="border-b border-border px-3 py-2 text-xs text-muted-foreground">Preview with sample values</div>
          <div className="px-3 py-2 text-sm"><span className="text-muted-foreground">Subject: </span><strong>{fill(form.subject, company) || '—'}</strong></div>
          <div className="whitespace-pre-wrap border-t border-border px-3 py-3 text-sm">{fill(form.body, company) || <span className="text-muted-foreground">The body appears here.</span>}</div>
        </div>
      </Stack>
    </div>
  )
}

function View({ data, app }) {
  const templates = Array.isArray(data.templates) ? data.templates : []
  const recruiter = app.roles.includes('recruiter')
  const [editing, setEditing] = useState(null) // null | 'new' | template id
  const [kind, setKind] = useState('')
  const [confirm, setConfirm] = useState(null)
  const [error, setError] = useState(null)
  const current = typeof editing === 'number' ? templates.find((t) => t.id === editing) : null
  const kinds = [...new Set(templates.map((t) => t.kind).filter(Boolean))].sort()
  const shown = templates.filter((t) => !kind || t.kind === kind).sort((a, b) => String(a.name).localeCompare(String(b.name)))

  async function remove(t) {
    setError(null)
    try {
      await app.act('delete_template', { record: t.id })
      setConfirm(null)
      app.refresh()
    } catch (err) {
      setError(err.message)
    }
  }

  if (editing === 'new' || current) {
    return (
      <Card title={current ? `Edit “${current.name}”` : 'New email template'}>
        <Editor key={current?.id || 'new'} app={app} template={current} onDone={() => setEditing(null)} />
      </Card>
    )
  }

  return (
    <Stack gap={4}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="max-w-xs">
          <Select label="Used for" value={kind} onChange={setKind}
            options={[{ value: '', label: `All kinds (${templates.length})` },
              ...kinds.map((k) => ({ value: k, label: app.optionLabel('email_template', 'kind', k) || k }))]} />
        </div>
        {recruiter && <Button tone="primary" onClick={() => setEditing('new')}>+ New template</Button>}
      </div>
      <p className="text-sm text-muted-foreground">
        The default emails (acknowledgement, rejection, pick-a-time, interview confirmation, offer) are the app's settings.
        Templates here are extra versions recruiters choose from when they write to a candidate.
      </p>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Card title="Email templates">
        <Table
          rows={shown}
          empty={templates.length ? 'No templates of this kind.' : 'No templates yet.'}
          columns={[
            { key: 'name', label: 'Name', render: (t) => (
              <button type="button" className="text-left font-medium hover:underline" onClick={() => setEditing(t.id)}>{t.name}</button>
            ) },
            { key: 'kind', label: 'Used for', render: (t) => <Badge>{app.optionLabel('email_template', 'kind', t.kind) || t.kind}</Badge> },
            { key: 'subject', label: 'Subject' },
            { key: 'updated_at', label: 'Changed', render: (t) => String(t.updated_at || '').slice(0, 10) },
            ...(recruiter ? [{ key: 'actions', label: '', render: (t) => (
              <div className="flex justify-end gap-1">
                <Button size="sm" onClick={() => setEditing(t.id)}>Edit</Button>
                {confirm === t.id ? (
                  <>
                    <Button size="sm" tone="destructive" onClick={() => remove(t)}>Delete it</Button>
                    <Button size="sm" onClick={() => setConfirm(null)}>Keep</Button>
                  </>
                ) : <Button size="sm" tone="destructive" onClick={() => setConfirm(t.id)}>Delete</Button>}
              </div>
            ) }] : []),
          ]}
        />
      </Card>
    </Stack>
  )
}
