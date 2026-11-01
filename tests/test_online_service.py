"""R1's shared online helper (jarvis/online): timeout handed to the call,
at most one retry and only for timeouts/connection failures, and the 429
cooldown that keeps later calls off the network. Plus the LOCAL / ONLINE /
LIMITED indicator it drives."""

from __future__ import annotations

import pytest

from jarvis.online import ConnectionMode, OnlineError, OnlineErrorKind, OnlineService, OnlineStatus


class FakeClock:
    """A clock the test moves by hand."""
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


def _make(clock: FakeClock, status: OnlineStatus | None = None, max_retries: int = 1) -> OnlineService:
    return OnlineService(
        "Svc",
        status or OnlineStatus(wall_clock=clock),
        timeout_seconds=10,
        max_retries=max_retries,
        cooldown_seconds=300,
        clock=clock,
        wall_clock=clock,
    )


def _raiser(kind: OnlineErrorKind, retry_after: float | None = None):
    def fn(_timeout):
        raise OnlineError(kind, retry_after_seconds=retry_after)

    return fn


def _identity(exc: Exception) -> OnlineError:
    return exc if isinstance(exc, OnlineError) else OnlineError(OnlineErrorKind.SERVICE_ERROR, str(exc))


def test_call_receives_the_settings_timeout() -> None:
    """Call receives the settings timeout."""
    seen = []
    assert _make(FakeClock()).call(lambda t: seen.append(t) or "ok", _identity) == "ok"
    assert seen == [10]


@pytest.mark.parametrize("kind", [OnlineErrorKind.TIMEOUT, OnlineErrorKind.CANNOT_REACH])
def test_timeout_and_connection_failures_retry_exactly_once(kind) -> None:
    """Timeout and connection failures retry exactly once."""
    attempts = []

    def fn(_t):
        attempts.append(1)
        raise OnlineError(kind)

    with pytest.raises(OnlineError) as info:
        _make(FakeClock()).call(fn, _identity)
    assert info.value.kind == kind
    assert len(attempts) == 2


def test_retry_succeeds_on_second_attempt() -> None:
    """Retry succeeds on second attempt."""
    attempts = []

    def fn(_t):
        attempts.append(1)
        if len(attempts) == 1:
            raise OnlineError(OnlineErrorKind.TIMEOUT)
        return "second time lucky"

    assert _make(FakeClock()).call(fn, _identity) == "second time lucky"


@pytest.mark.parametrize("kind", [OnlineErrorKind.KEY_REJECTED, OnlineErrorKind.SERVICE_ERROR, OnlineErrorKind.RATE_LIMITED])
def test_other_failures_never_retry(kind) -> None:
    """Other failures never retry."""
    attempts = []

    def fn(_t):
        attempts.append(1)
        raise OnlineError(kind)

    with pytest.raises(OnlineError):
        _make(FakeClock()).call(fn, _identity)
    assert len(attempts) == 1


def test_429_starts_default_cooldown_and_blocks_network() -> None:
    """429 starts default cooldown and blocks network."""
    clock = FakeClock()
    status = OnlineStatus(wall_clock=clock)
    service = _make(clock, status)
    with pytest.raises(OnlineError) as first:
        service.call(_raiser(OnlineErrorKind.RATE_LIMITED), _identity)
    assert first.value.until_wall == clock.now + 300
    calls = []
    with pytest.raises(OnlineError) as second:
        service.call(lambda t: calls.append(t), _identity)
    assert second.value.kind == OnlineErrorKind.COOLING
    assert calls == []  # the network was never touched
    snap = status.snapshot()
    assert snap.mode == ConnectionMode.LIMITED and snap.service == "Svc" and snap.limited_until == clock.now + 300


def test_429_retry_after_overrides_default() -> None:
    """429 retry after overrides default."""
    clock = FakeClock()
    service = _make(clock)
    with pytest.raises(OnlineError) as info:
        service.call(_raiser(OnlineErrorKind.RATE_LIMITED, retry_after=120), _identity)
    assert info.value.until_wall == clock.now + 120
    clock.now += 121
    assert service.call(lambda t: "back", _identity) == "back"


def test_cooldown_ends_and_status_returns_to_local() -> None:
    """Cooldown ends and status returns to local."""
    clock = FakeClock()
    status = OnlineStatus(wall_clock=clock)
    service = _make(clock, status)
    with pytest.raises(OnlineError):
        service.call(_raiser(OnlineErrorKind.RATE_LIMITED), _identity)
    clock.now += 301
    assert service.cooling_until_wall() is None
    assert status.snapshot().mode == ConnectionMode.LOCAL


def test_status_is_online_only_while_the_call_runs() -> None:
    """Status is online only while the call runs."""
    clock = FakeClock()
    status = OnlineStatus(wall_clock=clock)
    service = _make(clock, status)
    during = []
    service.call(lambda t: during.append(status.snapshot()), _identity)
    assert during[0].mode == ConnectionMode.ONLINE and during[0].service == "Svc"
    assert status.snapshot().mode == ConnectionMode.LOCAL


def test_online_wins_over_limited() -> None:
    """Online wins over limited."""
    clock = FakeClock()
    status = OnlineStatus(wall_clock=clock)
    status.set_limited("A", clock.now + 100)
    with status.online("B"):
        assert status.snapshot().mode == ConnectionMode.ONLINE
    assert status.snapshot().mode == ConnectionMode.LIMITED


def test_unexpected_exception_is_classified_not_raised_raw() -> None:
    """Unexpected exception is classified not raised raw."""
    def boom(_t):
        raise ValueError("weird")

    with pytest.raises(OnlineError) as info:
        _make(FakeClock()).call(boom, _identity)
    assert info.value.kind == OnlineErrorKind.SERVICE_ERROR
