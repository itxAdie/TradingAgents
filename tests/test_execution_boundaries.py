"""Phase-5 boundary guard: who may know about live execution (§6).

Direction rule: research | agents | graph | paper must NEVER import the
brokers or execution packages. The reverse is allowed — execution consumes
the shared research signal schema by design.

Also pins basic secret hygiene inside the new surface.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tradingagents"

ISOLATED_ROOTS = frozenset({"research", "agents", "graph", "paper"})
EXECUTION_ROOTS = frozenset({"brokers", "execution"})

SECRET_PATTERNS = (
    re.compile(r"(sk|pk)-[A-Za-z0-9]{16,}"),  # openai-style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # aws access key ids
    re.compile(r"(?i)(api_key|secret|password)\s*=\s*[\"'][^\"']{8,}[\"']"),
)


def package_files(package: str) -> list[Path]:
    base = SRC / package
    assert base.exists(), f"{package} package missing"
    return sorted(base.rglob("*.py"))


def _all_import_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


@pytest.mark.parametrize("isolated", sorted(ISOLATED_ROOTS))
@pytest.mark.parametrize("execution", sorted(EXECUTION_ROOTS))
def test_isolated_packages_never_import_execution(isolated: str, execution: str):
    for path in package_files(isolated):
        for module in _all_import_modules(path):
            top = module.split(".")[0]
            second = module.split(".")[1] if "." in module else ""
            assert not (
                top == "tradingagents" and second == execution
            ), f"{path.relative_to(ROOT)} imports {module!r} — isolated packages must not see {execution}"


@pytest.mark.parametrize("execution", sorted(EXECUTION_ROOTS))
def test_execution_surface_imports_stay_in_family(execution: str):
    """brokers/ + execution/ may use stdlib, pydantic, and first-party code;
    they must not pull venue SDKs directly (adapters own that seam)."""
    forbidden_venues = {
        "ccxt",
        "alpaca",
        "alpaca_trade_api",
        "binance",
        "ib_insync",
        "ibapi",
        "MetaTrader5",
        "oandapyV20",
    }
    for pkg in EXECUTION_ROOTS:
        for path in package_files(pkg):
            for module in _all_import_modules(path):
                assert module.split(".")[0] not in forbidden_venues, (
                    f"{path.relative_to(ROOT)} imports venue SDK {module!r}; "
                    "venue SDKs belong behind the BrokerAdapter protocol"
                )
    del execution


def test_no_hardcoded_secrets_in_live_surface():
    scanned = [p for pkg in ("brokers", "execution") for p in package_files(pkg)]
    scanned += [ROOT / "cli" / "broker.py", ROOT / "tradingagents" / "api" / "routes" / "broker.py"]
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            assert match is None, f"{path.name}: possible hardcoded secret {match.group(0)!r}"


def test_broker_registry_has_no_default():
    from tradingagents.brokers.registry import build_broker

    with pytest.raises(ValueError):
        build_broker("")  # empty name must fail closed


def test_sandbox_cannot_be_armed_for_live_environment(tmp_path, monkeypatch):
    """Even with every env flag forced on, sandbox+live is refused outright."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BROKER_ENVIRONMENT", "live")
    monkeypatch.delenv("BROKER_ACCOUNT_ID", raising=False)

    from tradingagents.execution.config import load_live_execution_config

    with pytest.raises(ValueError, match="sandbox"):
        load_live_execution_config(
            broker_name="sandbox",
            cache_dir=tmp_path,
            account_id="sbx-test",
        )
