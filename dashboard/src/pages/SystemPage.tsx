import { useQuery } from "@tanstack/react-query";
import { fetchAudit, fetchSettings, fetchSystemStatus } from "../api/hooks";
import { Card, Chip, ErrorNote, Loading } from "../components/ui";
import { fmtTime } from "../lib/format";

const TONE: Record<string, "positive" | "warning" | "negative" | "neutral"> = {
  online: "positive",
  enabled: "positive",
  degraded: "warning",
  idle: "warning",
  disabled: "neutral",
  offline: "negative",
};

export function SystemPage() {
  const status = useQuery({ queryKey: ["system-status"], queryFn: fetchSystemStatus });
  const settings = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const audit = useQuery({ queryKey: ["audit"], queryFn: () => fetchAudit(50) });

  if (status.isPending) return <Loading />;
  if (status.error) return <ErrorNote error={status.error} />;
  const aiComponent = status.data.components.find((c) => c.component === "AI Research");
  const aiKeyPresent = aiComponent?.status === "enabled" || aiComponent?.status === "online";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">System</h1>
        <Chip tone={TONE[status.data.overall] ?? "neutral"}>{status.data.overall}</Chip>
        <span className="font-mono text-xs text-slate-500">{fmtTime(status.data.generated_at)}</span>
      </div>

      <Card title="Components">
        <table className="w-full text-sm">
          <tbody>
            {status.data.components.map((c) => (
              <tr key={c.component} className="border-t border-slate-800/60 first:border-t-0">
                <td className="py-2 pr-4 font-medium text-slate-300">{c.component}</td>
                <td className="py-2 pr-4">
                  <Chip tone={TONE[c.status] ?? "neutral"}>{c.status}</Chip>
                </td>
                <td className="py-2 text-xs text-slate-500">{c.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-3 text-xs text-slate-500">
          AI provider key:{" "}
          {aiKeyPresent ? (
            <span className="text-emerald-400">configured (value never exposed)</span>
          ) : (
            <span className="text-slate-400">absent — research runs will fail honestly</span>
          )}
        </p>
      </Card>

      <Card title="Server settings (read-only view)">
        {settings.isPending ? (
          <Loading />
        ) : settings.error ? (
          <ErrorNote error={settings.error} />
        ) : (
          <dl className="grid grid-cols-2 gap-x-8 gap-y-2 font-mono text-sm lg:grid-cols-3">
            <dt className="text-slate-500">environment</dt>
            <dd>{settings.data.environment}</dd>
            <dt className="text-slate-500">account</dt>
            <dd>{settings.data.account_id}</dd>
            <dt className="text-slate-500">trading enabled</dt>
            <dd>{String(settings.data.trading_enabled)}</dd>
            <dt className="text-slate-500">assets</dt>
            <dd>{settings.data.assets.join(", ")}</dd>
            <dt className="text-slate-500">timeframes</dt>
            <dd>{settings.data.timeframes.join(", ")}</dd>
            <dt className="text-slate-500">risk limits</dt>
            <dd className="truncate" title={JSON.stringify(settings.data.risk_limits)}>
              {Object.entries(settings.data.risk_limits)
                .map(([k, v]) => `${k}=${v}`)
                .join(" · ")}
            </dd>
            <dt className="text-slate-500">execution</dt>
            <dd className="truncate" title={JSON.stringify(settings.data.execution)}>
              {Object.entries(settings.data.execution)
                .map(([k, v]) => `${k}=${v}`)
                .join(" · ")}
            </dd>
          </dl>
        )}
        {/* The settings payload carries configuration only; secret material is
            excluded server-side and the schema forbids it outright. */}
      </Card>

      <Card title="Audit trail (latest 50)">
        {audit.isPending ? (
          <Loading />
        ) : audit.error ? (
          <ErrorNote error={audit.error} />
        ) : audit.data && audit.data.length > 0 ? (
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left uppercase tracking-wider text-slate-600">
                <th className="py-1.5">Time</th>
                <th className="py-1.5">Action</th>
                <th className="py-1.5">Detail</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {audit.data.map((row, i) => (
                <tr key={`${row.ts}-${i}`} className="border-t border-slate-800/60">
                  <td className="whitespace-nowrap py-1.5 text-slate-500">{fmtTime(row.ts)}</td>
                  <td className="text-sky-300">{row.action}</td>
                  <td className="max-w-[40%] truncate text-slate-600">{JSON.stringify(row.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="p-4 text-center text-sm text-slate-500">No audited actions yet.</p>
        )}
      </Card>
    </div>
  );
}
