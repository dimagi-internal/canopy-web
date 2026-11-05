/**
 * A file dropped where no drop zone takes it must not replace the app.
 *
 * A browser's default for a file dropped outside a drop target is to open it in place
 * of the page. In a browser that costs a Back; in the Canopy Runner Mac app (a web view
 * with no Back) the page is simply gone. Drop zones (the chat composer) call
 * preventDefault themselves and run first — React's listeners sit on the root, below
 * the document — so this only cancels the drops nobody handled.
 */
const carriesFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes('Files')

export function installStrayFileDropGuard(target: Document = document): () => void {
  const onDragOver = (e: DragEvent) => {
    if (e.defaultPrevented || !carriesFiles(e)) return
    e.preventDefault()
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'none'
  }
  const onDrop = (e: DragEvent) => {
    if (e.defaultPrevented || !carriesFiles(e)) return
    e.preventDefault()
  }
  target.addEventListener('dragover', onDragOver)
  target.addEventListener('drop', onDrop)
  return () => {
    target.removeEventListener('dragover', onDragOver)
    target.removeEventListener('drop', onDrop)
  }
}
