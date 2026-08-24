import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export function Card({ title, children, className }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={cn("rounded-lg border border-slate-800 bg-slate-900/40", className)}>
      {title && <h2 className="border-b border-slate-800 px-4 py-2 text-sm font-semibold text-slate-300">{title}</h2>}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 px-4 py-3">
      <div className="text-[11px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-1 font-mono text-lg text-slate-100" title={hint}>
        {value}
      </div>
    </div>
  );
}

export function Loading({ label = "loading…" }: { label?: string }) {
  return <div className="animate-pulse p-6 text-center text-sm text-slate-500">{label}</div>;
}

export function ErrorNote({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div role="alert" className="rounded border border-red-900 bg-red-950/40 px-4 py-3 text-sm text-red-300">
      {message}
    </div>
  );
}

export function Empty({ children = "nothing here yet" }: { children?: ReactNode }) {
  return <div className="p-6 text-center text-sm text-slate-600">{children}</div>;
}

const CHIP_STYLES: Record<string, string> = {
  positive: "bg-emerald-500/15 text-emerald-300",
  negative: "bg-red-500/15 text-red-300",
  neutral: "bg-slate-700/40 text-slate-300",
  warning: "bg-amber-500/15 text-amber-300",
};

export function Chip({ tone = "neutral", children }: { tone?: keyof typeof CHIP_STYLES; children: ReactNode }) {
  return (
    <span className={cn("inline-block rounded px-2 py-0.5 font-mono text-xs", CHIP_STYLES[tone])}>{children}</span>
  );
}

export function statusTone(status: string): keyof typeof CHIP_STYLES {
  switch (status) {
    case "completed":
    case "accepted":
    case "executed":
    case "closed":
      return "positive";
    case "failed":
    case "rejected":
      return "negative";
    case "running":
    case "queued":
    case "generated":
    case "open":
      return "warning";
    default:
      return "neutral";
  }
}
