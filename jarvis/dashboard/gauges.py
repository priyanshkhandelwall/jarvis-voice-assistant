"""Real system gauges (R15: show a live memory gauge; F6: CPU/RAM/Disk/
Network gauges). Pure data functions, kept separate from any Flet code so
they're testable without a GUI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.settings import SETTINGS

try:
    import psutil

    PSUTIL_AVAILABLE = True
except ImportError:  # R14: degrade, don't crash
    PSUTIL_AVAILABLE = False

_LOW_MEMORY_FREE_PERCENT = float(SETTINGS["dashboard"]["low_memory_warning_free_percent"])
_CPU_ELEVATED_PERCENT = float(SETTINGS["dashboard"]["cpu_elevated_percent"])
_RAM_ELEVATED_PERCENT = float(SETTINGS["dashboard"]["ram_elevated_percent"])
_DISK_ELEVATED_PERCENT = float(SETTINGS["dashboard"]["disk_elevated_percent"])

# Disk usage for the drive this project lives on -- not a hardcoded "C:\\",
# and not the launch directory (Path.cwd()), which is whatever folder the
# app happened to be started from, not where JARVIS is.
_DISK_PATH = Path(__file__).resolve().anchor

if PSUTIL_AVAILABLE:
    # psutil.cpu_percent(interval=None) reports the change since the
    # PREVIOUS call, and returns a meaningless 0.0 on the very first one.
    # Priming it here means the dashboard's first real read is a real value.
    psutil.cpu_percent(interval=None)


@dataclass(frozen=True)
class SystemGauges:
    """One reading of CPU, memory and disk use for the F6 gauges."""
    cpu_percent: float
    memory_percent: float
    memory_available_mb: float
    available: bool
    disk_percent: float = 0.0


def read_gauges() -> SystemGauges:
    """Read CPU, memory and disk use now (psutil). `available` is False when psutil isn't installed (R14)."""
    if not PSUTIL_AVAILABLE:
        return SystemGauges(cpu_percent=0.0, memory_percent=0.0, memory_available_mb=0.0, available=False)
    cpu = psutil.cpu_percent(interval=None)  # non-blocking; first call after import is 0.0
    mem = psutil.virtual_memory()
    try:
        disk_percent = psutil.disk_usage(_DISK_PATH).percent
    except OSError:  # R14: degrade, don't crash, if the drive is unreadable
        disk_percent = 0.0
    return SystemGauges(
        cpu_percent=cpu,
        memory_percent=mem.percent,
        memory_available_mb=mem.available / (1024 * 1024),
        available=True,
        disk_percent=disk_percent,
    )


def gauges_setup_message() -> str | None:
    """Missing-setup rule (R14): what to do when psutil is missing, so the
    gauges never just say "N/A". None when the gauges work."""
    if PSUTIL_AVAILABLE:
        return None
    return str(SETTINGS["setup_messages"]["psutil_missing"])


def is_memory_low(gauges: SystemGauges) -> bool:
    """R15: warn when free memory is low."""
    if not gauges.available:
        return False
    free_percent = 100.0 - gauges.memory_percent
    return free_percent < _LOW_MEMORY_FREE_PERCENT


class LowMemoryWarning:
    """R15: the RAM gauge turns red on the first low sample (the caller does
    that), but the warning itself -- shown and spoken -- waits until free
    memory has stayed low for `consecutive` slow ticks in a row, and is said
    at most once per `cooldown_seconds`. One brief dip never warns."""

    def __init__(
        self,
        consecutive: int | None = None,
        cooldown_seconds: float | None = None,
        clock=time.monotonic,
    ) -> None:
        cfg = SETTINGS["dashboard"]
        self._needed = int(cfg["low_memory_consecutive_samples"] if consecutive is None else consecutive)
        self._cooldown = float(cfg["low_memory_warning_cooldown_seconds"] if cooldown_seconds is None else cooldown_seconds)
        self._clock = clock
        self._run = 0
        self._last_warned: float | None = None

    def sample(self, low: bool) -> bool:
        """One slow tick. True means: warn now."""
        if not low:
            self._run = 0
            return False
        self._run += 1
        if self._run < self._needed:
            return False
        now = self._clock()
        if self._last_warned is not None and now - self._last_warned < self._cooldown:
            return False
        self._last_warned = now
        return True


def low_memory_warning_text(gauges: SystemGauges) -> str:
    """The R15 low-memory warning, with the free percentage filled in."""
    free = max(0.0, 100.0 - gauges.memory_percent)
    return str(SETTINGS["dashboard"]["low_memory_warning_text"]).format(free=round(free))


def cpu_status_word(gauges: SystemGauges) -> str:
    """'elevated' or 'nominal' for the CPU gauge."""
    return "elevated" if gauges.cpu_percent >= _CPU_ELEVATED_PERCENT else "nominal"


def ram_status_word(gauges: SystemGauges) -> str:
    """'elevated' or 'nominal' for the memory gauge."""
    return "elevated" if gauges.memory_percent >= _RAM_ELEVATED_PERCENT else "nominal"


def disk_status_word(gauges: SystemGauges) -> str:
    """'elevated' or 'nominal' for the disk gauge."""
    return "elevated" if gauges.disk_percent >= _DISK_ELEVATED_PERCENT else "nominal"


@dataclass(frozen=True)
class NetworkRates:
    """Upload and download speed in kB/s since the last sample."""
    upload_kbps: float
    download_kbps: float
    available: bool


class NetworkRateSampler:
    """psutil's network counters are cumulative since boot -- this keeps
    the previous sample so each call can report a real, current rate
    (delta / elapsed time), not a running total. One instance per
    dashboard session; not thread-safe, matches the single dashboard
    refresh thread that owns it."""

    def __init__(self) -> None:
        self._last_time: float | None = None
        self._last_sent = 0
        self._last_recv = 0

    def sample(self) -> NetworkRates:
        """The network rates since the previous call (zero on the first)."""
        if not PSUTIL_AVAILABLE:
            return NetworkRates(0.0, 0.0, False)
        now = time.monotonic()
        counters = psutil.net_io_counters()
        if self._last_time is None:
            self._last_time, self._last_sent, self._last_recv = now, counters.bytes_sent, counters.bytes_recv
            return NetworkRates(0.0, 0.0, True)  # no prior sample yet to diff against
        elapsed = max(now - self._last_time, 1e-6)
        up_kbps = (counters.bytes_sent - self._last_sent) / 1024.0 / elapsed
        down_kbps = (counters.bytes_recv - self._last_recv) / 1024.0 / elapsed
        self._last_time, self._last_sent, self._last_recv = now, counters.bytes_sent, counters.bytes_recv
        return NetworkRates(max(up_kbps, 0.0), max(down_kbps, 0.0), True)
