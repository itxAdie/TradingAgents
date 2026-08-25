"""Broker adapter layer (P5).

The ONLY place in the codebase permitted to import a broker SDK. Outside
this package, code depends exclusively on
:class:`tradingagents.brokers.base.BrokerAdapter`. The registry exposes the
sandbox adapter for now; real venues plug in via :func:`register_broker`
without touching call sites.
"""

from tradingagents.brokers.base import (
    BrokerAccountSnapshot,
    BrokerAdapter,
    BrokerError,
    BrokerOrderInfo,
    BrokerOrderStatus,
    BrokerPosition,
    ConnectionStatus,
    ErrorClass,
    SubmitOutcome,
)
from tradingagents.brokers.registry import (
    available_brokers,
    build_broker,
    register_broker,
)

__all__ = [
    "BrokerAccountSnapshot",
    "BrokerAdapter",
    "BrokerError",
    "BrokerOrderInfo",
    "BrokerOrderStatus",
    "BrokerPosition",
    "ConnectionStatus",
    "ErrorClass",
    "SubmitOutcome",
    "available_brokers",
    "build_broker",
    "register_broker",
]
