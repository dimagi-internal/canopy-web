import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { registerPwa } from './pwa/registerPwa'
import { installWorkspaceRetry } from './api/workspaceRetry'
import { installStrayFileDropGuard } from './lib/strayFileDrop'

installWorkspaceRetry()
installStrayFileDropGuard()
registerPwa()

createRoot(document.getElementById('root')!).render(<App />)
