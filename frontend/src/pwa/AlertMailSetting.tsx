import { useEffect, useState, type JSX } from 'react'
import { apiV2 } from '@/api/client.v2'

type AlertMail = 'all' | 'assigned'

/**
 * Which alert MAIL reaches you. "All": everything your roles make you a recipient
 * of — every blocking alert you could close, every access request to a workspace
 * you run. "Only what's handed to me": the role fan-out skips you, unless nobody
 * else would be told. Email, so it does not depend on this browser's push.
 */
export function AlertMailSetting(): JSX.Element | null {
  const [value, setValue] = useState<AlertMail | null>(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let cancelled = false
    void apiV2.GET('/api/push/preferences').then(({ data }) => {
      if (!cancelled && data) setValue(data.alert_mail)
    })
    return () => {
      cancelled = true
    }
  }, [])

  if (value === null) return null

  const save = async (next: AlertMail) => {
    const { data } = await apiV2.PATCH('/api/push/preferences', { body: { alert_mail: next } })
    if (data) {
      setValue(data.alert_mail)
      setSaved(true)
      window.setTimeout(() => setSaved(false), 1500)
    }
  }

  return (
    <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
      Alert email:
      <select
        value={value}
        data-testid="alert-mail"
        onChange={(e) => void save(e.target.value as AlertMail)}
        className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
      >
        <option value="all">All alerts my roles get</option>
        <option value="assigned">Only what&apos;s handed to me</option>
      </select>
      {saved && <span className="text-success">saved</span>}
    </label>
  )
}
