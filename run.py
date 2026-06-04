#!/usr/bin/env python3
"""minis-content-rescue — root entry point.

Launches and monitors a crawl of every site until each one is **100% scraped and
validated**, then exits. Both sites run in parallel (one worker thread each), with a
shared dashboard. On a sustained outage a site's circuit breaker trips and it falls
back to a slow hourly connectivity probe, auto-resuming when the site returns — so
you can start this, walk away, and come back to finished archives.

Usage:
    python run.py                       # crawl + validate BOTH sites, block until done
    python run.py --site solegends      # one site only
    python run.py --once                # a single crawl pass per site (no blocking)
    python run.py --max-pages 25        # smoke test: stop each site after N pages
    python run.py --validate-only       # just report completeness and exit
    python run.py --no-resume           # start fresh (ignores existing state)

Ctrl-C snapshots state and exits cleanly; rerun to resume exactly where it stopped.
"""
from __future__ import annotations

import argparse
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from scrapers.common import engine
from scrapers.common.engine import Crawler
from scrapers.common.health import HealthMonitor
from scrapers.common.report import write_reports
from scrapers.common.state import rebuild_queue_from_mirror
from scrapers.common.validate import validate
from scrapers.sites import SITES

_print_lock = threading.Lock()


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Logger:
    """Thread-safe logger: stdout + per-site crawl.log."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, message: str) -> None:
        line = f"{_ts()} {message}"
        with _print_lock:
            print(line, flush=True)
            if self.log_path is not None:
                try:
                    with self.log_path.open("a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:
                    pass


@dataclass
class SiteStatus:
    name: str
    phase: str = "starting"
    done: int = 0
    queued: int = 0
    dead: int = 0
    assets: int = 0
    errors: int = 0
    validated: bool | None = None
    issues: list[str] = field(default_factory=list)
    health: dict = field(default_factory=dict)
    crawler: "Crawler | None" = None

    def refresh(self, crawler: "Crawler | None" = None) -> None:
        crawler = crawler or self.crawler
        if crawler is None:
            return
        st = crawler.state
        if st is not None:
            self.done = len(st.done)
            self.queued = len(st.queue)
            self.dead = len(st.dead)
            self.assets = len(st.asset_registry)
            self.errors = len(st.errors)
        self.health = crawler.health.status()


def run_site(name: str, args, stop_event: threading.Event, status: SiteStatus) -> bool:
    adapter = SITES[name]()
    out = adapter.default_out_dir()
    log = Logger(out / "crawl.log")
    health = HealthMonitor(name, fail_threshold=args.fail_threshold, log=log)
    crawler = Crawler(adapter, out, delay=args.delay, health=health,
                      stop_event=stop_event, log=log)
    status.crawler = crawler
    single_pass = args.once or args.max_pages is not None
    ok = False
    try:
        status.phase = "loading"
        crawler.load(resume=not args.no_resume)
        status.refresh(crawler)

        if args.validate_only:
            report = validate(adapter, out, state=crawler.state)
            status.validated = report["ok"]
            status.issues = report["issues"]
            status.phase = "valid" if report["ok"] else "incomplete"
            log(f"[{name}] validate ok={report['ok']} {report['stats']}")
            for w in report["warnings"]:
                log(f"[{name}] warning: {w}")
            return report["ok"]

        while not stop_event.is_set():
            status.phase = "crawling"
            result = crawler.crawl_pass(max_pages=args.max_pages)
            status.refresh(crawler)
            log(f"[{name}] pass ended: {result.reason} (+{result.pages} pages)")

            if result.reason == engine.CIRCUIT_OPEN:
                if single_pass:
                    break
                status.phase = "suspended"
                if not crawler.health.wait_for_recovery(crawler.probe, stop_event):
                    break
                continue
            if result.reason == engine.STOPPED:
                break
            if result.reason == engine.MAX_PAGES:
                break
            # COMPLETE: queue drained
            if single_pass:
                break
            status.phase = "validating"
            report = validate(adapter, out, state=crawler.state)
            if not report["ok"] and report["stats"].get("assets_missing_pending"):
                status.phase = "backfilling assets"
                repaired = crawler.backfill_assets()
                log(f"[{name}] backfilled {repaired} missing assets")
                report = validate(adapter, out, state=crawler.state)
            if report["ok"]:
                ok = True
                status.validated = True
                status.phase = "DONE"
                log(f"[{name}] COMPLETE & VALIDATED {report['stats']}")
                break
            # not ok and queue empty -> try to re-derive frontier once
            status.issues = report["issues"]
            if not crawler.state.queue:
                rebuilt = rebuild_queue_from_mirror(adapter, out, crawler.state)
                if not rebuilt:
                    status.phase = "stuck"
                    log(f"[{name}] cannot reach 100%: {report['issues']}")
                    break
                log(f"[{name}] re-derived frontier: +{rebuilt} URLs")

        # finalize reports + final validation
        crawler.state.snapshot()
        write_reports(adapter, crawler.state, out, started=crawler.started,
                      finished=datetime.now(timezone.utc).isoformat())
        final = validate(adapter, out, state=crawler.state)
        status.validated = final["ok"]
        status.issues = final["issues"]
        if not single_pass:
            status.phase = "DONE" if final["ok"] else status.phase
        ok = final["ok"]
        status.refresh(crawler)
        return ok
    except Exception as exc:  # noqa: BLE001
        status.phase = f"error: {exc}"
        log(f"[{name}] worker error: {exc!r}")
        return False
    finally:
        crawler.close()


def monitor(statuses: list[SiteStatus], stop_event: threading.Event,
            done_event: threading.Event, interval: float) -> None:
    while not done_event.is_set():
        if done_event.wait(interval):
            break
        for s in statuses:
            s.refresh()
        _dashboard(statuses)
    for s in statuses:
        s.refresh()
    _dashboard(statuses, final=True)


def _dashboard(statuses: list[SiteStatus], final: bool = False) -> None:
    lines = ["", "=" * 78, f"  minis-content-rescue {'FINAL' if final else _ts()}", "-" * 78]
    for s in statuses:
        h = s.health or {}
        hs = h.get("state", "?")
        extra = ""
        if hs != "HEALTHY":
            extra = f" probe_in={h.get('next_probe_in_s', 0)}s fails={h.get('consecutive_failures', 0)}"
        vflag = {True: "ok", False: "no", None: "-"}[s.validated]
        lines.append(
            f"  {s.name:10s} {s.phase:18s} done={s.done:<7d} queued={s.queued:<7d} "
            f"dead={s.dead:<5d} assets={s.assets:<7d} err={s.errors:<5d} "
            f"health={hs}{extra} valid={vflag}")
        if s.issues and s.phase in ("stuck", "incomplete"):
            lines.append(f"             issues: {'; '.join(s.issues[:3])}")
    lines.append("=" * 78)
    with _print_lock:
        print("\n".join(lines), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Crawl + validate site archives until 100%.")
    ap.add_argument("--site", choices=sorted(SITES), action="append",
                    help="Limit to one site (repeatable). Default: all.")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="Stop each site after N pages this run (smoke test).")
    ap.add_argument("--delay", type=float, default=None,
                    help="Seconds between requests (default: per-site).")
    ap.add_argument("--once", action="store_true",
                    help="Single crawl pass per site, then validate and exit.")
    ap.add_argument("--validate-only", action="store_true",
                    help="Report completeness and exit; no crawling.")
    ap.add_argument("--no-resume", action="store_true",
                    help="Ignore existing state and start fresh.")
    ap.add_argument("--fail-threshold", type=int, default=10,
                    help="Consecutive failures before the circuit breaker trips.")
    ap.add_argument("--dashboard-interval", type=float, default=20.0)
    args = ap.parse_args()

    names = args.site or list(SITES)
    statuses = [SiteStatus(n) for n in names]
    stop_event = threading.Event()
    done_event = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        with _print_lock:
            print("\n[run] stop requested — snapshotting and shutting down...", flush=True)
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass

    results: dict[str, bool] = {}
    workers: list[threading.Thread] = []
    for name, status in zip(names, statuses):
        t = threading.Thread(
            target=lambda n=name, s=status: results.__setitem__(n, run_site(n, args, stop_event, s)),
            name=f"crawl-{name}", daemon=True)
        workers.append(t)

    mon = threading.Thread(target=monitor,
                           args=(statuses, stop_event, done_event, args.dashboard_interval),
                           name="monitor", daemon=True)
    mon.start()
    for t in workers:
        t.start()
    for t in workers:
        # join in a loop so the main thread keeps handling signals
        while t.is_alive():
            t.join(timeout=1.0)

    done_event.set()
    mon.join(timeout=5.0)

    all_ok = bool(results) and all(results.get(n) for n in names) and not stop_event.is_set()
    with _print_lock:
        print("\n[run] summary:")
        for n in names:
            print(f"  {n}: {'COMPLETE & VALIDATED' if results.get(n) else 'INCOMPLETE'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
