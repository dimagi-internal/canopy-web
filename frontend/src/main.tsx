import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { registerPwa } from './pwa/registerPwa'
import { installWorkspaceRetry } from './api/workspaceRetry'

installWorkspaceRetry()
registerPwa()

createRoot(document.getElementById('root')!).render(<App />)
