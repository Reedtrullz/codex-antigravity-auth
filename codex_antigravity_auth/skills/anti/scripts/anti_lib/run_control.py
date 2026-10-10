"""Monotonic run time and process-local permits for actual HTTP destinations."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
import time

CURRENT_RUN = ContextVar('anti_current_run', default=None)
_CONDITION = threading.Condition()
_ACTIVE = {}


class DeadlineExceeded(Exception):
    submitted = False


class RunControl:
    def __init__(self, seconds, *, caps, default_cap=2, clock=None, sleeper=None, error_type=DeadlineExceeded):
        if isinstance(seconds, bool) or not math.isfinite(float(seconds)) or not 0 < float(seconds) <= 86400:
            raise ValueError('run timeout must be finite and greater than zero, at most 86400 seconds')
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.limit = float(seconds)
        self.started = self.clock()
        self.deadline = self.started + self.limit
        self.caps = dict(caps)
        self.default_cap = max(1, int(default_cap))
        self.error_type = error_type
        self.lock = threading.Lock()
        self.started_calls = self.finished_calls = self.deferred_calls = self.submitted_calls = 0
        self.events = []

    @contextmanager
    def bind(self):
        token = CURRENT_RUN.set(self)
        try:
            yield self
        finally:
            CURRENT_RUN.reset(token)

    def remaining(self):
        return max(0.0, self.deadline - self.clock())

    def expired(self):
        return self.remaining() <= 0

    def note_deferred(self, reason):
        with self.lock:
            self.deferred_calls += 1
            if len(self.events) < 128:
                self.events.append({'status': 'deferred', 'reason': reason})

    def stop(self, reason='run_deadline_exceeded', *, submitted=False):
        self.note_deferred(reason)
        error = self.error_type(reason + ': no further provider attempt was started')
        error.submitted = submitted
        error.reason = reason
        raise error

    def timeout(self, requested):
        remaining = self.remaining()
        if remaining <= 0:
            self.stop()
        if not math.isfinite(float(requested)) or float(requested) <= 0:
            raise ValueError('HTTP timeout must be finite and greater than zero')
        return min(float(requested), remaining)

    def check(self, *, submitted=False):
        if self.expired():
            self.stop(submitted=submitted)

    def sleep(self, seconds):
        if seconds >= self.remaining():
            self.stop('retry_deferred_by_run_deadline')
        self.check()
        self.sleeper(seconds)
        self.check()

    @contextmanager
    def attempt(self, gateway, destination):
        """Permit is held only for one attempt; retries/fallback reacquire."""
        key = (gateway, destination)
        cap = max(1, int(self.caps.get(destination, self.default_cap)))
        with _CONDITION:
            self.check()
            while _ACTIVE.get(key, 0) >= cap:
                _CONDITION.wait(self.remaining())
                self.check()
            self.check()
            _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
        with self.lock:
            self.started_calls += 1
        try:
            yield
        finally:
            with self.lock:
                self.finished_calls += 1
            with _CONDITION:
                active = _ACTIVE[key] - 1
                if active:
                    _ACTIVE[key] = active
                else:
                    del _ACTIVE[key]
                _CONDITION.notify_all()

    def mark_submitted(self):
        with self.lock:
            self.submitted_calls += 1

    def snapshot(self):
        with self.lock:
            return {'scope': 'process_local', 'limit_seconds': self.limit,
                    'elapsed_seconds': max(0.0, self.clock() - self.started),
                    'remaining_seconds': self.remaining(), 'deadline_exceeded': self.expired(),
                    'attempts_started': self.submitted_calls, 'permits_acquired': self.started_calls,
                    'permits_released': self.finished_calls,
                    'deferred_calls': self.deferred_calls, 'events': list(self.events),
                    'events_omitted': max(0, self.deferred_calls - len(self.events))}
