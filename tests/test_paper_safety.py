"""Structural safety: no broker code may ever exist under tradingagents/paper/.

Phase 3 is simulation-only (PROJECT_RULES §24/§26); broker integration is a
Phase 5 concern behind its own review. This test fails if anyone imports a
broker SDK or raw network client from the paper package.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PAPER_DIR = Path(__file__).resolve().parents[1] / "tradingagents" / "paper"

FORBIDDEN_ROOTS = frozenset(
    {
        "ccxt",
        "alpaca",
        "alpaca_trade_api",
        "binance",
        "binance_f",
        "python_binance",
        "ib_insync",
        "ibapi",
        "oandapyV20",
        "MetaTrader5",
        "websocket",
    }
)


def paper_python_files() -> list[Path]:
    assert PAPER_DIR.exists(), "paper package missing"
    return sorted(PAPER_DIR.rglob("*.py"))


def _all_import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


class TestNoBrokerImports:
    @pytest.mark.parametrize("path", paper_python_files(), ids=lambda p: p.name)
    def test_no_forbidden_import(self, path: Path) -> None:
        for module in _all_import_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_ROOTS, (
                f"{path.name}: forbidden broker/network import {module!r}"
            )

    def test_package_import_surface_stays_internal(self) -> None:
        """Every third-party top-level import in paper/ must be a known-safe one."""
        import sys

        stdlib = set(sys.stdlib_module_names)
        for path in paper_python_files():
            for module in _all_import_modules(path):
                root = module.split(".")[0]
                if root in stdlib or root == "tradingagents":
                    continue
                assert root in {"pydantic", "yaml"}, (
                    f"{path.name}: unexpected third-party import {module!r}"
                )

    def test_no_live_or_broker_words_in_code_paths(self) -> None:
        """Environment literal cannot grow a 'live' value silently."""
        from tradingagents.paper.config import Environment

        args = __import__("typing").get_args(Environment)
        assert set(args) == {"test", "paper"}


class TestSimulatedLabels:
    def test_report_carries_simulated_disclaimer(self) -> None:
        from tradingagents.paper.report import PAPER_DISCLAIMER

        text = PAPER_DISCLAIMER.upper()
        assert "SIMULATED" in text
        assert "NO REAL MONEY" in text or "NO BROKER" in text
