import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { fetchSignals } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading, statusTone } from "../components/ui";
import { fmtTime } from "../lib/format";

export function SignalsPage() {
  const [state, setState] = useState("");
  const [minConf, setMinConf] = useState(0);

  const signals = useQuery({
    queryKey: ["signals", state, minConf],
    queryFn: () =>
      fetchSignals({
        state: state || undefined,
        min_confidence: minConf > 0 ? minConf / 100 : undefined,
        limit: 100,
      }),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Signals</h1>

      <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
        <label className="flex items-center gap-2">
          state
          <select
            value={state}
            onChange={(e) => setState(e.target.value)}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300"
          >
            <option value="">any</option>
            <option value="generated">generated</option>
            <option value="accepted">accepted</option>
            <option value="rejected">rejected</option>
            <option value="expired">expired</option>
            <option value="executed">executed</option>
          </select>
        </label>
        <label className="flex items-center gap-2">
          min confidence
          <input
            type="range"
            min={0}
            max={100}
            value={minConf}
            onChange={(e) => setMinConf(Number(e.target.value))}
          />
          <span className="w-10 font-mono">{minConf}%</span>
        </label>
      </div>

      <Card>
        {signals.isPending ? (
          <Loading />
        ) : signals.error ? (
          <ErrorNote error={signals.error} />
        ) : signals.data && signals.data.items.length > 0 ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2">Generated</th>
                <th className="py-2">Asset</th>
                <th className="py-2">Action</th>
                <th className="py-2">Confidence</th>
                <th className="py-2">State</th>
                <th className="py-2">Risk</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {signals.data.items.map((s) => (
                <tr key={s.signal_id} className="border-t border-slate-800/60 hover:bg-slate-900/40">
                  <td className="py-2">
                    <Link
                      to={`/signals/${s.signal_id}`}
                      className="text-xs text-sky-400 hover:underline"
                    >
                      {fmtTime(s.generated_at)}
                    </Link>
                  </td>
                  <td>{s.asset_id}</td>
                  <td className={s.action === "BUY" ? "text-emerald-300" : "text-red-300"}>
                    {s.action}
                  </td>
                  <td>{(s.confidence * 100).toFixed(0)}%</td>
                  <td>
                    <Chip tone={statusTone(s.state)}>{s.state}</Chip>
                  </td>
                  <td className="text-xs">{s.risk_decision || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">No signals match these filters.</p>
        )}
      </Card>
    </div>
  );
}
