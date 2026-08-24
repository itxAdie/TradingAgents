"""Trade journal endpoints: history, full lifecycle detail, note mutation.

The trade-detail timeline reconstructs the complete decision chain
(market data → research → signal → risk → order → entry → position → exit)
exclusively from persisted records and transitions (spec §18).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from tradingagents.api.context import AppContext
from tradingagents.api.schemas import JournalNoteIn


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


router = APIRouter(tags=["trades"])


@router.get("/trades")
def list_trades(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
    asset_id: str | None = None,
    direction: str | None = Query(None, pattern="^(long|short)$"),
    outcome: str | None = Query(None, pattern="^(win|loss)$"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    from tradingagents.api.schemas import TradeListItem, TradeListPage

    store = ctx.store(environment, account)
    records = store.load_trades()
    journals_present = {t.trade_id for t in records if store.load_journal(t.trade_id)}

    if asset_id:
        wanted = asset_id.strip().upper()
        records = [r for r in records if r.asset_id == wanted]
    if direction:
        wanted_dir = 1 if direction == "long" else -1
        records = [r for r in records if r.direction == wanted_dir]
    if outcome == "win":
        records = [r for r in records if r.net_pnl > 0]
    elif outcome == "loss":
        records = [r for r in records if r.net_pnl <= 0]
    if date_from:
        records = [r for r in records if r.exit_timestamp >= date_from]
    if date_to:
        records = [r for r in records if r.exit_timestamp <= date_to]

    records.sort(key=lambda r: r.exit_timestamp, reverse=True)
    total = len(records)
    page = records[offset : offset + limit]

    items = [
        TradeListItem(
            trade_id=r.trade_id,
            run_id=r.run_id,
            asset_id=r.asset_id,
            timeframe=r.timeframe,
            direction=r.direction,
            entry_timestamp=r.entry_timestamp,
            exit_timestamp=r.exit_timestamp,
            entry_price=r.entry_price,
            exit_price=r.exit_price,
            net_pnl=r.net_pnl,
            return_pct=r.return_pct,
            holding_period=r.holding_period,
            bars_held=r.bars_held,
            outcome="win" if r.net_pnl > 0 else "loss",
            exit_reason=r.exit_reason,
            has_journal=r.trade_id in journals_present,
            strategy_version=_strategy_version(store, r.trade_id),
        )
        for r in page
    ]
    return TradeListPage(items=items, total=total, limit=limit, offset=offset)


def _strategy_version(store, trade_id: str) -> str:
    journal = store.load_journal(trade_id)
    return journal.snapshot.research_version if journal else ""


@router.get("/trades/{trade_id}")
def trade_detail(
    trade_id: str,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
):
    from tradingagents.api.schemas import TimelineStage, TradeDetailResponse
    from tradingagents.paper.models import SignalState

    store = ctx.store(environment, account)
    match = [r for r in store.load_trades() if r.trade_id == trade_id]
    if not match:
        raise HTTPException(status_code=404, detail="trade not found")
    trade = match[0]

    journal = store.load_journal(trade_id)
    related_signal = None
    if journal is not None:
        related_signal = store.load_signal(journal.signal_id)
    signal = related_signal

    timeline: list[TimelineStage] = []
    if signal is not None:
        timeline.append(
            TimelineStage(
                stage="market_data",
                label="Market Data",
                timestamp=signal.market_data_timestamp or signal.decision_bar_close,
                detail=f"{signal.asset_id} {signal.timeframe} decision bar close "
                f"{signal.decision_bar_close.isoformat()}",
            )
        )
        timeline.append(
            TimelineStage(
                stage="research",
                label="Research",
                timestamp=signal.research.generated_at,
                detail=f"models: {', '.join(signal.research.models_used) or 'n/a'}; "
                f"version {signal.research.research_version}",
            )
        )
        timeline.append(
            TimelineStage(
                stage="signal",
                label="Signal",
                timestamp=signal.generated_at,
                detail=f"{signal.action.value} confidence {signal.confidence:.0%}",
            )
        )
        transitions = sorted(
            (t for t in store.list_signal_transitions() if t.signal_id == signal.signal_id),
            key=lambda t: t.ts or datetime.min.replace(tzinfo=timezone.utc),
        )
        risk_ts = next(
            (
                t.ts
                for t in transitions
                if t.to_state in (SignalState.ACCEPTED.value, SignalState.REJECTED.value)
            ),
            None,
        )
        approved = any(
            t.to_state == SignalState.ACCEPTED.value for t in transitions
        )
        timeline.append(
            TimelineStage(
                stage="risk_decision",
                label="Risk Decision",
                timestamp=risk_ts,
                detail=(
                    f"{'approved' if approved else 'rejected'}"
                    + (
                        ""
                        if approved
                        else f": {signal.rejection_reason}" if signal.rejection_reason else ""
                    )
                ),
            )
        )

    orders = [
        o.model_dump(mode="json")
        for o in _orders_for_trade(store, trade, journal)
    ]
    if orders:
        created = orders[0]["created_at"]
        timeline.append(
            TimelineStage(
                stage="paper_order",
                label="Paper Order",
                timestamp=created,
                detail=f"order {orders[0]['order_id']} ({orders[0]['action']})",
            )
        )
    timeline.append(
        TimelineStage(
            stage="entry",
            label="Entry",
            timestamp=trade.entry_timestamp,
            detail=f"{trade.quantity:g} @ {trade.entry_price:.4f} "
            f"(raw {trade.raw_entry_price:.4f})",
        )
    )
    if journal is not None:
        timeline.append(
            TimelineStage(
                stage="position",
                label="Position Open",
                timestamp=journal.opened_at,
                detail=trade.timeframe,
            )
        )
    timeline.append(
        TimelineStage(
            stage="exit",
            label="Exit",
            timestamp=trade.exit_timestamp,
            detail=f"{trade.exit_reason} @ {trade.exit_price:.4f} → net P&L "
            f"{trade.net_pnl:+.2f}",
        )
    )

    return TradeDetailResponse(
        trade=trade.model_dump(mode="json"),
        journal=journal,
        timeline=timeline,
        related_signal=related_signal,
    )


def _orders_for_trade(store, trade, journal):
    from tradingagents.paper.models import fold_order_events

    folded = fold_order_events(store.load_order_events())
    if journal is not None:
        return [o for o in folded.values() if o.signal_id == journal.signal_id]
    return [
        o
        for o in folded.values()
        if o.asset_id == trade.asset_id and o.state.value in ("executed", "open", "closed")
    ]


@router.put("/trades/{trade_id}/journal")
def add_journal_note(
    trade_id: str,
    note_in: JournalNoteIn,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    environment: str = "test",
    account: str = "paper-default",
):
    from tradingagents.paper.models import JournalNote

    store = ctx.store(environment, account)
    ok = store.add_journal_note(
        trade_id,
        JournalNote(timestamp=datetime.now(timezone.utc), author=note_in.author,
                    text=note_in.text),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="trade has no journal entry")
    ctx.audit.record("journal_note_added", trade_id=trade_id, account=account)
    return {"status": "ok", "trade_id": trade_id}


__all__ = ["router"]
