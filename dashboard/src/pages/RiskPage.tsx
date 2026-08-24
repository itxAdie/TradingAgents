import { useQuery } from "@tanstack/react-query";
import { fetchRisk, fetchRiskEvents } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading } from "../components/ui";
import { fmtMoney, fmtPct, fmtTime } from "../lib/format";

export function RiskPage() {
  const risk = useQuery({ queryKey: ["risk"], queryFn: fetchRisk });
  const events = useQuery({ queryKey: ["risk-events"], queryFn: fetchRiskEvents });

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Risk</h1>
        {risk.data?.halted && <Chip tone="negative">halted: {risk.data.halt_reason}</Chip>}
      </div>

      <Card title="Limits vs current utilization">
        {risk.isPending ? (
          <Loading />
        ) : risk.error ? (
          <ErrorNote error={risk.error} />
        ) : (
          <ul className="space-y-4">
            {risk.data?.limits.map((row) => {
              const displayCurrent =
                row.unit === "currency" ? fmtMoney(row.current_value) : `${row.current_value}`;
              const displayLimit =
                row.unit === "currency" ? fmtMoney(row.limit_value) : `${row.limit_value}${row.unit === "pct" ? "%" : ""}`;
              return (
                <li key={row.key}>
                  <div className="mb-1 flex items-baseline justify-between text-sm">
                    <span className="font-medium text-slate-300">{row.label}</span>
                    <span className="flex items-center gap-2 font-mono text-xs text-slate-400">
                      {displayCurrent} / {displayLimit}
                      <Chip
                        tone={
                          row.utilization_pct >= 90
                            ? "negative"
                            : row.utilization_pct >= 70
                              ? "warning"
                              : "positive"
                        }
                      >
                        {fmtPct(row.utilization_pct, 0)}
                      </Chip>
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded bg-slate-800">
                    <div
                      className={
                        row.utilization_pct >= 90
                          ? "h-full bg-red-500"
                          : row.utilization_pct >= 70
                            ? "h-full bg-amber-500"
                            : "h-full bg-emerald-600"
                      }
                      style={{ width: `${Math.min(row.utilization_pct, 100)}%` }}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Card>

      <Card title="Risk events">
        {events.isPending ? (
          <Loading />
        ) : events.error ? (
          <ErrorNote error={events.error} />
        ) : events.data && events.data.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2">Time</th>
                <th className="py-2">Type</th>
                <th className="py-2">Asset</th>
                <th className="py-2">Message</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {events.data.map((e, i) => (
                <tr key={`${e.ts}-${i}`} className="border-t border-slate-800/60">
                  <td className="py-2">{fmtTime(e.ts)}</td>
                  <td>
                    <Chip tone={e.type.includes("REJECT") || e.type.includes("HALT") ? "negative" : "warning"}>
                      {e.type}
                    </Chip>
                  </td>
                  <td>{e.asset_id || "—"}</td>
                  <td className="text-xs text-slate-400">{e.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">No risk events on record.</p>
        )}
      </Card>
    </div>
  );
}
