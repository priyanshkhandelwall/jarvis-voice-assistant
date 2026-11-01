"""Shared rule 9: plain HTTP JSON calls (F16a's Open-Meteo now; F20/F21
later) with the standard library's urllib only -- no new dependency.

These functions make one request. R1's timeout, single retry and 429
cooldown are applied by OnlineService around them, the same way they are
for Groq: a feature calls `service.call(lambda t: get_json(url, t),
classify_http_error)`.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

from jarvis.online.service import OnlineError, OnlineErrorKind

# Identifies the program to the service, nothing about the developer (R5).
_USER_AGENT = "JARVIS-student-project (urllib)"


def build_url(base: str, params: Mapping[str, Any]) -> str:
    """`base?name=value&...`, with every value URL-encoded. Lists become
    comma-separated, which is how Open-Meteo takes its variable lists."""
    flat = {key: ",".join(str(v) for v in value) if isinstance(value, (list, tuple)) else value for key, value in params.items()}
    return f"{base}?{urllib.parse.urlencode(flat)}"


def get_json(url: str, timeout: float) -> Any:
    """GET `url` and parse the body as JSON. Raises the urllib/socket
    exception as it is (classify_http_error names it), or OnlineError
    SERVICE_ERROR when the body is not JSON."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OnlineError(OnlineErrorKind.SERVICE_ERROR, "the response was not JSON") from exc


def get_bytes(url: str, timeout: float) -> bytes:
    """GET `url` and return the raw body (F20/F21's RSS feeds, parsed with
    defusedxml by the caller). Exceptions are raised as they are, for
    classify_http_error."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _retry_after(headers) -> Optional[float]:
    try:
        return float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None


def classify_http_error(exc: Exception) -> OnlineError:
    """urllib's exceptions as OnlineErrorKinds (shared rule 4: each failure
    named separately). HTTPError is checked first: it subclasses URLError."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return OnlineError(OnlineErrorKind.RATE_LIMITED, "HTTP 429", retry_after_seconds=_retry_after(exc.headers))
        return OnlineError(OnlineErrorKind.SERVICE_ERROR, f"HTTP {exc.code}")
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return OnlineError(OnlineErrorKind.TIMEOUT, str(exc))
    if isinstance(exc, urllib.error.URLError):
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            return OnlineError(OnlineErrorKind.TIMEOUT, str(exc.reason))
        return OnlineError(OnlineErrorKind.CANNOT_REACH, str(exc.reason))
    if isinstance(exc, OSError):  # connection reset, DNS failure outside URLError
        return OnlineError(OnlineErrorKind.CANNOT_REACH, str(exc))
    return OnlineError(OnlineErrorKind.SERVICE_ERROR, f"{type(exc).__name__}: {exc}")
