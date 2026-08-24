import type { TimelineStage } from "../api/types";
import { fmtTime } from "../lib/format";
import { cn } from "../lib/cn";

const ORDER = [
  "market_data",
  "research",
  "signal",
  "risk_decision",
  "paper_order",
  "entry",
  "position",
  "exit",
] as const;

/**
 * Renders the backend-provided stage list in canonical order; stages the
 * engine has not reached are shown as pending. Purely presentational —
 * ordering, labels and details come from /trades/{id} (spec §19).
 */
export function TradeTimeline({ steps }: { steps: TimelineStage[] }) {
  const byStage = new Map(steps.map((s) => [s.stage, s]));
  return (
    <ol data-testid="trade-timeline" className="space-y-1">
      {ORDER.map((stage) => {
        const step = byStage.get(stage);
        return (
          <li
            key={stage}
            className={cn(
              "flex items-center gap-3 rounded px-3 py-2 text-sm",
              step ? "bg-slate-900/60" : "bg-slate-950/40 opacity-50",
            )}
          >
            <span
              aria-hidden
              className={cn("h-2 w-2 rounded-full", step ? "bg-emerald-400" : "bg-slate-700")}
            />
            <span className="w-36 shrink-0 font-medium text-slate-300">
              {step?.label ?? stage.replace(/_/g, " ")}
            </span>
            <span className="font-mono text-xs text-slate-500">
              {step ? fmtTime(step.timestamp) : "pending"}
            </span>
            <span className="ml-auto max-w-[45%] truncate font-mono text-xs text-slate-500">
              {step?.detail ?? ""}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
