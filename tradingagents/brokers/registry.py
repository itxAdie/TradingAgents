"""Broker registry: name -> adapter factory. Deliberately no default broker.

A real venue is added ONLY by registering it here after its adapter passes
the contract suite. Nothing may fall back to another broker silently — an
unknown name is a configuration error (fail closed).
"""

from __future__ import annotations

from collections.abc import Callable

from tradingagents.brokers.base import BrokerAdapter
from tradingagents.brokers.sandbox import SandboxBrokerAdapter

AdapterFactory = Callable[..., BrokerAdapter]

_REGISTRY: dict[str, AdapterFactory] = {
    SandboxBrokerAdapter.name: lambda **kw: SandboxBrokerAdapter(**kw),
}


def register_broker(name: str, factory: AdapterFactory) -> None:
    """Explicit registration point for future real adapters."""
    _REGISTRY[name] = factory


def available_brokers() -> list[str]:
    return sorted(_REGISTRY)


def build_broker(name: str, **kwargs: object) -> BrokerAdapter:
    """Construct the named adapter or raise — never substitute a fallback."""
    try:
        factory = _REGISTRY[name]
    except KeyError as exc:  # pragma: no cover - message clarity
        raise ValueError(
            f"unknown broker {name!r}; available: {', '.join(available_brokers())}"
        ) from exc
    adapter = factory(**kwargs)
    if adapter.name != name:
        raise ValueError(f"adapter name mismatch: requested {name!r}, got {adapter.name!r}")
    return adapter
