import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { getLinkPage, type LinkPage } from '@/api/workspaceApps'
import { AppFrame } from '@/workspaceApps/AppFrame'

/**
 * The page behind a workspace app's PRIVATE link (`/w/:workspace/app/:appKey/l/:token`)
 * — a candidate picking an interview time, answering an offer. No account: the
 * signed token is the credential, and it opens ONE record. The page sees only
 * the fields the link declares and runs only the link's actions, on that record.
 */
export default function LinkAppPage() {
  const { workspace = '', appKey = '', token = '' } = useParams()
  const [link, setLink] = useState<LinkPage | null>(null)
  const [missing, setMissing] = useState(false)

  useEffect(() => {
    let cancelled = false
    getLinkPage(workspace, appKey, token).then(
      (l) => {
        if (cancelled) return
        setLink(l)
        document.title = `${l.label} · ${l.workspace_name}`
      },
      () => !cancelled && setMissing(true),
    )
    return () => {
      cancelled = true
    }
  }, [workspace, appKey, token])

  if (missing) {
    return (
      <div className="mx-auto max-w-xl p-10 text-center text-muted-foreground">
        This link is not valid, or it has expired. Reply to the email it came in to ask for a new one.
      </div>
    )
  }
  if (!link) return <div className="p-10 text-center text-sm text-muted-foreground">Loading…</div>

  return (
    <div className="min-h-screen bg-background">
      {link.draft && (
        <div className="bg-warning/15 px-4 py-2 text-center text-sm text-warning">Test copy — nothing here is real and nothing is sent.</div>
      )}
      <AppFrame
        workspace={{ slug: link.workspace, name: link.workspace_name }}
        app={{ key: link.key, name: link.name }}
        page={link.page}
        roles={['link']}
        params={{}}
        door="link"
        link={{ token, record: link.record }}
        dataModel={link.data_model}
        settings={link.settings}
        viewer={{ email: '', name: '' }}
        onGo={() => {}}
      />
    </div>
  )
}
