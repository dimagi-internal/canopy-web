export function CopyBlock({
  label, value, copied, onCopy,
}: { label: string; value: string; copied: boolean; onCopy: () => void }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">{label}</span>
        <button
          type="button"
          onClick={onCopy}
          className="text-xs text-primary hover:text-primary transition-colors"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="bg-background border border-border rounded-lg p-3 text-xs text-foreground-secondary font-mono overflow-x-auto whitespace-pre-wrap break-all">
        {value}
      </pre>
    </div>
  )
}
