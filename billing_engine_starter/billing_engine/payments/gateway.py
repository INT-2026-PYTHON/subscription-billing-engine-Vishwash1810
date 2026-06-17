"""
PaymentGateway — abstract + two mock implementations.

In real life this would talk to Stripe / Razorpay / Adyen. For the project
we use mocks so tests are deterministic and the demo never hits the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import random

from billing_engine.models import Invoice


@dataclass(frozen=True)
class PaymentResult:
    success: bool
    failure_reason: Optional[str] = None


class PaymentGateway(ABC):
    @abstractmethod
    def charge(self, invoice: Invoice) -> PaymentResult:
        raise NotImplementedError


# ----------------------------------------------------------------
# Scripted — for deterministic tests
# ----------------------------------------------------------------
class ScriptedGateway(PaymentGateway):
    """Returns pre-set results from a queue. Used in tests.

    Example:
        gateway = ScriptedGateway([
            PaymentResult(False, "INSUFFICIENT_FUNDS"),
            PaymentResult(False, "INSUFFICIENT_FUNDS"),
            PaymentResult(True),
        ])
    """

    def __init__(self, results: list[PaymentResult]) -> None:
        self.results = list(results)
        self.index = 0

    def charge(self, invoice: Invoice) -> PaymentResult:
        if self.index >= len(self.results):
            raise IndexError(f"ScriptedGateway has no more results (requested index {self.index})")
        result = self.results[self.index]
        self.index += 1
        return result


# ----------------------------------------------------------------
# Fake-random — for the CLI demo
# ----------------------------------------------------------------
class FakeRandomGateway(PaymentGateway):
    """Succeeds at a configurable rate; seeded for reproducibility."""

    def __init__(self, success_rate: float = 0.7, seed: Optional[int] = None) -> None:
        if not (0 <= success_rate <= 1):
            raise ValueError(f"success_rate must be in [0, 1], got {success_rate}")
        self.success_rate = success_rate
        self.rng = random.Random(seed)

    def charge(self, invoice: Invoice) -> PaymentResult:
        if self.rng.random() < self.success_rate:
            return PaymentResult(success=True)
        else:
            return PaymentResult(success=False, failure_reason="DECLINED_BY_FRAUD_CHECK")
