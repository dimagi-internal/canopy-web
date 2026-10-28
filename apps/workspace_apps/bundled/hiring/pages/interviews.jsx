// My interviews: what's coming up for me (today, this week, later), the
// scorecards I still owe, and — for recruiters — every interview, including the
// ones still waiting for the candidate to pick a time.
function arr(x) { return Array.isArray(x) ? x : [] }
function people(text) { return String(text || '').split(/[,;\s]+/).map((e) => e.trim().toLowerCase()).filter(Boolean) }
function localDay(offsetDays) {
  const d = new Date(Date.now() + (offsetDays || 0) * 86400000)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}
function chunks(list, n) { const out = []; for (let i = 0; i < list.length; i += n) out.push(list.slice(i, i + n)); return out }

function View({ data, app, scope }) {
  const interviews = arr(data.interviews)
  const scorecards = arr(data.scorecards)
  const me = String((scope && scope.viewer && scope.viewer.email) || '').toLowerCase()
  const recruiter = app.roles.includes('recruiter')
  const [everyone, setEveryone] = useState(false)
  const [cands, setCands] = useState({})
  const [jobs, setJobs] = useState({})

  // Names and job titles for the interviews on screen (a `where` list takes at most 50 ids).
  const appIds = useMemo(() => [...new Set(interviews.map((x) => x.application).filter(Boolean))], [interviews])
  useEffect(() => {
    let live = true
    Promise.all(chunks(appIds, 50).map((ids) => app.query('application', { where: { id: ids }, limit: 50 }).catch(() => [])))
      .then((parts) => { if (live) setCands(Object.fromEntries(parts.flatMap(arr).map((a) => [a.id, a]))) })
    app.query('job', { limit: 2000 }).then((rows) => { if (live) setJobs(Object.fromEntries(arr(rows).map((j) => [j.id, j]))) }).catch(() => {})
    return () => { live = false }
  }, [appIds.join(',')])

  const today = localDay(0)
  const weekEnd = localDay(7)
  const mineOnly = (x) => people(x.interviewers).includes(me)
  const scope_ = everyone && recruiter ? interviews : interviews.filter(mineOnly)
  const submitted = (x, who) => scorecards.some((s) => s.interview === x.id && String(s.interviewer || '').toLowerCase() === who)
  const byTime = (x, y) => `${x.date || ''} ${x.time || ''}`.localeCompare(`${y.date || ''} ${y.time || ''}`)

  const upcoming = scope_.filter((x) => x.status === 'Scheduled' && x.date && x.date >= today).sort(byTime)
  const groups = [
    ['Today', upcoming.filter((x) => x.date === today)],
    ['This week', upcoming.filter((x) => x.date > today && x.date <= weekEnd)],
    ['Later', upcoming.filter((x) => x.date > weekEnd)],
  ]
  const finished = (x) => x.status === 'Done' || (x.status === 'Scheduled' && x.date && x.date < today)
  const due = scope_.filter(finished).flatMap((x) => {
    const owed = (everyone && recruiter ? people(x.interviewers) : [me]).filter((who) => !submitted(x, who))
    return owed.length ? [{ ...x, owed }] : []
  }).sort(byTime)
  const requested = interviews.filter((x) => x.status === 'Requested')

  const who = (x) => {
    const c = cands[x.application]
    const j = jobs[x.job || (c && c.job)]
    return (
      <div className="min-w-0">
        <button type="button" className="text-left font-medium hover:underline" onClick={() => app.go('candidate', { id: x.application })}>
          {c ? c.name : `Candidate #${x.application}`}
        </button>
        <div className="truncate text-xs text-muted-foreground">{[app.optionLabel('interview', 'kind', x.kind), j && j.title].filter(Boolean).join(' · ')}</div>
      </div>
    )
  }
  const row = (x, right) => (
    <div key={`${x.id}`} className="flex flex-wrap items-center justify-between gap-3 border-b border-border py-2 text-sm last:border-0">
      <div className="w-28 shrink-0 tabular-nums text-muted-foreground">{x.date || '—'}{x.time ? ` ${x.time}` : ''}</div>
      <div className="min-w-0 flex-1">{who(x)}</div>
      {everyone && <div className="hidden max-w-xs truncate text-xs text-muted-foreground sm:block">{x.interviewers}</div>}
      <div className="flex shrink-0 items-center gap-2">{right}</div>
    </div>
  )

  return (
    <Stack gap={4}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <Grid cols={3}>
          <Stat label="Today" value={groups[0][1].length} />
          <Stat label="Next 7 days" value={groups[1][1].length} />
          <Stat label="Scorecards due" value={due.length} tone={due.length ? 'warning' : 'success'} />
        </Grid>
        {recruiter && (
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            <input type="checkbox" checked={everyone} onChange={(e) => setEveryone(e.target.checked)} /> Everyone's interviews
          </label>
        )}
      </div>

      <Card title={everyone ? 'Scorecards still owed' : 'Scorecards I owe'}>
        {due.length === 0 ? <Empty>All caught up.</Empty> : due.map((x) => row(x, (
          <>
            {everyone && <span className="text-xs text-warning">{x.owed.join(', ')}</span>}
            {!everyone && <Button size="sm" tone="primary" onClick={() => app.go('kit', { id: x.id })}>Write scorecard</Button>}
            {everyone && <Button size="sm" onClick={() => app.go('kit', { id: x.id })}>Kit</Button>}
          </>
        )))}
      </Card>

      {groups.map(([label, list]) => (
        <Card key={label} title={`${label} (${list.length})`}>
          {list.length === 0 ? <Empty>{label === 'Today' ? 'No interviews today.' : 'None.'}</Empty> : list.map((x) => row(x, (
            <>
              {x.duration && <span className="text-xs text-muted-foreground">{x.duration} min</span>}
              {x.meeting_link && <a className="text-xs text-primary hover:underline" href={x.meeting_link}>Join</a>}
              <Button size="sm" onClick={() => app.go('kit', { id: x.id })}>Interview kit</Button>
            </>
          )))}
        </Card>
      ))}

      {recruiter && everyone && (
        <Card title={`Waiting for the candidate to pick a time (${requested.length})`}>
          {requested.length === 0 ? <Empty>None.</Empty> : [...requested].sort((x, y) => String(x.created_at).localeCompare(String(y.created_at))).map((x) => row(x, (
            <>
              <span className="text-xs text-muted-foreground">sent {String(x.created_at || '').slice(0, 10)} · {String(x.slot_options || '').split('\n').filter(Boolean).length} times offered</span>
              <Button size="sm" onClick={() => app.go('candidate', { id: x.application })}>Open</Button>
            </>
          )))}
        </Card>
      )}
    </Stack>
  )
}
