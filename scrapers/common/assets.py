"""Asset mirroring: download, register, and recurse into stylesheets.

Decoupled from the network policy — callers pass a `fetch_fn(url, timeout) -> bytes`
that already applies retry/backoff and the health/circuit-breaker guard. On-disk
files that already exist are never re-fetched, which is what makes resuming cheap.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .htmlparse import parse_css_asset_urls

if TYPE_CHECKING:
    from .site import SiteAdapter
    from .state import CrawlState

FetchFn = Callable[..., bytes]


def rel_archive_path(out_dir: Path, dest: Path) -> str:
    return dest.relative_to(out_dir).as_posix()


def download_asset(fetch_fn: FetchFn, asset_url: str, dest: Path, delay: float,
                   errors, *, force: bool = False) -> tuple[str | None, str | None]:
    """Returns (sha256, error_reason). error_reason is None on success."""
    if dest.exists() and dest.stat().st_size > 0 and not force:
        return hashlib.sha256(dest.read_bytes()).hexdigest(), None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if delay:
        time.sleep(delay)
    try:
        data = fetch_fn(asset_url)
        if not data:
            raise OSError("empty response")
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest(), None
    except Exception as exc:  # noqa: BLE001 — failure is recorded, not raised
        reason = str(exc)
        errors.append({"url": asset_url, "error": reason, "dest": str(dest)})
        try:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None, reason


def register_asset(adapter: "SiteAdapter", state: "CrawlState", asset_url: str,
                   dest: Path, out_dir: Path, source_page: str, *,
                   kind: str = "image") -> str:
    key = adapter.asset_key(asset_url)
    entry = state.asset_registry.get(key)
    if entry is None:
        entry = {
            "url": asset_url,
            "kind": kind,
            "local": rel_archive_path(out_dir, dest),
            "sha256": None,
            "sources": [],
        }
        state.asset_registry[key] = entry
        state.note_new_asset(key, entry)
    if source_page and source_page not in entry["sources"]:
        entry["sources"].append(source_page)
    return key


def mirror_asset_record(adapter: "SiteAdapter", state: "CrawlState", fetch_fn: FetchFn,
                        asset_url: str, out_dir: Path, source_page: str, delay: float,
                        *, kind: str = "image", force: bool = False) -> dict[str, Any]:
    dest = adapter.asset_mirror_path(out_dir, asset_url)
    if dest is None:
        return {"url": asset_url, "local": None, "skipped": "external", "kind": kind}
    sha, reason = download_asset(fetch_fn, asset_url, dest, delay, state.errors, force=force)
    rel_local = rel_archive_path(out_dir, dest)
    register_asset(adapter, state, asset_url, dest, out_dir, source_page, kind=kind)
    entry: dict[str, Any] = {"url": asset_url, "local": rel_local, "sha256": sha, "kind": kind}
    if sha is None:
        entry["error"] = reason or "download_failed"
    return entry


def mirror_stylesheet(adapter: "SiteAdapter", state: "CrawlState", fetch_fn: FetchFn,
                      css_url: str, out_dir: Path, source_page: str, delay: float,
                      *, force: bool = False) -> dict[str, Any]:
    record = mirror_asset_record(adapter, state, fetch_fn, css_url, out_dir,
                                 source_page, delay, kind="css", force=force)
    nested: list[dict[str, Any]] = []
    if record.get("sha256"):
        dest = adapter.asset_mirror_path(out_dir, css_url)
        if dest and dest.exists():
            css_text = dest.read_bytes().decode(adapter.encoding, "replace")
            for ref_url in parse_css_asset_urls(adapter, css_text, css_url):
                if ref_url.lower().endswith(".css"):
                    nested.append(mirror_stylesheet(adapter, state, fetch_fn, ref_url,
                                                    out_dir, source_page, delay, force=force))
                else:
                    nested.append(mirror_asset_record(adapter, state, fetch_fn, ref_url,
                                                      out_dir, source_page, delay, force=force))
    record["nested_assets"] = nested
    return record
