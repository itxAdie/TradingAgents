import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchMarkets } from "../api/hooks";
import { ErrorNote, Loading } from "../components/ui";
import { fmtMoney, fmtPct } from "../lib/format";

export function MarketsPage() {
  const markets = useQuery({ queryKey: ["markets"], queryFn: fetchMarkets });

  if (markets.isPending) return <Loading />;
  if (markets.error) return <ErrorNote error={markets.error} />;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Markets</h1>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
            <th className="py-2">Asset</th>
            <th className="py-2">Name</th>
            <th className="py-2">Class</th>
            <th className="py-2">Last</th>
            <th className="py-2">Change</th>
            <th className="py-2">Freshness</th>
            <th className="py-2" />
          </tr>
        </thead>
        <tbody className="font-mono">
          {markets.data.map((m) => (
            <tr key={m.spec.asset_id} className="border-t border-slate-800/60">
              <td className="py-2">{m.spec.asset_id}</td>
              <td className="font-sans text-slate-400">{m.spec.display_name}</td>
              <td className="text-xs">{m.spec.asset_class}</td>
              <td>{fmtMoney(m.quote?.last)}</td>
              <td className={(m.change_pct ?? 0) >= 0 ? "text-emerald-300" : "text-red-300"}>
                {fmtPct(m.change_pct)}
              </td>
              <td className="text-xs text-slate-500" title={m.note}>
                {m.freshness}
                {m.quote ? ` · ${m.quote.data_status}` : ""}
              </td>
              <td className="text-right">
                <Link
                  to={`/markets/${m.spec.asset_id}`}
                  className="text-xs text-sky-400 hover:underline"
                >
                  detail →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-slate-600">
        Prices update via realtime events; all computation happens server-side (spec §6).
      </p>
    </div>
  );
}
