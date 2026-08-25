import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  brokerHalt,
  brokerReconcile,
  brokerResume,
  brokerShutdown,
  brokerStartup,
  fetchBrokerAdapters,
  fetchBrokerStatus,
} from "../api/hooks";
import { Card, Chip, ErrorNote, Loading } from "../components/ui";
import { fmtTime } from "../lib/format";

export function BrokerPage() {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: ["broker-status"],
    queryFn: fetchBrokerStatus,
    refetchInterval: 10_000,
  });
  const adapters = useQuery({ queryKey: ["broker-adapters"], queryFn: fetchBrokerAdapters });
  const [operator, setOperator] = useState("dashboard-operator");
  const [haltReason, setHaltReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["broker-status"] });
    void queryClient.invalidateQueries({ queryKey: ["broker-recon"] });
  };

  const withNotice = (fn: () => Promise<unknown>, label: string) => async () => {
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} OK`);
    } finally {
      refresh();
    }
  };

  const startup = useMutation({ mutationFn: withNotice(brokerStartup, "startup") });
  const shutdown = useMutation({ mutationFn: withNotice(brokerShutdown, "shutdown") });
  const reconcile = useMutation({ mutationFn: withNotice(brokerReconcile, "reconcile") });
  const halt = useMutation({
    mutationFn: () => brokerHalt(haltReason || "operator halt", operator),
    onSuccess: () => {
      setNotice("halt armed");
      setHaltReason("");
      refresh();
    },
  });
  const resume = useMutation({
    mutationFn: () => brokerResume(operator),
    onSuccess: () => {
      setNotice("resumed OK");
      refresh();
    },
  });

  if (status.isPending) return <Loading />;
  if (status.error) return <ErrorNote error={status.error} />;
  const s = status.data!;

  const connTone =
    s.connection === "CONNECTED" ? "positive" : s.connection === "DISCONNECTED" ? "neutral" : "negative";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold">Broker</h1>
        <Chip tone={s.ready ? "positive" : "warning"}>{s.ready ? "ready" : "not ready"}</Chip>
        {s.halted && <Chip tone="negative">halted</Chip>}
        {s.circuit_breaker && <Chip tone="negative">breaker tripped</Chip>}
        <span className="text-xs text-slate-500">
          sandbox adapter only — no real venue connectivity
        </span>
      </div>

      {notice && <p className="text-xs text-emerald-400">{notice}</p>}

      <Card title="Execution environment">
        <dl className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm md:grid-cols-4">
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Broker</dt>
            <dd className="font-mono">{s.broker}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Environment</dt>
            <dd className="font-mono">{s.environment}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Account</dt>
            <dd className="font-mono">{s.account_id}</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Connection</dt>
            <dd>
              <Chip tone={connTone}>{s.connection}</Chip>
            </dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Live armed</dt>
            <dd>{s.live_armed ? "yes" : "no"} (sandbox cannot be armed)</dd>
          </div>
          <div>
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Config version</dt>
            <dd className="font-mono text-xs">{s.configuration_version}</dd>
          </div>
          <div className="col-span-2">
            <dt className="text-[11px] uppercase tracking-wider text-slate-500">Adapters registered</dt>
            <dd className="font-mono text-xs">
              {(adapters.data ?? []).map((a) => a.name).join(", ") || "…"}
            </dd>
          </div>
        </dl>
        {(s.halted || s.circuit_breaker) && (
          <p className="mt-3 rounded border border-red-900/60 bg-red-950/40 p-2 text-xs text-red-300">
            {s.halted && <span>halt reason: {s.halt_reason || "—"} </span>}
            {s.circuit_breaker && <span>breaker: {s.circuit_breaker_reason || "—"}</span>}
          </p>
        )}
      </Card>

      <Card title="Operator controls (audited)">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label className="flex items-center gap-1 text-xs text-slate-400">
            operator
            <input
              value={operator}
              onChange={(e) => setOperator(e.target.value)}
              className="w-44 rounded border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs"
            />
          </label>
          <button
            onClick={() => startup.mutate()}
            disabled={startup.isPending}
            className="rounded bg-emerald-700 px-3 py-1 text-xs hover:bg-emerald-600 disabled:opacity-50"
          >
            startup
          </button>
          <button
            onClick={() => reconcile.mutate()}
            disabled={reconcile.isPending}
            className="rounded bg-sky-800 px-3 py-1 text-xs hover:bg-sky-700 disabled:opacity-50"
          >
            reconcile now
          </button>
          <button
            onClick={() => shutdown.mutate()}
            disabled={shutdown.isPending}
            className="rounded bg-slate-700 px-3 py-1 text-xs hover:bg-slate-600 disabled:opacity-50"
          >
            shutdown
          </button>
          <input
            value={haltReason}
            onChange={(e) => setHaltReason(e.target.value)}
            placeholder="halt reason"
            className="w-40 rounded border border-slate-700 bg-slate-900 px-2 py-1 font-mono text-xs"
          />
          <button
            onClick={() => halt.mutate()}
            disabled={halt.isPending}
            className="rounded bg-red-800 px-3 py-1 text-xs hover:bg-red-700 disabled:opacity-50"
          >
            HALT
          </button>
          <button
            onClick={() => resume.mutate()}
            disabled={resume.isPending}
            className="rounded bg-amber-700 px-3 py-1 text-xs hover:bg-amber-600 disabled:opacity-50"
          >
            resume
          </button>
        </div>
        {(startup.error || halt.error || resume.error || reconcile.error) && (
          <ErrorNote error={startup.error ?? halt.error ?? resume.error ?? reconcile.error} />
        )}
      </Card>

      <Card title="Last reconciliation (broker is authoritative)">
        {!s.last_reconciliation ? (
          <p className="p-4 text-center text-sm text-slate-500">
            No reconciliation has run yet in this store.
          </p>
        ) : (
          <div className="space-y-2 text-sm">
            <p className="text-xs text-slate-400">
              {fmtTime(s.last_reconciliation.ts)} · trigger {s.last_reconciliation.trigger} ·{" "}
              {s.last_reconciliation.orders_checked} orders / {s.last_reconciliation.positions_checked}{" "}
              positions ·{" "}
              <Chip tone={s.last_reconciliation.clean ? "positive" : "negative"}>
                {s.last_reconciliation.clean ? "clean" : `${s.last_reconciliation.mismatches.length} mismatches`}
              </Chip>
            </p>
            {s.last_reconciliation.resolutions.length > 0 && (
              <ul className="list-inside list-disc font-mono text-xs text-slate-400">
                {s.last_reconciliation.resolutions.map((r, i) => (
                  <li key={i}>{r}</li>
                ))}
              </ul>
            )}
            {s.last_reconciliation.mismatches.map((m, i) => (
              <p key={i} className="font-mono text-xs text-red-300">
                [{m.kind}] {m.detail}
              </p>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
