import { cn } from "../lib/cn";

/**
 * The PAPER TRADING badge is a hard requirement (spec §3/§44): it must be
 * visible on every page, in the viewport at all times, for every mode the
 * backend can report. Live environments render it red; paper renders blue.
 */
export function PaperBadge({ environment, compact = false }: { environment: string; compact?: boolean }) {
  const isLive = environment === "live";
  return (
    <span
      data-testid="paper-badge"
      aria-label={`Trading environment: ${environment}`}
      className={cn(
        "inline-flex items-center gap-1.5 rounded font-bold uppercase tracking-widest",
        compact ? "px-2 py-0.5 text-[10px]" : "px-3 py-1 text-xs",
        isLive
          ? "bg-red-600/20 text-red-300 ring-1 ring-red-500"
          : "bg-blue-600/20 text-blue-300 ring-1 ring-blue-500",
      )}
    >
      <span
        aria-hidden
        className={cn("h-2 w-2 rounded-full", isLive ? "animate-pulse bg-red-400" : "bg-blue-400")}
      />
      {isLive ? "LIVE" : "PAPER"} TRADING
    </span>
  );
}
