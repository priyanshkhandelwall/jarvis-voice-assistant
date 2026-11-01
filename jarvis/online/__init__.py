"""R1/R3: the shared online helper and the LOCAL/ONLINE/LIMITED indicator."""

from jarvis.online.service import OnlineError, OnlineErrorKind, OnlineService
from jarvis.online.status import ConnectionMode, OnlineSnapshot, OnlineStatus

__all__ = [
    "OnlineService",
    "OnlineError",
    "OnlineErrorKind",
    "OnlineStatus",
    "OnlineSnapshot",
    "ConnectionMode",
]
