"""Generic crawl engine, driven by a SiteAdapter.

Generalises the two near-identical crawl loops from the old scripts, with the
correctness fixes that the "100% & validated" guarantee depends on:

  * A page is added to `done` ONLY after it is successfully fetched and mirrored.
    Transient failures re-queue (bounded), permanent failures (404/300...) go to a
    dead-letter set so the queue can still drain to empty.
  * Every fetch flows through the health monitor; a sustained outage trips the
    circuit breaker and the pass returns CIRCUIT_OPEN instead of silently burning
    through the queue marking everything failed.
  * State is snapshotted atomically on a timer / page interval / shutdown, never as
    a multi-hundred-MB rewrite per page.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import retry, yamlio
from .assets import mirror_asset_record, mirror_stylesheet
from .health import HealthMonitor
from .htmlparse import (H1_RE, META_DESC_RE, META_DESC_RE2, TITLE_RE, classify_link,
                        collect_html_assets, extract_first, page_links,
                        parse_stylesheet_hrefs)
from .site import SiteAdapter
from .state import CrawlState, load_state

COMPLETE = "COMPLETE"
MAX_PAGES = "MAX_PAGES"
CIRCUIT_OPEN = "CIRCUIT_OPEN"
STOPPED = "STOPPED"

SNAPSHOT_EVERY_SECONDS = 30.0
SNAPSHOT_EVERY_PAGES = 200
PROGRESS_EVERY_PAGES = 25


class PermanentFetchError(Exception):
    pass


class TransientFetchError(Exception):
    pass


class CircuitOpenError(Exception):
    pass


class StoppedError(Exception):
    pass


@dataclass
class PassResult:
    reason: str
    pages: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Crawler:
    def __init__(self, adapter: SiteAdapter, out_dir: Path, *, delay: float | None = None,
                 max_retries: int = 4, page_retry_budget: int = 10,
                 health: HealthMonitor | None = None,
                 stop_event: threading.Event | None = None, log=None) -> None:
        self.adapter = adapter
        self.out_dir = out_dir
        self.delay = adapter.default_delay if delay is None else delay
        self.max_retries = max_retries          # per fetch() call
        self.page_retry_budget = page_retry_budget  # per URL across passes
        self.stop_event = stop_event or threading.Event()
        self.log = log or (lambda m: print(m, flush=True))
        self.health = health or HealthMonitor(adapter.name, log=self.log)
        self.state: CrawlState | None = None
        self.started = _now()
        self._last_snapshot = 0.0

    # --- lifecycle ----------------------------------------------------------
    def load(self, *, resume: bool = True) -> None:
        self.state = load_state(self.adapter, self.out_dir, resume=resume)

    def close(self) -> None:
        if self.state is not None:
            self.state.snapshot()
            self.state.close()
        self.adapter.fetcher.close()

    # --- network (retry + classify + health) --------------------------------
    def fetch(self, url: str, timeout: int = 60) -> bytes:
        attempt = 0
        while True:
            if self.stop_event.is_set():
                raise StoppedError()
            if self.health.is_open():
                raise CircuitOpenError(url)
            attempt += 1
            try:
                data = self.adapter.fetcher.fetch_bytes(url, timeout)
                self.health.record_success()
                return data
            except Exception as exc:  # noqa: BLE001
                kind = retry.classify(exc)
                if kind == retry.PERMANENT:
                    self.health.record_permanent()
                    raise PermanentFetchError(str(exc)) from exc
                tripped = self.health.record_transient_failure(str(exc))
                if tripped:
                    raise CircuitOpenError(str(exc)) from exc
                if attempt > self.max_retries:
                    raise TransientFetchError(str(exc)) from exc
                if self.stop_event.wait(retry.backoff_seconds(attempt)):
                    raise StoppedError()

    def probe(self) -> bool:
        """One cheap connectivity check for circuit-breaker recovery."""
        try:
            self.adapter.fetcher.fetch_bytes(self.adapter.base_url, 45)
            return True
        except Exception:  # noqa: BLE001
            return False

    # --- queue helpers ------------------------------------------------------
    def _enqueue(self, url: str, *, front: bool = False) -> None:
        assert self.state is not None
        norm = self.adapter.normalize_page_url(url)
        self.state.discovered.add(norm)
        if norm in self.state.done or norm in self.state.dead:
            return
        if front:
            self.state.queue.appendleft(norm)
        else:
            self.state.queue.append(norm)

    def _record_error(self, url: str, error: str) -> None:
        assert self.state is not None
        self.state.errors.append({"url": url, "error": error, "at": _now()})

    # --- main pass ----------------------------------------------------------
    def crawl_pass(self, *, max_pages: int | None = None) -> PassResult:
        assert self.state is not None
        st = self.state
        n = 0
        while st.queue:
            if self.stop_event.is_set():
                st.snapshot()
                return PassResult(STOPPED, n)
            if max_pages is not None and n >= max_pages:
                st.snapshot()
                return PassResult(MAX_PAGES, n)
            if self.health.is_open():
                st.snapshot()
                return PassResult(CIRCUIT_OPEN, n)

            raw_url = st.queue.popleft()
            url = self.adapter.normalize_page_url(raw_url)
            if url in st.done or url in st.dead:
                continue
            is_page = self.adapter.is_html_page_url(url)
            try:
                self.process_page(url)
                if is_page:
                    n += 1
            except PermanentFetchError as exc:
                st.dead[url] = str(exc)
                self._record_error(url, str(exc))
            except CircuitOpenError:
                st.queue.appendleft(url)   # not done — retry after recovery
                st.snapshot()
                return PassResult(CIRCUIT_OPEN, n)
            except StoppedError:
                st.queue.appendleft(url)
                st.snapshot()
                return PassResult(STOPPED, n)
            except TransientFetchError as exc:
                tries = st.attempts.get(url, 0) + 1
                st.attempts[url] = tries
                self._record_error(url, str(exc))
                if tries >= self.page_retry_budget:
                    st.dead[url] = f"transient_exhausted: {exc}"
                else:
                    st.queue.append(url)   # try again later this/next pass

            if is_page and n % PROGRESS_EVERY_PAGES == 0:
                self._progress(n)
            self._maybe_snapshot(n)

        st.snapshot()
        return PassResult(COMPLETE, n)

    def _maybe_snapshot(self, n: int) -> None:
        now = time.time()
        if (now - self._last_snapshot >= SNAPSHOT_EVERY_SECONDS
                or (n and n % SNAPSHOT_EVERY_PAGES == 0)):
            assert self.state is not None
            self.state.snapshot()
            self._last_snapshot = now

    def _progress(self, n: int) -> None:
        assert self.state is not None
        st = self.state
        self.log(f"[{self.adapter.name}] pages+{n} done={len(st.done)} "
                 f"queued={len(st.queue)} dead={len(st.dead)} "
                 f"assets={len(st.asset_registry)} errors={len(st.errors)}")

    # --- one page -----------------------------------------------------------
    def process_page(self, url: str) -> None:
        assert self.state is not None
        st = self.state
        st.discovered.add(url)

        if not self.adapter.is_html_page_url(url):
            # an asset URL that landed in the page queue
            if self.adapter.looks_like_asset_url(url):
                mirror_asset_record(self.adapter, st, self.fetch, url, self.out_dir,
                                    "", self.delay * 0.5, kind="asset-link")
            st.done.add(url)
            return

        if self.delay and self.stop_event.wait(self.delay):
            raise StoppedError()
        raw = self.fetch(url)
        html_text = raw.decode(self.adapter.encoding, "replace")

        links = [classify_link(self.adapter, h, url) for h in page_links(html_text)]
        assets = collect_html_assets(self.adapter, html_text, url)
        stylesheet_urls = parse_stylesheet_hrefs(self.adapter, html_text, url)

        # adapter-specific extra page targets (e.g. File: detail pages) — front
        extra_targets = self.adapter.collect_extra_targets(html_text, url)
        for tgt in extra_targets:
            self._enqueue(tgt, front=True)
        # adapter-specific extra assets (e.g. full image on a File: page)
        assets = assets + self.adapter.extract_page_extra_assets(html_text, url)

        for link in links:
            if link["kind"] == "internal":
                norm = self.adapter.normalize_page_url(link["url"])
                st.discovered.add(norm)
                if norm in st.done or norm in st.dead:
                    continue
                if self.adapter.is_crawlable_page(norm):
                    st.queue.append(norm)

        mirror_dir, html_path, yaml_path = self.adapter.page_files(self.out_dir, url)
        mirror_dir.mkdir(parents=True, exist_ok=True)
        html_path.write_bytes(raw)

        css_records = [mirror_stylesheet(self.adapter, st, self.fetch, u, self.out_dir,
                                         url, self.delay * 0.5) for u in stylesheet_urls]
        image_records: list[dict[str, Any]] = []
        for asset in assets:
            rec = mirror_asset_record(self.adapter, st, self.fetch, asset["url"],
                                      self.out_dir, url, self.delay * 0.5)
            rec.update({k: asset[k] for k in ("src", "alt", "kind", "file_page")
                        if k in asset})
            image_records.append(rec)

        rel_mirror = str(mirror_dir.relative_to(self.adapter.mirror_root(self.out_dir)))
        rel_mirror = rel_mirror.replace("\\", "/")
        meta: dict[str, Any] = {
            "url": url,
            "mirror_dir": rel_mirror,
            "page_type": self.adapter.page_type(url),
            "title": extract_first(TITLE_RE, html_text),
            "h1": extract_first(H1_RE, html_text),
            "description": (extract_first(META_DESC_RE, html_text)
                            or extract_first(META_DESC_RE2, html_text)),
            "fetched_at": _now(),
            "content_type": "text/html",
            "encoding": self.adapter.encoding,
            "links": links,
            "images": image_records,
            "stylesheets": css_records,
            "stats": {
                "links": len(links),
                "images": len(image_records),
                "stylesheets": len(css_records),
                "internal_out": sum(1 for l in links if l["kind"] == "internal"),
            },
        }
        meta.update(self.adapter.page_extra_meta(html_text, url))
        yaml_path.write_text(yamlio.dump(meta, sort_keys=False, allow_unicode=True),
                             encoding="utf-8")

        st.done.add(url)
        st.attempts.pop(url, None)

    # --- backfill: re-fetch assets missing from already-mirrored pages ------
    def backfill_assets(self, *, force: bool = False) -> int:
        """Walk mirrored pages and (re)download any asset whose local file is
        missing. Used to repair gaps left by an outage during the asset phase."""
        assert self.state is not None
        st = self.state
        root = self.adapter.mirror_root(self.out_dir)
        if not root.exists():
            return 0
        repaired = 0
        for yaml_path in root.rglob("page.yaml"):
            if self.stop_event.is_set() or self.health.is_open():
                break
            try:
                meta = yamlio.load(yaml_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(meta, dict):
                continue
            changed = False
            for rec in meta.get("images", []) or []:
                if not isinstance(rec, dict):
                    continue
                local = rec.get("local")
                if not local:
                    continue
                p = self.out_dir / local
                if p.exists() and p.stat().st_size > 0 and not force:
                    continue
                try:
                    new = mirror_asset_record(self.adapter, st, self.fetch, rec["url"],
                                              self.out_dir, meta.get("url", ""),
                                              self.delay * 0.5, kind=rec.get("kind", "image"),
                                              force=force)
                except (PermanentFetchError, TransientFetchError, CircuitOpenError, StoppedError):
                    continue
                if new.get("sha256"):
                    rec["sha256"] = new["sha256"]
                    rec.pop("error", None)
                    changed = True
                    repaired += 1
            if changed:
                yaml_path.write_text(
                    yamlio.dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return repaired
