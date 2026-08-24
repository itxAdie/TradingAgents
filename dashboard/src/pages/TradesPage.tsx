import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addJournalNote,
  fetchTrades,
  fetchTradeDetail,
} from "../api/hooks";
import type { TradeListItem } from "../api/types";
import { Card, Chip, ErrorNote, Loading } from "../components/ui";
import { TradeTimeline } from "../components/TradeTimeline";
import { fmtQty, fmtSignedMoney, fmtTime } from "../lib/format";

export function TradesPage() {
  const [outcome, setOutcome] = useState<"" | "win" | "loss">("");
  const trades = useQuery({
    queryKey: ["trades", outcome],
    queryFn: () => fetchTrades(outcome || undefined),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Trade log</h1>

      <div className="flex items-center gap-3 text-xs text-slate-500">
        <label className="flex items-center gap-2">
          outcome
          <select
            value={outcome}
            onChange={(e) => setOutcome(e.target.value as "" | "win" | "loss")}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-300"
          >
            <option value="">any</option>
            <option value="win">win</option>
            <option value="loss">loss</option>
          </select>
        </label>
        <span>the only permitted mutation here is a journal note (audited)</span>
      </div>

      {trades.isPending ? (
        <Loading />
      ) : trades.error ? (
        <ErrorNote error={trades.error} />
      ) : (
        <div className="space-y-4">
          {trades.data?.map((t) => <TradeRow key={t.trade_id} trade={t} />)}
          {trades.data && trades.data.length === 0 && (
            <p className="p-6 text-center text-sm text-slate-500">No trades recorded yet.</p>
          )}
        </div>
      )}
    </div>
  );
}

function TradeRow({ trade }: { trade: TradeListItem }) {
  const [open, setOpen] = useState(false);
  const detail = useQuery({
    queryKey: ["trade-detail", trade.trade_id],
    queryFn: () => fetchTradeDetail(trade.trade_id),
    enabled: open,
  });
  const win = trade.outcome === "win";

  return (
    <Card>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 text-left"
        aria-expanded={open}
      >
        <Chip tone={win ? "positive" : "negative"}>
          {trade.asset_id} · {trade.exit_reason}
        </Chip>
        <span className={trade.direction === 1 ? "text-emerald-300" : "text-red-300"}>
          {trade.direction === 1 ? "long" : "short"} {fmtQty(trade.entry_price)} →{" "}
          {fmtQty(trade.exit_price)}
        </span>
        <span className="ml-auto font-mono text-xs text-slate-500">
          {fmtTime(trade.entry_timestamp)} → {fmtTime(trade.exit_timestamp)}
        </span>
        <span
          className={
            win ? "font-mono text-sm text-emerald-300" : "font-mono text-sm text-red-300"
          }
        >
          {fmtSignedMoney(trade.net_pnl)}
        </span>
        <span aria-hidden className="text-slate-600">
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="mt-4 space-y-4 border-t border-slate-800 pt-4">
          {detail.isPending ? (
            <Loading label="loading timeline…" />
          ) : detail.error ? (
            <ErrorNote error={detail.error} />
          ) : (
            <>
              <TradeTimeline steps={detail.data?.timeline ?? []} />
              <JournalNoteForm
                tradeId={trade.trade_id}
                hasJournal={Boolean(detail.data?.journal)}
              />
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function JournalNoteForm({
  tradeId,
  hasJournal,
}: {
  tradeId: string;
  hasJournal: boolean;
}) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const mutation = useMutation({
    mutationFn: (noteText: string) => addJournalNote(tradeId, "web", noteText),
    onSuccess: () => {
      setText("");
      qc.invalidateQueries({ queryKey: ["trade-detail", tradeId] });
    },
  });

  if (!hasJournal) {
    return (
      <p className="text-xs text-slate-600">
        This trade has no journal entry yet — notes attach to the journal created with the
        position.
      </p>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (text.trim()) mutation.mutate(text.trim());
      }}
      className="flex items-center gap-2"
    >
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="journal note (stored verbatim, audited)"
        className="flex-1 rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-sm"
      />
      <button
        type="submit"
        disabled={!text.trim() || mutation.isPending}
        className="rounded bg-sky-800 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
      >
        add note
      </button>
      {mutation.error && (
        <span className="text-xs text-red-400">{(mutation.error as Error).message}</span>
      )}
      {mutation.isSuccess && <span className="text-xs text-emerald-400">saved</span>}
    </form>
  );
}
