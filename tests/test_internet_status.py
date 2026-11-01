"""F6 Internet panel text and colours (approved): LOCAL cyan,
ONLINE green, LIMITED amber with the time the limit ends."""

from __future__ import annotations

from datetime import datetime

from jarvis.dashboard.internet_status import internet_display
from jarvis.online import ConnectionMode, OnlineSnapshot


def test_local_is_cyan() -> None:
    """Local is cyan."""
    shown = internet_display(OnlineSnapshot(ConnectionMode.LOCAL))
    assert shown.value == "LOCAL" and shown.color == "#00E5FF"


def test_online_is_green_and_never_names_the_provider() -> None:
    """Online is green and never names the provider."""
    # R3 amended No provider name anywhere on screen.
    shown = internet_display(OnlineSnapshot(ConnectionMode.ONLINE, service="Groq"))
    assert shown.value == "ONLINE" and shown.color == "#5DEAB0"
    assert "Groq" not in shown.detail and shown.detail == "online search in use"


def test_limited_is_amber_with_until_time() -> None:
    """Limited is amber with until time."""
    until = datetime(2026, 10, 3, 14, 32).timestamp()
    shown = internet_display(OnlineSnapshot(ConnectionMode.LIMITED, service="Groq", limited_until=until))
    assert shown.value == "LIMITED until 14:32" and shown.color == "#FFB020"
