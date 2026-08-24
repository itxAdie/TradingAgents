import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchPortfolio, fetchSystemStatus, useEventBuffer } from "../api/hooks";
import { PaperBadge } from "./PaperBadge";
import { cn } from "../lib/cn";

const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/markets", label: "Markets" },
  { to: "/signals", label: "Signals" },
  { to: "/portfolio", label: "Portfolio" },
  { to: "/trades", label: "Trades" },
  { to: "/risk", label: "Risk" },
  { to: "/backtests", label: "Backtests" },
  { to: "/system", label: "System" },
];

export function Layout() {
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: fetchPortfolio });
  const status = useQuery({ queryKey: ["system-status"], queryFn: fetchSystemStatus });
  const { events, state } = useEventBuffer(50);
  const environment = portfolio.data?.environment ?? "paper";
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;

  return (
    <div className="flex h-full">
      <aside className="flex w-56 shrink-0 flex-col border-r border-slate-800 bg-slate-900/60">
        <div className="border-b border-slate-800 px-4 py-4">
          <div className="text-sm font-bold tracking-wide text-slate-100">TradingAgents</div>
          <div className="mt-0.5 text-[11px] text-slate-500">Web Terminal · read-only</div>
        </div>
        <nav className="flex-1 space-y-1 p-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "block rounded px-3 py-2 text-sm",
                  isActive
                    ? "bg-slate-800 font-semibold text-white"
                    : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-200",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="space-y-2 border-t border-slate-800 p-3 text-[11px] text-slate-500">
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className={cn(
                "h-2 w-2 rounded-full",
                state === "open" ? "bg-emerald-400" : state === "connecting" ? "bg-amber-400" : "bg-slate-600",
              )}
            />
            realtime: {state}
          </div>
          {status.data && (
            <div>
              ai key:{" "}
              <span
                className={
                  status.data.components.find((c) => c.component === "AI Research")?.status ===
                  "online"
                    ? "text-emerald-400"
                    : "text-slate-400"
                }
              >
                {status.data.components.find((c) => c.component === "AI Research")?.status ===
                "online"
                  ? "configured"
                  : "absent"}
              </span>
            </div>
          )}
          {lastEvent && <div title={lastEvent.event}>last event: {lastEvent.event}</div>}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Persistent header — the PAPER badge never leaves the viewport. */}
        <header className="flex items-center justify-between gap-4 border-b border-slate-800 bg-slate-900/60 px-6 py-3">
          <div className="flex items-center gap-3">
            <PaperBadge environment={environment} />
            {portfolio.data && (
              <span className="text-xs text-slate-500">
                {portfolio.data.account_id} · equity{" "}
                <span className="font-mono text-slate-300">
                  ${portfolio.data.equity.toLocaleString("en-US", { minimumFractionDigits: 2 })}
                </span>
              </span>
            )}
          </div>
          {status.data && (
            <span className="font-mono text-[11px] text-slate-600">{status.data.generated_at}</span>
          )}
        </header>

        <main className="min-h-0 flex-1 overflow-y-auto p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
