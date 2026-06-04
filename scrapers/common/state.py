"""Crawl state: slim, atomic, resumable.

The old scripts re-serialised the *entire* world (queue + every discovered URL +
the whole asset registry + the whole page index) into one crawl-state.json every
10-25 pages. For Lost Minis that meant rewriting a ~167 MB file constantly, getting
slower as the crawl grew.

Here the only persisted state is the frontier needed to resume — queue / done /
discovered / dead / attempts / a capped error tail — written atomically to a small
`state.json`. The page index and asset rollup are not persisted at all; they are
rebuilt from the authoritative on-disk mirror (page.yaml + asset files) at report
time (see report.py).
"""
from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import retry, yamlio

if TYPE_CHECKING:
    from .site import SiteAdapter

STATE_VERSION = 2
ERRORS_CAP = 5000

STATE_FILE = "state.json"
LEGACY_FILE = "crawl-state.json"
LEGACY_RETIRED = "crawl-state.legacy.json"


@dataclass
class CrawlState:
    out_dir: Path
    queue: deque[str] = field(default_factory=deque)
    done: set[str] = field(default_factory=set)
    discovered: set[str] = field(default_factory=set)
    dead: dict[str, str] = field(default_factory=dict)      # url -> reason
    attempts: dict[str, int] = field(default_factory=dict)  # url -> transient retries
    errors: deque = field(default_factory=lambda: deque(maxlen=ERRORS_CAP))
    # in-memory only; the on-disk mirror is the source of truth for reports
    asset_registry: dict[str, dict[str, Any]] = field(default_factory=dict)

    def note_new_asset(self, key: str, entry: dict[str, Any]) -> None:  # noqa: D401
        """No-op hook kept for API symmetry (registry isn't persisted)."""

    def snapshot(self) -> None:
        data = {
            "version": STATE_VERSION,
            "queue": list(self.queue),
            "done": sorted(self.done),
            "discovered": sorted(self.discovered),
            "dead": self.dead,
            "attempts": self.attempts,
            "errors": list(self.errors),
        }
        path = self.out_dir / STATE_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    def close(self) -> None:
        self.snapshot()


# --- mirror scanning --------------------------------------------------------
def load_mirrored_page_urls(adapter: "SiteAdapter", out_dir: Path) -> set[str]:
    urls: set[str] = set()
    root = adapter.mirror_root(out_dir)
    if not root.exists():
        return urls
    for yaml_path in root.rglob("page.yaml"):
        try:
            meta = yamlio.load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(meta, dict) and meta.get("url"):
            urls.add(adapter.normalize_page_url(str(meta["url"])))
    return urls


def reconcile_resume_state(adapter: "SiteAdapter", state: CrawlState, out_dir: Path) -> None:
    """Drop 'done' markers for pages never actually mirrored (the legacy bug: pages
    were marked seen before the fetch, so failures left phantom 'done' entries).
    Re-queue them so they get fetched for real.

    A migrated 'done' set is a superset of what's on disk, so we just check each
    done URL's page.html exists — a stat per URL, far cheaper than parsing every
    page.yaml in the archive."""
    phantom: set[str] = set()
    for url in state.done:
        try:
            _, html_path, _ = adapter.page_files(out_dir, url)
        except Exception:  # noqa: BLE001 — malformed legacy URL
            phantom.add(url)
            continue
        if not html_path.exists() or html_path.stat().st_size == 0:
            phantom.add(url)
    if phantom:
        print(f"[{adapter.name}] re-queueing {len(phantom)} pages marked done but not mirrored")
        state.done -= phantom
        requeue = [u for u in phantom if u not in state.queue and u not in state.dead]
        state.queue.extendleft(reversed(requeue))


def rebuild_queue_from_mirror(adapter: "SiteAdapter", out_dir: Path, state: CrawlState) -> int:
    """Re-derive the crawl frontier from links on already-mirrored pages."""
    from .htmlparse import classify_link, page_links

    queued: set[str] = set(state.queue)
    added = 0
    root = adapter.mirror_root(out_dir)
    if not root.exists():
        return 0
    for yaml_path in root.rglob("page.yaml"):
        try:
            meta = yamlio.load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(meta, dict):
            continue
        page_url = meta.get("url")
        html_path = yaml_path.parent / "page.html"
        if not page_url or not html_path.exists():
            continue
        html_text = html_path.read_bytes().decode(adapter.encoding, "replace")
        for href in page_links(html_text):
            classified = classify_link(adapter, href, page_url)
            if classified["kind"] != "internal":
                continue
            norm = adapter.normalize_page_url(classified["url"])
            state.discovered.add(norm)
            if norm in state.done or norm in queued or norm in state.dead:
                continue
            if adapter.is_crawlable_page(norm):
                state.queue.append(norm)
                queued.add(norm)
                added += 1
        for tgt in adapter.collect_extra_targets(html_text, page_url):
            norm = adapter.normalize_page_url(tgt)
            state.discovered.add(norm)
            if norm in state.done or norm in queued or norm in state.dead:
                continue
            state.queue.append(norm)
            queued.add(norm)
            added += 1
    return added


# --- slim / legacy loaders --------------------------------------------------
def _load_slim(state: CrawlState, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    state.done = set(data.get("done", []))
    state.discovered = set(data.get("discovered", []))
    state.dead = dict(data.get("dead", {}))
    state.attempts = dict(data.get("attempts", {}))
    for e in data.get("errors", []):
        state.errors.append(e)
    state.queue = deque(data.get("queue", []))


_KEY_RE = re.compile(r'^  "(\w+)":')
_STR_RE = re.compile(r'^\s+"(.*)",?\s*$')
_FIELD_RE = re.compile(r'^\s+"(url|error)":\s*"(.*?)",?\s*$')


def _migrate_legacy(adapter: "SiteAdapter", state: CrawlState, path: Path) -> None:
    """Stream the giant legacy crawl-state.json (indent=2) without loading it whole.
    Pulls seen_pages -> done, queue, discovered, and classifies logged errors into
    the dead-letter set so the queue can actually reach empty."""
    print(f"[{adapter.name}] migrating legacy crawl-state.json (streaming)...")
    section: str | None = None
    pending_url: str | None = None
    string_sections = {"seen_pages", "discovered", "queue"}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            km = _KEY_RE.match(line)
            if km:
                section = km.group(1)
                pending_url = None
                continue
            if section in string_sections:
                m = _STR_RE.match(line)
                if m:
                    val = m.group(1)
                    if section == "seen_pages":
                        state.done.add(val)
                    elif section == "discovered":
                        state.discovered.add(val)
                    else:
                        state.queue.append(val)
            elif section == "errors":
                fm = _FIELD_RE.match(line)
                if fm:
                    if fm.group(1) == "url":
                        pending_url = fm.group(2)
                    elif fm.group(1) == "error" and pending_url:
                        if retry.classify(Exception(fm.group(2))) == retry.PERMANENT:
                            state.dead[adapter.normalize_page_url(pending_url)] = fm.group(2)
                        pending_url = None
    _dedupe_queue(adapter, state)


def _dedupe_queue(adapter: "SiteAdapter", state: CrawlState) -> None:
    out: deque[str] = deque()
    seen: set[str] = set()
    for u in state.queue:
        n = adapter.normalize_page_url(u)
        if n in state.done or n in state.dead or n in seen:
            continue
        seen.add(n)
        out.append(n)
    state.queue = out


def load_state(adapter: "SiteAdapter", out_dir: Path, *, resume: bool) -> CrawlState:
    out_dir.mkdir(parents=True, exist_ok=True)
    state = CrawlState(out_dir=out_dir)
    state_path = out_dir / STATE_FILE
    legacy_path = out_dir / LEGACY_FILE

    if resume and state_path.exists():
        _load_slim(state, state_path)
        _dedupe_queue(adapter, state)
    elif resume and legacy_path.exists():
        _migrate_legacy(adapter, state, legacy_path)
        try:
            legacy_path.replace(out_dir / LEGACY_RETIRED)
        except OSError:
            pass
        reconcile_resume_state(adapter, state, out_dir)
    elif resume:
        state.done = load_mirrored_page_urls(adapter, out_dir)

    if resume and not state.queue:
        rebuilt = rebuild_queue_from_mirror(adapter, out_dir, state)
        if rebuilt:
            print(f"[{adapter.name}] rebuilt frontier from mirror: +{rebuilt} URLs")

    if not state.queue:
        for s in (adapter.base_url, *adapter.seed_urls):
            n = adapter.normalize_page_url(s)
            if n not in state.done and n not in state.dead:
                state.queue.append(n)

    print(f"[{adapter.name}] loaded: {len(state.done)} done, {len(state.queue)} queued, "
          f"{len(state.dead)} dead")
    return state
