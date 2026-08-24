import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchEquity, fetchMarkets, fetchPortfolio, fetchSignals } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading, Stat, statusTone } from "../components/ui";
import { EquityChart } from "../components/charts";
import { fmtMoney, fmtPct, fmtSignedMoney, fmtTime } from "../lib/format";

export function OverviewPage() {
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: fetchPortfolio });
  const markets = useQuery({ queryKey: ["markets"], queryFn: fetchMarkets });
  const signals = useQuery({
    queryKey: ["signals", "recent"],
    queryFn: () => fetchSignals({ limit: 5 }),
  });
  const equity = useQuery({ queryKey: ["equity"], queryFn: () => fetchEquity(200) });

  if (portfolio.isPending) return <Loading />;
  if (portfolio.error) {
    // A fresh server with no account yet reports 404; show honest guidance.
    return (
      <ErrorNote
        error={new Error(
          `${portfolio.error.message} — no paper account found for this server. ` +
            "Start it against a seeded account or let the engine open its first position.",
        )}
      />
    );
  }
  const p = portfolio.data;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Equity" value={fmtMoney(p.equity)} />
        <Stat label="Cash" value={fmtMoney(p.cash)} />
        <Stat
          label="Realized P&L"
          value={
            <span className={p.realized_pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
              {fmtSignedMoney(p.realized_pnl)}
            </span>
          }
        />
        <Stat
          label="Unrealized P&L"
          value={
            <span className={p.unrealized_pnl >= 0 ? "text-emerald-300" : "text-red-300"}>
              {fmtSignedMoney(p.unrealized_pnl)}
            </span>
          }
        />
        <Stat label="Open positions" value={p.open_positions.length} />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <Card title="Equity curve" className="xl:col-span-2">
          {equity.isPending ? (
            <Loading />
          ) : equity.data && equity.data.length > 1 ? (
            <EquityChart points={equity.data} />
          ) : (
            <p className="text-sm text-slate-500">
              Not enough equity points yet — the curve appears after a few sessions.
            </p>
          )}
        </Card>

        <Card title="Latest signals">
          {signals.isPending ? (
            <Loading />
          ) : signals.data && signals.data.items.length > 0 ? (
            <ul className="space-y-2 text-sm">
              {signals.data.items.map((s) => (
                <li key={s.signal_id} className="flex items-center gap-2">
                  <Chip tone={statusTone(s.state)}>{s.state}</Chip>
                  <Link
                    to={`/signals/${s.signal_id}`}
                    className="font-mono text-xs text-sky-400 hover:underline"
                  >
                    {s.asset_id} {s.action} {(s.confidence * 100).toFixed(0)}%
                  </Link>
                  <span className="ml-auto font-mono text-[10px] text-slate-600">
                    {fmtTime(s.generated_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-slate-500">No signals yet.</p>
          )}
        </Card>
      </div>

      <Card title="Watchlist">
        {markets.isPending ? (
          <Loading />
        ) : markets.error ? (
          <ErrorNote error={markets.error} />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2">Asset</th>
                <th className="py-2">Last</th>
                <th className="py-2">Change ({markets.data?.[0]?.change_timeframe ?? "1d"})</th>
                <th className="py-2">Source</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {markets.data?.map((m) => (
                <tr key={m.spec.asset_id} className="border-t border-slate-800/60">
                  <td className="py-2">
                    <Link to={`/markets/${m.spec.asset_id}`} className="text-sky-400 hover:underline">
                      {m.spec.asset_id}
                    </Link>
                  </td>
                  <td className="py-2">{fmtMoney(m.quote?.last)}</td>
                  <td
                    className={`py-2 ${(m.change_pct ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}`}
                  >
                    {fmtPct(m.change_pct)}
                  </td>
                  <td className="text-xs text-slate-500" title={m.note}>
                    {m.quote ? `${m.quote.source} · ${m.quote.data_status}` : m.note || "no quote"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <p className="font-mono text-[11px] italic text-blue-300">{p.disclaimer}</p>
    </div>
  );
}
