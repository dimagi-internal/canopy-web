import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { registerPwa } from './pwa/registerPwa'
import { installWorkspaceRetry } from './api/workspaceRetry'
import { installStrayFileDropGuard } from './lib/strayFileDrop'
import { captureListenPairing } from './listen/pairLink'

// First: a /listen#pair= link's one-time secret leaves the address bar before anything
// (a sign-in redirect, a history entry) can carry it off.
captureListenPairing()
installWorkspaceRetry()
installStrayFileDropGuard()
registerPwa()

createRoot(document.getElementById('root')!).render(<App />)
