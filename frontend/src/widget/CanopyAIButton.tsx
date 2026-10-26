import { Button } from 'canopy-ui/ui'
import { useAssistant } from './assistant'

/** "Canopy AI" beside Send, where a page hides the floating bubble (it sat on
 *  Send). Opens and closes the same widget the bubble does; absent when the
 *  widget is not up. */
export function CanopyAIButton() {
  const assistant = useAssistant()
  if (!assistant) return null
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="canopy-ai"
      onClick={() => assistant.toggle()}
    >
      Canopy AI
    </Button>
  )
}
