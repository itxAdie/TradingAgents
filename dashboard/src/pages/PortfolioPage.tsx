import { useQuery } from "@tanstack/react-query";
import { fetchEquity, fetchPositions, fetchPortfolio } from "../api/hooks";
import { Card, ErrorNote, Loading, Stat } from "../components/ui";
import { EquityChart } from "../components/charts";
import { fmtMoney, fmtQty, fmtSignedMoney, fmtTime } from "../lib/format";

export function PortfolioPage() {
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: fetchPortfolio });
  const equity = useQuery({ queryKey: ["equity"], queryFn: () => fetchEquity(500) });
  const positions = useQuery({ queryKey: ["positions"], queryFn: () => fetchPositions() });

  if (portfolio.isPending) return <Loading />;
  if (portfolio.error) return <ErrorNote error={portfolio.error} />;
  const p = portfolio.data;

  return (
    <div className="space-y-6">
      {/* The backend's own report text, verbatim, disclaimer included (spec §16). */}
      <div className="rounded-lg border border-blue-900/60 bg-blue-950/20 px-4 py-3">
        <div className="text-[11px] uppercase tracking-wider text-slate-500">
          Backend report · verbatim
        </div>
        <p className="mt-1 font-mono text-sm text-slate-300">
          {p.environment}/{p.account_id} · equity {fmtMoney(p.equity)} · cash{" "}
          {fmtMoney(p.cash)} · as of {fmtTime(p.generated_at)}
        </p>
        <p className="mt-2 text-xs italic text-blue-300">{p.disclaimer}</p>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Starting capital" value={fmtMoney(p.initial_capital)} />
        <Stat label="Realized P&L" value={fmtSignedMoney(p.realized_pnl)} />
        <Stat label="Unrealized P&L" value={fmtSignedMoney(p.unrealized_pnl)} />
        <Stat label="Open positions" value={p.open_positions.length} />
      </div>

      <Card title="Equity curve">
        {equity.isPending ? (
          <Loading />
        ) : equity.data && equity.data.length > 1 ? (
          <EquityChart points={equity.data} height={340} />
        ) : (
          <p className="text-sm text-slate-500">Not enough history yet.</p>
        )}
      </Card>

      <Card title="Positions">
        {positions.isPending ? (
          <Loading />
        ) : positions.error ? (
          <ErrorNote error={positions.error} />
        ) : positions.data && positions.data.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2">Asset</th>
                <th className="py-2">Qty</th>
                <th className="py-2">Entry</th>
                <th className="py-2">Last</th>
                <th className="py-2">Unrealized</th>
                <th className="py-2">SL / TP</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {positions.data.map((pos) => (
                <tr key={pos.position_id} className="border-t border-slate-800/60">
                  <td className="py-2">{pos.asset_id}</td>
                  <td>{fmtQty(pos.quantity)}</td>
                  <td>{fmtMoney(pos.entry_price)}</td>
                  <td>{fmtMoney(pos.current_price)}</td>
                  <td
                    className={
                      (pos.unrealized_pnl ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"
                    }
                  >
                    {fmtSignedMoney(pos.unrealized_pnl)}
                  </td>
                  <td className="text-xs text-slate-400">
                    {fmtMoney(pos.stop_loss)} / {fmtMoney(pos.take_profit)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">No open positions.</p>
        )}
      </Card>

      <p className="font-mono text-[11px] text-slate-600">
        rendered {fmtTime(new Date().toISOString())}
      </p>
    </div>
  );
}
