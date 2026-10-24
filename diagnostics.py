import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_time(value):
    if type(value) in (int, float) and 0 < value < float('inf'):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError):
            pass
    return None


class Diagnostics:
    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._since = time.time()
        self._counts = dict.fromkeys((
            'api_requests', 'api_errors', 'api_rate_limits', 'cdn_requests', 'cdn_errors',
            'auth_successes', 'auth_failures', 'guide_successes', 'guide_failures',
            'sessions_restored', 'sessions_created', 'session_cache_hits',
            'manifest_cache_hits', 'sessions_invalidated', 'cache_errors',
        ), 0)
        self._last = {}
        self._running = set()

    def record(self, event, amount=1, *, status=None, error_type=None):
        with self._lock:
            self._counts[event] += amount
            self._last[event] = {'at': utc_time(time.time()), 'http_status': status, 'error_type': error_type}

    @contextmanager
    def operation(self, name):
        with self._lock:
            self._running.add(name)
        try:
            yield
        except Exception as error:
            self.record(name + '_failures', error_type=type(error).__name__)
            raise
        else:
            self.record(name + '_successes')
        finally:
            with self._lock:
                self._running.discard(name)

    def snapshot(self):
        with self._lock:
            return {
                'since': utc_time(self._since), 'uptime_seconds': max(0, int(time.monotonic() - self._started)),
                'counters': dict(self._counts), 'last': {key: dict(value) for key, value in self._last.items()},
                'running': sorted(self._running),
            }
