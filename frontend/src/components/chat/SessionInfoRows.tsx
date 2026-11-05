import type { ReactNode } from "react";
import { CopyButton } from "canopy-ui/ui";

/** One heading and its facts, in the session panel's style. `note` is a one-line plain explanation. */
export function Section({
  title,
  note,
  children,
  testId,
}: {
  title: string;
  note?: string;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section data-testid={testId}>
      <h3 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
      {note && <p className="mb-1 text-muted-foreground">{note}</p>}
      <dl className="space-y-0.5">{children}</dl>
    </section>
  );
}

/** One fact. An empty value is not rendered: the panel lists what IS stored. */
export function Row({
  label,
  value,
  mono,
  copy,
  href,
  testId,
}: {
  label: string;
  value: string;
  mono?: boolean;
  copy?: boolean;
  href?: string;
  testId?: string;
}) {
  if (!value) return null;
  return (
    <div className="grid grid-cols-[6.5rem_1fr] items-start gap-2 sm:grid-cols-[8.5rem_1fr]" data-testid={testId}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={`flex min-w-0 items-start gap-1 ${mono ? "font-mono text-[11px]" : ""}`}>
        {href ? (
          <a href={href} className="break-all text-primary hover:underline">{value}</a>
        ) : (
          <span className={`min-w-0 whitespace-pre-wrap ${mono ? "break-all" : "break-words"}`}>{value}</span>
        )}
        {copy && <CopyButton text={value} variant="ghost" className="shrink-0" title="Copy value" resetMs={1200} />}
      </dd>
    </div>
  );
}
