"""
Circuit Breaker — OpenStack API çağrılarını kademeli korur.

Durumlar:
  CLOSED  → normal çalışma
  OPEN    → art arda N hata sonrası hızlı fail (timeout saniye)
  HALF    → timeout dolunca tek deneme, başarılı → CLOSED, başarısız → OPEN

Her OpenStack servis endpoint'i için ayrı breaker kullanılır:
  nova_breaker    = CircuitBreaker(name="nova")
  keystone_breaker= CircuitBreaker(name="keystone")
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

import structlog

log = structlog.get_logger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN   = "open"
    HALF   = "half"


@dataclass
class BreakerMetrics:
    total_calls: int = 0
    failures: int = 0
    successes: int = 0
    short_circuits: int = 0   # OPEN iken engellenenler
    half_open_trials: int = 0


class CircuitBreakerOpen(Exception):
    """Circuit açıkken çağrı geldiğinde fırlatılır."""


class CircuitBreaker:
    """
    Async circuit breaker.

    Kullanım:
        breaker = CircuitBreaker(name="nova", threshold=5, timeout=30)

        result = await breaker.call(
            conn.compute.get_server, instance_id
        )
    """

    def __init__(
        self,
        name: str = "default",
        threshold: int = 5,      # kaç art arda hata sonrası OPEN
        timeout: float = 30.0,   # OPEN → HALF bekleme süresi (saniye)
        half_open_max: int = 1,  # HALF modunda kaç deneme
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.timeout = timeout
        self.half_open_max = half_open_max

        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._half_trials = 0
        self._lock = asyncio.Lock()
        self.metrics = BreakerMetrics()

    @property
    def state(self) -> BreakerState:
        return self._state

    async def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Fonksiyonu breaker korumasıyla çalıştır."""
        async with self._lock:
            await self._maybe_transition()

            if self._state == BreakerState.OPEN:
                self.metrics.short_circuits += 1
                raise CircuitBreakerOpen(
                    f"Circuit '{self.name}' OPEN — "
                    f"{self.timeout - (time.monotonic() - self._opened_at):.0f}s kaldı"
                )

            if self._state == BreakerState.HALF:
                self._half_trials += 1
                self.metrics.half_open_trials += 1

        self.metrics.total_calls += 1
        try:
            # fn sync ise to_thread ile çalıştır
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = await asyncio.to_thread(fn, *args, **kwargs)

            await self._on_success()
            return result

        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            await self._on_failure(exc)
            raise

    async def _maybe_transition(self) -> None:
        if self._state == BreakerState.OPEN:
            if time.monotonic() - self._opened_at >= self.timeout:
                self._state = BreakerState.HALF
                self._half_trials = 0
                log.info("circuit half-open", breaker=self.name)

    async def _on_success(self) -> None:
        async with self._lock:
            self.metrics.successes += 1
            if self._state in (BreakerState.HALF, BreakerState.CLOSED):
                self._failures = 0
                self._state = BreakerState.CLOSED
                log.debug("circuit closed", breaker=self.name)

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            self.metrics.failures += 1
            self._failures += 1
            log.warning(
                "circuit failure",
                breaker=self.name,
                failures=self._failures,
                threshold=self.threshold,
                error=str(exc),
            )
            if self._failures >= self.threshold or self._state == BreakerState.HALF:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                log.error("circuit opened", breaker=self.name)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state,
            "failures": self._failures,
            "threshold": self.threshold,
            "metrics": {
                "total_calls": self.metrics.total_calls,
                "short_circuits": self.metrics.short_circuits,
                "half_open_trials": self.metrics.half_open_trials,
            },
        }


# ── Servis bazlı breaker singleton'ları ──────────────────────────────────────

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, threshold: int = 5, timeout: float = 30.0) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, threshold=threshold, timeout=timeout)
    return _breakers[name]


def all_breaker_statuses() -> list[dict[str, Any]]:
    return [b.status() for b in _breakers.values()]
