"""Structured trade ledger.

Every simulated trade produces one immutable
:class:`TradeRecord`; the ledger exports JSON and CSV (pandas is already a
repo dependency). Records carry full cost/price provenance so any reported
number can be recomputed from raw fills.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field


class TradeRecord(BaseModel):
    """One completed simulated round trip."""

    trade_id: str
    run_id: str
    strategy_id: str
    asset_id: str
    timeframe: str
    direction: int  # +1 long / -1 short
    signal_generated_at: datetime
    entry_timestamp: datetime
    entry_price: float  # adverse-adjusted fill
    raw_entry_price: float  # market price actually touched (pre-costs)
    exit_timestamp: datetime
    exit_price: float
    raw_exit_price: float
    quantity: float
    stop_loss: float | None = None
    take_profit: float | None = None
    gross_pnl: float
    transaction_costs: float
    net_pnl: float = Field()
    return_pct: float  # net P&L / entry notional * 100
    holding_period: str  # ISO-8601 duration
    bars_held: int
    exit_reason: str  # stop_loss | take_profit | signal_exit | end_of_data


class TradeLedger:
    """Append-only list of :class:`TradeRecord` with export helpers."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.records: list[TradeRecord] = []
        self._next_id = 1

    def append(
        self,
        *,
        strategy_id: str,
        asset_id: str,
        timeframe: str,
        direction: int,
        signal_generated_at: datetime,
        entry_time: datetime,
        entry_price: float,
        raw_entry_price: float,
        exit_time: datetime,
        exit_price: float,
        raw_exit_price: float,
        quantity: float,
        stop_loss: float | None,
        take_profit: float | None,
        gross_pnl: float,
        transaction_costs: float,
        bars_held: int,
        exit_reason: str,
    ) -> TradeRecord:
        net_pnl = gross_pnl - transaction_costs
        record = TradeRecord(
            trade_id=f"{self.run_id}-T{self._next_id:04d}",
            run_id=self.run_id,
            strategy_id=strategy_id,
            asset_id=asset_id,
            timeframe=timeframe,
            direction=direction,
            signal_generated_at=signal_generated_at.astimezone(timezone.utc),
            entry_timestamp=entry_time.astimezone(timezone.utc),
            entry_price=entry_price,
            raw_entry_price=raw_entry_price,
            exit_timestamp=exit_time.astimezone(timezone.utc),
            exit_price=exit_price,
            raw_exit_price=raw_exit_price,
            quantity=quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            gross_pnl=gross_pnl,
            transaction_costs=transaction_costs,
            net_pnl=net_pnl,
            return_pct=(
                net_pnl / (entry_price * quantity) * 100 if entry_price * quantity else 0.0
            ),
            holding_period=str(exit_time - entry_time),
            bars_held=bars_held,
            exit_reason=exit_reason,
        )
        self._next_id += 1
        self.records.append(record)
        return record

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.records)

    def to_json(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(r.model_dump_json()) for r in self.records]
        path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        return path

    def to_csv(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r.model_dump(mode="json") for r in self.records]
        pd.DataFrame(rows).to_csv(path, index=False)
        return path


__all__ = ["TradeLedger", "TradeRecord"]
