"""Realtime endpoints: SSE stream + replay history (spec §29/§30).

The bus is thread-based; each SSE client gets one small pump thread that
bridges events onto that request's asyncio queue. Disconnects stop the pump
within its poll interval, so no request ever holds a worker slot hostage.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from tradingagents.api.context import AppContext
from tradingagents.api.eventbus import Subscriber

router = APIRouter(tags=["events"])

_HEARTBEAT_SECONDS = 15.0


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _sse(event: dict) -> str:
    name = event.get("event", "message")
    data = json.dumps(event, separators=(",", ":"), default=str)
    return f"event: {name}\ndata: {data}\n\n"


@router.get("/events/stream")
async def event_stream(
    request: Request,
    ctx: Annotated[AppContext, Depends(get_ctx)],
    replay: Annotated[int, Query(ge=0, le=200)] = 50,
):
    """Server-Sent Events: engine lifecycle, prices, backtests, audit.

    Reconnect-safe: browsers auto-reconnect (``retry`` hint) and short gaps
    are covered by history replay; heartbeat comments keep proxies open.

    Subscription happens in the handler (before headers are returned) so
    nothing published while the response streams is ever missed, even if
    the client is slow to start reading the body.
    """
    sub: Subscriber = ctx.bus.subscribe()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            item = sub.get(timeout=0.5)
            if item is not None:
                asyncio.run_coroutine_threadsafe(queue.put(item), loop)

    pump_thread = threading.Thread(target=pump, daemon=True, name="sse-pump")
    pump_thread.start()

    async def generator():
        try:
            yield "retry: 3000\n\n"
            if replay:
                for evt in ctx.bus.history(replay):
                    yield _sse(evt)
            while True:
                # Disconnection surfaces as task cancellation from the
                # server (StreamingResponse races a disconnect listener);
                # awaiting is_disconnected() here would stall quiet streams.
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=_HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                else:
                    yield _sse(item)
        finally:
            stop.set()
            ctx.bus.unsubscribe(sub)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/events/history")
def event_history(
    ctx: Annotated[AppContext, Depends(get_ctx)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    from tradingagents.api.schemas import EventHistoryPage, EventRow

    rows = ctx.bus.history(limit)
    return EventHistoryPage(
        items=[EventRow(**e) for e in rows],
        total=len(rows),
        limit=limit,
        offset=0,
    )


__all__ = ["router"]
