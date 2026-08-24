"""Persistence: atomic writes, loud corruption, schema guards (paper/store.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingagents.paper.models import (
    AccountState,
    DailyPerformanceRow,
    EquitySnapshot,
    JournalEntry,
    JournalNote,
    OrderState,
    PaperOrderEvent,
    PaperSignalRecord,
    PositionRecord,
    ResearchSnapshot,
    SignalState,
)
from tradingagents.paper.store import (
    SCHEMA_VERSION,
    JsonPaperStateStore,
    PaperStateError,
)
from tradingagents.research.schemas import SignalAction

NOW = datetime(2026, 8, 24, 13, 5, tzinfo=timezone.utc)


def make_store(root: Path, *, env: str = "test", account: str = "a1") -> JsonPaperStateStore:
    return JsonPaperStateStore(root, environment=env, account_id=account)


def make_account(**overrides) -> AccountState:
    fields = {
        "account_id": "a1",
        "environment": "test",
        "initial_capital": 10_000.0,
        "cash": 10_000.0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return AccountState(**fields)


class TestAccountLifecycle:
    def test_create_then_load_roundtrip(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create_account(make_account())
        loaded = store.load_account()
        assert loaded.cash == 10_000.0
        assert loaded.schema_version == SCHEMA_VERSION

    def test_create_refuses_overwrite(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create_account(make_account())
        with pytest.raises(PaperStateError, match="already initialised"):
            store.create_account(make_account())

    def test_load_missing_is_loud(self, tmp_path: Path) -> None:
        with pytest.raises(PaperStateError, match="no paper account"):
            make_store(tmp_path).load_account()

    def test_schema_version_mismatch_refused(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create_account(make_account(schema_version=99))
        with pytest.raises(PaperStateError, match="schema_version"):
            store.load_account()

    def test_directory_account_mismatch_detected(self, tmp_path: Path) -> None:
        store = make_store(tmp_path, env="test", account="a1")
        store.create_account(make_account(account_id="a1"))
        other = JsonPaperStateStore(tmp_path, environment="test", account_id="other")
        # state.json lives in a1/; "other" has none — but simulate a copy:
        (tmp_path / "test" / "other" / "state.json").write_text(
            (tmp_path / "test" / "a1" / "state.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with pytest.raises(PaperStateError, match="account id mismatch"):
            other.load_account()

    def test_multi_account_isolation(self, tmp_path: Path) -> None:
        s1 = make_store(tmp_path, account="one")
        s2 = make_store(tmp_path, account="two")
        s1.create_account(make_account(account_id="one", cash=111.0))
        s2.create_account(make_account(account_id="two", cash=222.0))
        assert s1.load_account().cash == 111.0
        assert s2.load_account().cash == 222.0

    def test_set_halted_roundtrip(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.create_account(make_account())
        store.set_halted(True, "operator")
        assert store.load_account().halted is True
        assert store.load_account().halt_reason == "operator"
        store.set_halted(False)
        assert store.load_account().halted is False
        assert store.load_account().halt_reason == ""


class TestCorruptionIsLoud:
    @pytest.fixture()
    def populated(self, tmp_path: Path) -> JsonPaperStateStore:
        store = make_store(tmp_path)
        store.create_account(make_account())
        return store

    def _corrupt_first_line(self, path: Path, payload: str = "{not json") -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[0] = payload
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_corrupt_order_line_raises(self, populated: JsonPaperStateStore) -> None:
        populated.append_order_event(
            PaperOrderEvent(
                ts=NOW,
                order_id="o1",
                signal_id="s1",
                account_id="a1",
                asset_id="XAUUSD",
                timeframe="1h",
                action="BUY",
                from_state=OrderState.SIGNAL,
                to_state=OrderState.PENDING,
            )
        )
        self._corrupt_first_line(populated.base_dir / "orders.jsonl")
        with pytest.raises(PaperStateError, match="corrupt line 1"):
            populated.load_order_events()

    def test_corrupt_state_json_detected_on_load(self, populated: JsonPaperStateStore) -> None:
        (populated.base_dir / "state.json").write_text("{oops")
        with pytest.raises(PaperStateError, match="corrupt paper state file"):
            populated.load_account()


class TestJsonlRoundtrips:
    def test_positions_roundtrip(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        recs = [
            PositionRecord(
                position_id=f"p{i}",
                account_id="a1",
                signal_id=f"s{i}",
                asset_id="XAUUSD",
                timeframe="1h",
                direction=-1,
                quantity=0.25,
                entry_price=2000.0,
                entry_time=NOW,
                updated_at=NOW,
            )
            for i in range(2)
        ]
        store.save_positions(recs)
        assert store.load_positions() == recs

    def test_trades_and_equity_append_order_preserved(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        for i in range(3):
            store.append_equity(
                EquitySnapshot(
                    timestamp=NOW + timedelta(minutes=i),
                    equity=10_000.0 + i,
                    cash=10_000.0,
                    exposure=0.0,
                    open_positions=0,
                    drawdown_pct=0.0,
                )
            )
        curve = store.load_equity_curve()
        assert [p.equity for p in curve] == [10_000.0, 10_001.0, 10_002.0]

    def test_signals_saved_and_loaded_by_id(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        record = PaperSignalRecord(
            signal_id="abc",
            account_id="a1",
            environment="test",
            asset_id="XAUUSD",
            timeframe="1h",
            state=SignalState.GENERATED,
            decision_bar_close=NOW,
            generated_at=NOW,
            action=SignalAction.HOLD,
            confidence=0.5,
            thesis="t",
            research=ResearchSnapshot(
                thesis="t", confidence=0.5, generated_at=NOW, research_version="v1"
            ),
            updated_at=NOW,
        )
        store.save_signal(record)
        loaded = store.load_signal("abc")
        assert loaded is not None and loaded.signal_id == "abc"
        assert store.load_signal("missing") is None
        record2 = record.with_transition(
            new_state=SignalState.REJECTED, reason="x", at=NOW
        )
        store.save_signal(record2)
        assert store.load_signal("abc").state is SignalState.REJECTED

    def test_journal_notes_persist(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        entry = JournalEntry(
            trade_id="t1",
            signal_id="s1",
            account_id="a1",
            asset_id="XAUUSD",
            timeframe="1h",
            direction=1,
            opened_at=NOW,
            snapshot=ResearchSnapshot(
                thesis="t", confidence=0.7, generated_at=NOW, research_version="v1"
            ),
        )
        store.save_journal(entry)
        assert store.add_journal_note(
            "t1", JournalNote(timestamp=NOW, text="note one")
        )
        loaded = store.load_journal("t1")
        assert loaded.notes[0].text == "note one"
        assert not store.add_journal_note("ghost", JournalNote(timestamp=NOW, text="x"))

    def test_daily_fold_last_write_wins(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        rows = [
            DailyPerformanceRow(
                date="2026-08-24",
                starting_equity=100.0,
                ending_equity=a,
                daily_return_pct=a - 100.0,
            )
            for a in (101.0, 105.0, 103.0)
        ]
        for row in rows:
            store.append_daily(row)
        folded = store.load_daily_folded()
        assert len(folded) == 1
        assert folded[0].ending_equity == 103.0

    def test_scheduler_bookkeeping_per_key(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        assert store.load_schedule_state("XAUUSD:1h") is None
        store.save_schedule_state("XAUUSD:1h", {"last_run_at": NOW.isoformat()})
        store.save_schedule_state("BTCUSD:15m", {"last_run_at": NOW.isoformat()})
        assert store.load_schedule_state("XAUUSD:1h")["last_run_at"] == NOW.isoformat()
        assert set(json.loads((store.base_dir / "scheduler.json").read_text())) == {
            "XAUUSD:1h",
            "BTCUSD:15m",
        }
