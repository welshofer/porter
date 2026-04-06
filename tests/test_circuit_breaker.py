# tests/test_circuit_breaker.py
import time
from lib.circuit_breaker import CircuitBreaker, CircuitBreakerRegistry


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        assert cb.is_closed()
        assert cb.allow_request()

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_closed()
        cb.record_failure()
        assert cb.is_open()
        assert not cb.allow_request()

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb.consecutive_failures == 0
        assert cb.is_closed()

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.15)
        assert cb.allow_request()  # Half-open: allow one probe

    def test_failure_in_half_open_reopens(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        assert cb.allow_request()  # Half-open
        cb.record_failure()
        assert cb.is_open()
        assert not cb.allow_request()

    def test_success_in_half_open_closes(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.15)
        assert cb.allow_request()  # Half-open
        cb.record_success()
        assert cb.is_closed()
        assert cb.consecutive_failures == 0


class TestCircuitBreakerRegistry:
    def test_subsystems_are_independent(self):
        registry = CircuitBreakerRegistry(failure_threshold=2, recovery_timeout_seconds=60)
        # Trip "reminders" breaker
        registry.record_failure("reminders")
        registry.record_failure("reminders")

        # "claude" should still be allowed
        assert registry.allow_request("claude") is True
        assert registry.allow_request("reminders") is False

    def test_any_open_reports_tripped_subsystems(self):
        registry = CircuitBreakerRegistry(failure_threshold=2, recovery_timeout_seconds=60)
        registry.record_failure("reminders")
        registry.record_failure("reminders")

        open_list = registry.any_open()
        assert "reminders" in open_list
        assert "claude" not in open_list

    def test_success_resets_subsystem(self):
        registry = CircuitBreakerRegistry(failure_threshold=2, recovery_timeout_seconds=60)
        registry.record_failure("claude")
        registry.record_success("claude")
        assert registry.allow_request("claude") is True
