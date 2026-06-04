"""Per-site health monitor + circuit breaker — the "variable health monitor".

State machine:  HEALTHY  ──N consecutive transient failures──▶  OPEN
                  ▲                                               │
                  └────K consecutive good probes────  PROBING ◀──┘

While OPEN the engine stops hammering the site; the runner calls
`wait_for_recovery()`, which slow-probes the base URL on an escalating cadence
(1m → 5m → 15m → 60m, capped) until the site answers reliably again, then resumes
at normal pace. This is what lets you start the run, lose connectivity for a day,
and still come back to a finished archive.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

HEALTHY = "HEALTHY"
OPEN = "OPEN"
PROBING = "PROBING"

# escalating wait between probes while suspended (seconds)
PROBE_SCHEDULE = (60, 300, 900, 3600)


class HealthMonitor:
    def __init__(self, name: str, *, fail_threshold: int = 10,
                 probe_success_needed: int = 2,
                 probe_schedule: tuple[int, ...] = PROBE_SCHEDULE,
                 log: Callable[[str], None] | None = None) -> None:
        self.name = name
        self.fail_threshold = fail_threshold
        self.probe_success_needed = probe_success_needed
        self.probe_schedule = probe_schedule
        self._log = log or (lambda m: None)

        self.state = HEALTHY
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_error = ""
        self.next_probe_at = 0.0
        self._lock = threading.Lock()

    # --- outcome recording (called from the crawl loop) --------------------
    def record_success(self) -> None:
        with self._lock:
            self.total_successes += 1
            self.consecutive_failures = 0

    def record_permanent(self) -> None:
        # A dead URL says nothing about connectivity; don't move the breaker.
        with self._lock:
            self.consecutive_failures = 0

    def record_transient_failure(self, error: str = "") -> bool:
        """Returns True if this failure tripped the breaker OPEN."""
        with self._lock:
            self.total_failures += 1
            self.consecutive_failures += 1
            self.last_error = error
            if self.state == HEALTHY and self.consecutive_failures >= self.fail_threshold:
                self.state = OPEN
                self._log(f"[{self.name}] circuit OPEN after "
                          f"{self.consecutive_failures} consecutive failures: {error}")
                return True
            return False

    def is_open(self) -> bool:
        return self.state != HEALTHY

    # --- recovery (called from the runner when a pass reports CIRCUIT_OPEN) -
    def wait_for_recovery(self, probe_fn: Callable[[], bool],
                          stop_event: threading.Event) -> bool:
        """Block (cooperatively) until the site recovers or we're asked to stop.

        `probe_fn()` should attempt a single cheap fetch of the base URL and return
        True on success. Returns True if recovered, False if stopped."""
        with self._lock:
            self.state = PROBING
        good = 0
        step = 0
        while not stop_event.is_set():
            wait = self.probe_schedule[min(step, len(self.probe_schedule) - 1)]
            self.next_probe_at = time.time() + wait
            self._log(f"[{self.name}] suspended; next connectivity probe in {wait}s")
            if stop_event.wait(wait):
                return False
            try:
                ok = probe_fn()
            except Exception as exc:  # noqa: BLE001
                ok = False
                self.last_error = str(exc)
            if ok:
                good += 1
                self._log(f"[{self.name}] probe OK ({good}/{self.probe_success_needed})")
                if good >= self.probe_success_needed:
                    with self._lock:
                        self.state = HEALTHY
                        self.consecutive_failures = 0
                        self.next_probe_at = 0.0
                    self._log(f"[{self.name}] connectivity restored — resuming")
                    return True
            else:
                good = 0
                step += 1  # back off further
        return False

    def status(self) -> dict:
        eta = max(0, int(self.next_probe_at - time.time())) if self.next_probe_at else 0
        return {
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "next_probe_in_s": eta,
            "last_error": self.last_error[:120],
        }
