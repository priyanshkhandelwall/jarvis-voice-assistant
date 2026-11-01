"""Tests for the real CPU/RAM/Disk/Network gauge data (R15, F6)."""

from __future__ import annotations

from jarvis.dashboard.gauges import (
    NetworkRateSampler,
    SystemGauges,
    cpu_status_word,
    disk_status_word,
    is_memory_low,
    ram_status_word,
    read_gauges,
)


def test_read_gauges_returns_real_data() -> None:
    """Read gauges returns real data."""
    gauges = read_gauges()
    assert gauges.available is True
    assert 0.0 <= gauges.cpu_percent <= 100.0
    assert 0.0 <= gauges.memory_percent <= 100.0
    assert gauges.memory_available_mb > 0
    assert 0.0 <= gauges.disk_percent <= 100.0


def test_cpu_status_word_nominal_vs_elevated() -> None:
    """Cpu status word nominal VS elevated."""
    nominal = SystemGauges(cpu_percent=10.0, memory_percent=0.0, memory_available_mb=0.0, available=True)
    elevated = SystemGauges(cpu_percent=99.0, memory_percent=0.0, memory_available_mb=0.0, available=True)
    assert cpu_status_word(nominal) == "nominal"
    assert cpu_status_word(elevated) == "elevated"


def test_ram_status_word_nominal_vs_elevated() -> None:
    """Ram status word nominal VS elevated."""
    nominal = SystemGauges(cpu_percent=0.0, memory_percent=10.0, memory_available_mb=0.0, available=True)
    elevated = SystemGauges(cpu_percent=0.0, memory_percent=99.0, memory_available_mb=0.0, available=True)
    assert ram_status_word(nominal) == "nominal"
    assert ram_status_word(elevated) == "elevated"


def test_disk_status_word_nominal_vs_elevated() -> None:
    """Disk status word nominal VS elevated."""
    nominal = SystemGauges(cpu_percent=0.0, memory_percent=0.0, memory_available_mb=0.0, available=True, disk_percent=10.0)
    elevated = SystemGauges(cpu_percent=0.0, memory_percent=0.0, memory_available_mb=0.0, available=True, disk_percent=99.0)
    assert disk_status_word(nominal) == "nominal"
    assert disk_status_word(elevated) == "elevated"


def test_network_rate_sampler_first_sample_is_zero_with_no_prior_baseline() -> None:
    """Network rate sampler first sample is zero with no prior baseline."""
    sampler = NetworkRateSampler()
    rates = sampler.sample()
    assert rates.available is True
    assert rates.upload_kbps == 0.0
    assert rates.download_kbps == 0.0


def test_network_rate_sampler_second_sample_reports_a_real_nonnegative_rate() -> None:
    """Network rate sampler second sample reports a real nonnegative rate."""
    sampler = NetworkRateSampler()
    sampler.sample()  # establishes the baseline
    rates = sampler.sample()
    assert rates.available is True
    assert rates.upload_kbps >= 0.0
    assert rates.download_kbps >= 0.0


def test_is_memory_low_true_when_free_percent_below_threshold() -> None:
    """Is memory low true when free percent below threshold."""
    low = SystemGauges(cpu_percent=10.0, memory_percent=95.0, memory_available_mb=100.0, available=True)
    assert is_memory_low(low) is True


def test_is_memory_low_false_when_plenty_free() -> None:
    """Is memory low false when plenty free."""
    ok = SystemGauges(cpu_percent=10.0, memory_percent=40.0, memory_available_mb=8000.0, available=True)
    assert is_memory_low(ok) is False


def test_is_memory_low_false_when_unavailable() -> None:
    """Is memory low false when unavailable."""
    unavailable = SystemGauges(cpu_percent=0.0, memory_percent=0.0, memory_available_mb=0.0, available=False)
    assert is_memory_low(unavailable) is False


def test_no_setup_message_when_psutil_is_installed() -> None:
    """No setup message when psutil is installed."""
    from jarvis.dashboard.gauges import gauges_setup_message

    assert gauges_setup_message() is None


def test_missing_psutil_gives_the_missing_setup_message(monkeypatch) -> None:
    """R14 missing-setup rule: never "N/A"/"unavailable" on its own."""
    from jarvis.dashboard import gauges

    monkeypatch.setattr(gauges, "PSUTIL_AVAILABLE", False)
    assert gauges.gauges_setup_message() == (
        "psutil isn't installed. Run pip install -r requirements.txt in the JARVIS folder, then restart JARVIS."
    )
    assert gauges.read_gauges().available is False
    assert gauges.NetworkRateSampler().sample().available is False


def test_dashboard_has_no_bare_na_or_unavailable_gauge_text() -> None:
    """Dashboard has no bare na or unavailable gauge text."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath("jarvis", "dashboard", "app.py").read_text(encoding="utf-8")
    assert '"N/A"' not in source
    assert '"unavailable"' not in source
