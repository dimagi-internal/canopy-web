import { RouterProvider } from 'react-router-dom'
import { AuthProvider } from './auth/AuthProvider'
import { StepUpDialog } from './auth/StepUpDialog'
import { ThemeProvider } from './theme/ThemeProvider'
import { router } from './router'

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <RouterProvider router={router} />
        {/* The passkey prompt behind every `step_up_required` (canopy-web#1538). */}
        <StepUpDialog />
      </AuthProvider>
    </ThemeProvider>
  )
}
