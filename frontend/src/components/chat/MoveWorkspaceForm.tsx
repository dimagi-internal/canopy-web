import { useState } from "react";
import { Button } from "canopy-ui/ui";

export type MoveTarget = { slug: string; display_name: string };

/**
 * Pick the workspace to file this session in (POST /{id}/move). Tenant-first:
 * after the move only members of the new workspace can see it, and anyone given
 * the chat who is not a member there loses it — said here, before the click.
 */
export function MoveWorkspaceForm({
  current,
  targets,
  onMove,
  onDone,
}: {
  current: string;
  targets: MoveTarget[];
  onMove: (workspace: string) => Promise<void>;
  onDone: () => void;
}) {
  const options = targets.filter((t) => t.slug !== current);
  const [target, setTarget] = useState(options[0]?.slug ?? "");
  const [moving, setMoving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    if (!target) return;
    setMoving(true);
    setError(null);
    try {
      await onMove(target);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not move this session");
    } finally {
      setMoving(false);
    }
  };

  return (
    <div className="flex flex-col gap-3" data-testid="move-workspace-form">
      <p className="text-[12px] text-muted-foreground">
        Filed in <span className="font-medium text-foreground">{current}</span> now. After the move,
        only members of the new workspace can see it, and people you gave it to who are not members
        there lose it. Your runner will not move it back.
      </p>
      <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
        Move to
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          data-testid="move-workspace-select"
          className="rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
        >
          {options.map((t) => (
            <option key={t.slug} value={t.slug}>
              {t.display_name}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      <Button size="sm" disabled={!target || moving} onClick={() => void confirm()}>
        {moving ? "Moving…" : "Move"}
      </Button>
    </div>
  );
}
