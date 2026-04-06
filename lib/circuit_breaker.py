# lib/circuit_breaker.py
"""Circuit breaker for Claude API calls — prevents cascading failures."""

import time
import logging
import threading

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Simple circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout_seconds: float = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.consecutive_failures = 0
        self._opened_at: float = 0
        self._state = "closed"  # closed, open, half_open
        self._lock = threading.Lock()

    def is_closed(self) -> bool:
        return self._state == "closed"

    def is_open(self) -> bool:
        return self._state == "open"

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.recovery_timeout_seconds:
                    self._state = "half_open"
                    self.consecutive_failures = 0
                    logger.info("Circuit breaker half-open — allowing probe request")
                    return True
                return False
            # half_open — allow one request
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == "half_open":
                logger.info("Circuit breaker closing — probe succeeded")
            self.consecutive_failures = 0
            self._state = "closed"

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker OPEN after %d consecutive failures (recovery in %ds)",
                    self.consecutive_failures, self.recovery_timeout_seconds,
                )
            elif self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning("Circuit breaker re-opened — probe request failed")


class CircuitBreakerRegistry:
    """Manages per-subsystem circuit breakers."""

    def __init__(self, failure_threshold: int = 3, recovery_timeout_seconds: float = 300):
        self._default_threshold = failure_threshold
        self._default_recovery = recovery_timeout_seconds
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def _get(self, subsystem: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a subsystem."""
        with self._lock:
            if subsystem not in self._breakers:
                self._breakers[subsystem] = CircuitBreaker(
                    self._default_threshold, self._default_recovery
                )
            return self._breakers[subsystem]

    def allow_request(self, subsystem: str) -> bool:
        return self._get(subsystem).allow_request()

    def record_success(self, subsystem: str) -> None:
        self._get(subsystem).record_success()

    def record_failure(self, subsystem: str) -> None:
        self._get(subsystem).record_failure()

    def any_open(self) -> list[str]:
        """Return list of subsystem names with open circuit breakers."""
        with self._lock:
            return [name for name, cb in self._breakers.items() if cb.is_open()]
