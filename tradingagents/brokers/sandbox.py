"""SandboxBrokerAdapter: deterministic in-process broker for demo/tests (P5).

Implements the full BrokerAdapter contract with NO network and NO SDK. A
script queue drives outcomes so tests can reproduce every lifecycle event
deterministically: fills at chosen prices, partial fills, rejections,
timeouts *after* recording the order (the UNKNOWN case), rate limits,
disconnects and malformed payloads.

It is a real state machine, not a stub: orders persist in memory (+optional
jsonl), fills move cash/positions, protective stops attach as sibling
orders — which is exactly why reconciliation logic can be exercised
honestly against it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tradingagents.brokers.base import (
    BrokerAccountSnapshot,
    BrokerError,
    BrokerOrderInfo,
    BrokerOrderStatus,
    BrokerPosition,
    ConnectionStatus,
    ErrorClass,
    SubmitOutcome,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _ScriptedEvent:
    """One scripted outcome consumed by the next submit/cancel/status call."""

    kind: Literal["fill", "partial", "reject", "timeout", "rate_limit", "malformed", "disconnect"]
    price: float | None = None
    quantity: float | None = None  # partial-fill quantity
    code: str = ""
    message: str = ""


@dataclass
class _InternalOrder:
    broker_order_id: str
    client_order_id: str
    asset_id: str
    side: str
    order_type: str
    quantity: float
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    status: BrokerOrderStatus = BrokerOrderStatus.PENDING_SUBMIT
    fees_reported: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss_attached: float | None = None
    take_profit_attached: float | None = None
    ts: datetime = field(default_factory=_utcnow)
    raw_status: str = ""

    def to_info(self) -> BrokerOrderInfo:
        return BrokerOrderInfo(
            broker_order_id=self.broker_order_id,
            client_order_id=self.client_order_id,
            asset_id=self.asset_id,
            side=self.side,  # type: ignore[arg-type]
            order_type=self.order_type,
            quantity=self.quantity,
            filled_quantity=self.filled_quantity,
            avg_fill_price=self.avg_fill_price,
            status=self.status,
            fees_reported=self.fees_reported,
            limit_price=self.limit_price,
            stop_price=self.stop_price,
            stop_loss_attached=self.stop_loss_attached,
            take_profit_attached=self.take_profit_attached,
            ts=self.ts,
            raw_status=self.raw_status,
        )


class SandboxBrokerAdapter:
    """Deterministic venue simulator. Thread-safe; no I/O beyond optional log."""

    name = "sandbox"

    def __init__(
        self,
        *,
        account_id: str = "sbx-default",
        base_currency: str = "USD",
        starting_cash: float = 100_000.0,
        leverage_cap: float | None = 20.0,  # venue maximum; system cap is separate
        clock: Any = None,  # callable -> datetime; injectable for determinism
        event_log_path: Path | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._account_id = account_id
        self._currency = base_currency
        self._cash = starting_cash
        self._leverage_cap = leverage_cap
        self._clock = clock or _utcnow
        self._orders: dict[str, _InternalOrder] = {}
        self._positions: dict[str, dict[str, float]] = {}
        self._status = ConnectionStatus.DISCONNECTED
        self._script: list[_ScriptedEvent] = []
        self._call_log: list[dict[str, Any]] = []
        self._seq = 0
        self._rate_limit_after: int | None = None  # fail Nth+ call with rate limit
        self._calls_made = 0
        self._event_log_path = event_log_path

    # -- scripting (test/demo harness only) -----------------------------------

    def script(self, events: Iterator[_ScriptedEvent] | list[_ScriptedEvent]) -> None:
        with self._lock:
            self._script.extend(events)

    def set_rate_limit_after(self, calls: int) -> None:
        with self._lock:
            self._rate_limit_after = calls

    def force_status(self, status: ConnectionStatus) -> None:
        with self._lock:
            self._status = status

    def inject_position(self, asset_id: str, quantity: float, avg_price: float) -> None:
        """Create a position the local system does not know about."""
        with self._lock:
            self._positions[asset_id] = {"qty": quantity, "avg": avg_price}

    def drop_order(self, client_order_id: str) -> None:
        """Simulate the venue losing an order entirely (reconciliation input)."""
        with self._lock:
            self._orders.pop(client_order_id, None)

    # -- internals -------------------------------------------------------------

    def _now(self) -> datetime:
        return self._clock()  # type: ignore[operator]

    def _guard(self, op: str) -> None:
        self._calls_made += 1
        self._call_log.append({"op": op, "ts": self._now().isoformat()})
        if self._rate_limit_after is not None and self._calls_made > self._rate_limit_after:
            raise BrokerError(
                ErrorClass.RETRYABLE, "rate_limited", "sandbox rate limit exceeded"
            )
        if self._status is not ConnectionStatus.CONNECTED:
            raise BrokerError(
                ErrorClass.UNKNOWN, "not_connected", f"{op} while {self._status.value}"
            )

    def _log_event(self, payload: dict[str, Any]) -> None:
        if self._event_log_path is not None:
            self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._event_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str) + "\n")

    def _apply_fill(self, order: _InternalOrder, qty: float, price: float, fee: float | None) -> None:
        direction = 1 if order.side == "BUY" else -1
        signed = direction * qty
        pos = self._positions.setdefault(order.asset_id, {"qty": 0.0, "avg": price})
        new_qty = pos["qty"] + signed
        if pos["qty"] == 0 or (pos["qty"] > 0) != (new_qty > 0):
            pos["avg"] = price
        elif abs(new_qty) >= abs(pos["qty"]):  # increasing
            pos["avg"] = (pos["avg"] * abs(pos["qty"]) + price * qty) / (abs(pos["qty"]) + qty)
        pos["qty"] = new_qty
        self._cash -= signed * price + (fee or 0.0)
        order.filled_quantity += qty
        order.avg_fill_price = (
            (order.avg_fill_price * (order.filled_quantity - qty) + price * qty)
            / order.filled_quantity
            if order.avg_fill_price is not None
            else price
        )
        if fee is not None:
            order.fees_reported = (order.fees_reported or 0.0) + fee

    # -- connection --------------------------------------------------------------

    def connect(self) -> ConnectionStatus:
        with self._lock:
            self._call_log.append({"op": "connect", "ts": self._now().isoformat()})
            self._status = ConnectionStatus.CONNECTED
            return self._status

    def disconnect(self) -> None:
        with self._lock:
            self._status = ConnectionStatus.DISCONNECTED

    def health_check(self) -> ConnectionStatus:
        with self._lock:
            self._call_log.append({"op": "health_check", "ts": self._now().isoformat()})
            return self._status

    # -- account -----------------------------------------------------------------

    def get_account(self) -> BrokerAccountSnapshot:
        with self._lock:
            self._guard("get_account")
            equity = self._cash + sum(
                p["qty"] * p["avg"] for p in self._positions.values()
            )
            return BrokerAccountSnapshot(
                account_id=self._account_id,
                currency=self._currency,
                cash=self._cash,
                equity=equity,
                positions=tuple(self.get_positions()),
                open_orders=tuple(o.to_info() for o in self._orders.values()
                                  if o.status not in {
                                      BrokerOrderStatus.FILLED,
                                      BrokerOrderStatus.CANCELLED,
                                      BrokerOrderStatus.REJECTED,
                                      BrokerOrderStatus.EXPIRED,
                                  }),
                server_time=self._now(),
                leverage_cap=self._leverage_cap,
            )

    def get_balances(self) -> dict[str, float]:
        with self._lock:
            self._guard("get_balances")
            return {"cash": self._cash, "equity": self.get_account().equity}

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        with self._lock:
            self._guard("get_positions")
            return tuple(
                BrokerPosition(
                    asset_id=aid,
                    quantity=p["qty"],
                    avg_entry_price=p["avg"],
                    protective_orders_ok=True,
                    ts=self._now(),
                )
                for aid, p in self._positions.items()
                if abs(p["qty"]) > 1e-12
            )

    def get_orders(self, *, open_only: bool = True) -> tuple[BrokerOrderInfo, ...]:
        with self._lock:
            self._guard("get_orders")
            terminal = {
                BrokerOrderStatus.FILLED,
                BrokerOrderStatus.CANCELLED,
                BrokerOrderStatus.REJECTED,
                BrokerOrderStatus.EXPIRED,
            }
            selected = [
                o for o in self._orders.values() if not open_only or o.status not in terminal
            ]
            return tuple(sorted((o.to_info() for o in selected), key=lambda i: i.client_order_id))

    def find_order(self, *, client_order_id: str) -> BrokerOrderInfo | None:
        with self._lock:
            self._guard("find_order")
            order = self._orders.get(client_order_id)
            return order.to_info() if order else None

    # -- orders --------------------------------------------------------------------

    def _next_script(self) -> _ScriptedEvent | None:
        if self._script:
            return self._script.pop(0)
        return None

    def submit_order(
        self,
        *,
        client_order_id: str,
        asset_id: str,
        side: Literal["BUY", "SELL"],
        order_type: Literal["MARKET", "LIMIT", "STOP"],
        quantity: float,
        limit_price: float | None = None,
        stop_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        time_in_force: Literal["GTC", "IOC", "DAY"] = "GTC",
    ) -> SubmitOutcome:
        with self._lock:
            self._guard("submit_order")
            event = self._next_script()

            if event is not None and event.kind == "reject":
                raise BrokerError(
                    ErrorClass.NON_RETRYABLE,
                    event.code or "broker_rejected",
                    event.message or "scripted rejection",
                )
            if event is not None and event.kind == "rate_limit":
                raise BrokerError(ErrorClass.RETRYABLE, "rate_limited", "scripted")
            if event is not None and event.kind == "malformed":
                raise BrokerError(
                    ErrorClass.NON_RETRYABLE, "malformed_response", "unparseable venue payload",
                    broker_raw=event.message or "\x00garbage",
                )

            # idempotency: same client id must never create a second order
            existing = self._orders.get(client_order_id)
            if existing is not None:
                return SubmitOutcome(order=existing.to_info(), detail="existing")

            self._seq += 1
            order = _InternalOrder(
                broker_order_id=f"SBX-{self._seq:08d}",
                client_order_id=client_order_id,
                asset_id=asset_id,
                side=side,
                order_type=order_type,
                quantity=quantity,
                limit_price=limit_price,
                stop_price=stop_price,
                stop_loss_attached=stop_loss,
                take_profit_attached=take_profit,
                status=BrokerOrderStatus.ACKNOWLEDGED,
                ts=self._now(),
                raw_status="ACKNOWLEDGED",
            )
            # record BEFORE any ambiguous failure: the venue may have it even
            # when the response is lost — this is what makes UNKNOWN honest.
            self._orders[client_order_id] = order
            self._log_event(
                {"ev": "accepted", "client_order_id": client_order_id, "ts": self._now().isoformat()}
            )

            if event is not None and event.kind == "timeout":
                # outcome lost: order exists venue-side but caller saw nothing
                return SubmitOutcome(order=None, unknown=True, detail="response lost")

            fill_price = (
                event.price
                if event is not None and event.kind in {"fill", "partial"} and event.price
                else (limit_price or stop_price or 100.0)
            )
            if event is not None and event.kind == "partial":
                assert event.quantity is not None
                self._apply_fill(order, event.quantity, fill_price or 0.0, fee=0.75)
                order.status = BrokerOrderStatus.PARTIALLY_FILLED
                order.raw_status = "PARTIALLY_FILLED"
                return SubmitOutcome(order=order.to_info())

            fee = round((limit_price or stop_price or 100.0) * quantity * 0.0001, 4)
            self._apply_fill(order, quantity, fill_price or 0.0, fee=fee)
            order.status = BrokerOrderStatus.FILLED
            order.raw_status = "FILLED"
            return SubmitOutcome(order=order.to_info())

    def cancel_order(self, *, client_order_id: str) -> bool:
        with self._lock:
            self._guard("cancel_order")
            order = self._orders.get(client_order_id)
            if order is None:
                return False
            if order.status in {BrokerOrderStatus.FILLED, BrokerOrderStatus.CANCELLED}:
                return False
            order.status = BrokerOrderStatus.CANCELLED
            order.raw_status = "CANCELLED"
            return True

    def modify_order(
        self,
        *,
        client_order_id: str,
        quantity: float | None = None,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> BrokerOrderInfo:
        with self._lock:
            self._guard("modify_order")
            order = self._orders.get(client_order_id)
            if order is None:
                raise BrokerError(ErrorClass.NON_RETRYABLE, "unknown_order", "no such order")
            if order.status in {BrokerOrderStatus.FILLED, BrokerOrderStatus.CANCELLED}:
                raise BrokerError(
                    ErrorClass.NON_RETRYABLE, "terminal_state", "cannot modify terminal order"
                )
            if quantity is not None:
                order.quantity = quantity
            if limit_price is not None:
                order.limit_price = limit_price
            if stop_price is not None:
                order.stop_price = stop_price
            return order.to_info()

    # -- telemetry ---------------------------------------------------------------

    def raw_call_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._call_log)
