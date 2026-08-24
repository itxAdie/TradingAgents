"""Paper-trading persistence.

JSON-file state store behind the ``PaperStateStore`` protocol so a later
Postgres/cloud implementation can replace it without touching the engine
(ARCHITECTURE.md P3.4).

Layout (one directory per environment/account — multi-account by design):

    {root}/{environment}/{account_id}/
        state.json            AccountState (atomic rewrite)
        positions.json        list[PositionRecord] (atomic rewrite)
        orders.jsonl          append-only PaperOrderEvent transitions
        signals/{id}.json     PaperSignalRecord (atomic rewrite on change)
        signals_index.jsonl   append-only signal transitions
        trades.jsonl          append-only TradeRecord lines
        journal/{trade}.json  JournalEntry
        equity_curve.jsonl    append-only EquitySnapshot lines
        daily.jsonl           append-only DailyPerformanceRow lines
        scheduler.json        per-schedule runtime bookkeeping

Durability rules:
- writes are atomic (tmp file + ``os.replace``) or append-only;
- loads are schema-validated; *any* corruption raises ``PaperStateError``
  loudly — the engine never resets or "repairs" state silently;
- append-only jsonl files tolerate leftover ``.tmp`` siblings from crashed
  writers (ignored, never read).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, RootModel

from tradingagents.backtest.ledger import TradeRecord
from tradingagents.paper.models import (
    AccountState,
    DailyPerformanceRow,
    EquitySnapshot,
    JournalEntry,
    JournalNote,
    PaperOrderEvent,
    PaperSignalRecord,
    PositionRecord,
)

SCHEMA_VERSION = 1

_M = TypeVar("_M", bound=BaseModel)


class PaperStateError(RuntimeError):
    """Raised when persisted paper-trading state is missing or corrupt."""


class PaperStateStore(Protocol):
    """Persistence seam — swap for a DB backend without engine changes."""

    def load_account(self) -> AccountState: ...
    def save_account(self, state: AccountState) -> None: ...
    def set_halted(self, halted: bool, reason: str = "") -> None: ...

    def load_positions(self) -> list[PositionRecord]: ...
    def save_positions(self, positions: list[PositionRecord]) -> None: ...

    def load_signal(self, signal_id: str) -> PaperSignalRecord | None: ...
    def save_signal(self, record: PaperSignalRecord) -> None: ...
    def append_signal_transition(
        self, *, signal_id: str, from_state: str, to_state: str, reason: str
    ) -> None: ...

    def append_order_event(self, event: PaperOrderEvent) -> None: ...
    def load_order_events(self) -> list[PaperOrderEvent]: ...

    def append_trade(self, record: TradeRecord) -> None: ...
    def load_trades(self) -> list[TradeRecord]: ...

    def append_equity(self, snapshot: EquitySnapshot) -> None: ...
    def load_equity_curve(self) -> list[EquitySnapshot]: ...

    def append_daily(self, row: DailyPerformanceRow) -> None: ...
    def load_daily_folded(self) -> list[DailyPerformanceRow]: ...

    def load_journal(self, trade_id: str) -> JournalEntry | None: ...
    def save_journal(self, entry: JournalEntry) -> None: ...
    def add_journal_note(self, trade_id: str, note: JournalNote) -> bool: ...

    def load_schedule_state(self, key: str) -> dict | None: ...
    def save_schedule_state(self, key: str, state: dict) -> None: ...


def _read_model(path: Path, model_cls: type[_M]) -> _M:
    try:
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except Exception as exc:  # corrupt payload — loud failure, never silent
        raise PaperStateError(f"corrupt paper state file {path}: {exc}") from exc


def _write_model(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(model.model_dump_json(indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _append_line(path: Path, model: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(model.model_dump_json() + "\n")


def _read_lines(path: Path, model_cls: type[_M]) -> list[_M]:
    if not path.exists():
        return []
    items: list[_M] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(model_cls.model_validate_json(line))
            except Exception as exc:
                raise PaperStateError(
                    f"corrupt line {line_no} in {path}: {exc}"
                ) from exc
    return items


class JsonPaperStateStore:
    """Default JSON implementation of :class:`PaperStateStore`."""

    def __init__(self, root: Path, *, environment: str, account_id: str):
        self._base = Path(root) / environment / account_id
        self._base.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------------------

    @property
    def base_dir(self) -> Path:
        return self._base

    def _signal_path(self, signal_id: str) -> Path:
        return self._base / "signals" / f"{signal_id}.json"

    def _journal_path(self, trade_id: str) -> Path:
        return self._base / "journal" / f"{trade_id}.json"

    @property
    def _orders_path(self) -> Path:
        return self._base / "orders.jsonl"

    @property
    def _signals_index_path(self) -> Path:
        return self._base / "signals_index.jsonl"

    @property
    def _trades_path(self) -> Path:
        return self._base / "trades.jsonl"

    @property
    def _equity_path(self) -> Path:
        return self._base / "equity_curve.jsonl"

    @property
    def _daily_path(self) -> Path:
        return self._base / "daily.jsonl"

    @property
    def _positions_path(self) -> Path:
        return self._base / "positions.json"

    @property
    def _scheduler_path(self) -> Path:
        return self._base / "scheduler.json"

    # -- account ---------------------------------------------------------------

    def create_account(self, state: AccountState) -> None:
        path = self._base / "state.json"
        if path.exists():
            raise PaperStateError(f"account already initialised at {path}")
        _write_model(path, state)

    def load_account(self) -> AccountState:
        path = self._base / "state.json"
        if not path.exists():
            raise PaperStateError(f"no paper account initialised at {path}")
        state = _read_model(path, AccountState)
        if state.schema_version != SCHEMA_VERSION:
            raise PaperStateError(
                f"unsupported account schema_version {state.schema_version} "
                f"(this build supports {SCHEMA_VERSION}); refusing to guess"
            )
        if state.account_id != self._base.name:
            raise PaperStateError(
                f"account id mismatch: state says {state.account_id!r}, "
                f"directory is {self._base.name!r}"
            )
        return state

    def save_account(self, state: AccountState) -> None:
        _write_model(self._base / "state.json", state)

    def set_halted(self, halted: bool, reason: str = "") -> None:
        state = self.load_account()
        state.halted = halted
        state.halt_reason = reason if halted else ""
        state.updated_at = datetime.now(timezone.utc)
        self.save_account(state)

    # -- positions ---------------------------------------------------------------

    def load_positions(self) -> list[PositionRecord]:
        return _read_model(self._positions_path, _PositionList).root

    def save_positions(self, positions: list[PositionRecord]) -> None:
        _write_model(self._positions_path, _PositionList(root=positions))

    # -- signals ---------------------------------------------------------------

    def load_signal(self, signal_id: str) -> PaperSignalRecord | None:
        path = self._signal_path(signal_id)
        if not path.exists():
            return None
        return _read_model(path, PaperSignalRecord)

    def save_signal(self, record: PaperSignalRecord) -> None:
        _write_model(self._signal_path(record.signal_id), record)

    def append_signal_transition(
        self, *, signal_id: str, from_state: str, to_state: str, reason: str
    ) -> None:
        _append_line(
            self._signals_index_path,
            _SignalTransition(
                signal_id=signal_id,
                from_state=from_state,
                to_state=to_state,
                reason=reason,
            ),
        )

    # -- orders ---------------------------------------------------------------

    def append_order_event(self, event: PaperOrderEvent) -> None:
        _append_line(self._orders_path, event)

    def load_order_events(self) -> list[PaperOrderEvent]:
        return _read_lines(self._orders_path, PaperOrderEvent)

    # -- trades ---------------------------------------------------------------

    def append_trade(self, record: TradeRecord) -> None:
        _append_line(self._trades_path, record)

    def load_trades(self) -> list[TradeRecord]:
        return _read_lines(self._trades_path, TradeRecord)

    # -- equity curve ---------------------------------------------------------------

    def append_equity(self, snapshot: EquitySnapshot) -> None:
        _append_line(self._equity_path, snapshot)

    def load_equity_curve(self) -> list[EquitySnapshot]:
        return _read_lines(self._equity_path, EquitySnapshot)

    # -- daily performance ---------------------------------------------------------------

    def append_daily(self, row: DailyPerformanceRow) -> None:
        _append_line(self._daily_path, row)

    def load_daily_folded(self) -> list[DailyPerformanceRow]:
        """One row per date — the LAST row written for a date wins."""
        folded: dict[str, DailyPerformanceRow] = {}
        for row in _read_lines(self._daily_path, DailyPerformanceRow):
            folded[row.date] = row
        return [folded[date] for date in sorted(folded)]

    # -- journal ---------------------------------------------------------------

    def load_journal(self, trade_id: str) -> JournalEntry | None:
        path = self._journal_path(trade_id)
        if not path.exists():
            return None
        return _read_model(path, JournalEntry)

    def save_journal(self, entry: JournalEntry) -> None:
        _write_model(self._journal_path(entry.trade_id), entry)

    def add_journal_note(self, trade_id: str, note: JournalNote) -> bool:
        entry = self.load_journal(trade_id)
        if entry is None:
            return False
        entry.notes.append(note)
        self.save_journal(entry)
        return True

    # -- scheduler bookkeeping ---------------------------------------------------------------

    def load_schedule_state(self, key: str) -> dict | None:
        path = self._scheduler_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise PaperStateError(f"corrupt paper state file {path}: {exc}") from exc
        return data.get(key)

    def save_schedule_state(self, key: str, state: dict) -> None:
        path = self._scheduler_path
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:
            raise PaperStateError(f"corrupt paper state file {path}: {exc}") from exc
        data[key] = state
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


class _PositionList(RootModel[list[PositionRecord]]):
    pass


class _SignalTransition(BaseModel):
    signal_id: str
    from_state: str
    to_state: str
    reason: str = ""


__all__ = ["JsonPaperStateStore", "PaperStateError", "PaperStateStore", "SCHEMA_VERSION"]
