"""Share one hosted Decider request-rate limit across assistant features."""
from __future__ import annotations

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_CALLS: deque[float] = deque()


def reserve_decider_request(limit: int = 10, window_seconds: int = 60) -> None:
    """Wait for a request slot while keeping concurrent callers under the shared limit."""
    with _LOCK:
        now = time.monotonic()
        while _CALLS and now - _CALLS[0] >= window_seconds:
            _CALLS.popleft()
        if len(_CALLS) >= limit:
            time.sleep(max(0.0, window_seconds - (now - _CALLS[0])))
            now = time.monotonic()
            while _CALLS and now - _CALLS[0] >= window_seconds:
                _CALLS.popleft()
        _CALLS.append(now)
