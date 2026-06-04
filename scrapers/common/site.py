"""SiteAdapter — the seam between the shared crawl engine and per-site quirks.

Each site provides a subclass that knows how to recognise, normalise and lay out its
own URLs, plus which transport (urllib vs Playwright) to fetch with. Everything else
(the crawl loop, retries, health/circuit-breaker, state, validation, reporting) is
shared and lives in this package.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fetcher import Fetcher


class SiteAdapter:
    # --- identity (override as plain attributes on the subclass) -------------
    name: str = ""               # short id, also the archive subfolder name
    label: str = ""              # human-friendly site name (manifest "site")
    base_url: str = ""           # crawl seed
    hosts: set[str] = set()      # netlocs considered "internal"
    mirror_host: str = ""        # top dir under the archive root
    user_agent: str = ""
    copyright_notice: str = ""
    encoding: str = "utf-8"      # how page.html / css bytes are decoded
    default_delay: float = 0.35  # seconds between requests
    seed_urls: tuple[str, ...] = ()

    # Capability flags the validator keys off of.
    has_file_detail_pages: bool = False

    def __init__(self, fetcher: "Fetcher") -> None:
        self.fetcher = fetcher

    # --- URL identity / classification (MUST override) ----------------------
    def is_internal(self, url: str) -> bool:
        raise NotImplementedError

    def normalize_page_url(self, url: str) -> str:
        raise NotImplementedError

    def is_crawlable_page(self, url: str) -> bool:
        raise NotImplementedError

    def is_html_page_url(self, url: str) -> bool:
        raise NotImplementedError

    # --- mirror layout (MUST override) --------------------------------------
    def mirror_root(self, out_dir: Path) -> Path:
        return out_dir / self.mirror_host

    def url_to_mirror_rel(self, url: str) -> str:
        raise NotImplementedError

    def page_files(self, out_dir: Path, page_url: str) -> tuple[Path, Path, Path]:
        """Return (page_dir, page.html path, page.yaml path)."""
        raise NotImplementedError

    def asset_mirror_path(self, out_dir: Path, asset_url: str) -> Path | None:
        raise NotImplementedError

    # --- asset URL canonicalisation -----------------------------------------
    def normalize_asset_url(self, url: str) -> str:
        # Default: assets share the page-URL namespace (Solegends behaviour).
        return self.normalize_page_url(url)

    def looks_like_asset_url(self, url: str) -> bool:
        raise NotImplementedError

    def asset_key(self, url: str) -> str:
        """Registry/dedup key for an asset URL."""
        return self.normalize_asset_url(url) if self.is_internal(url) else url

    def expand_asset_url(self, url: str) -> list[str]:
        """Extra asset URLs implied by one reference (e.g. thumb -> original).

        Default: none. Lost Minis maps MediaWiki thumbnails to full images.
        """
        return []

    # --- optional crawl hooks ------------------------------------------------
    def page_type(self, url: str) -> str:
        return "article"

    def extra_html_assets(self, html_text: str, page_url: str):
        """Yield (src, kind, alt, file_page) tuples for assets the generic
        scanner would miss (Lost Minis: MediaWiki file-description thumbnails)."""
        return []

    def collect_extra_targets(self, html_text: str, page_url: str) -> list[str]:
        """Extra *page* URLs to enqueue (Lost Minis: File: detail pages)."""
        return []

    def extract_page_extra_assets(self, html_text: str, page_url: str) -> list[dict]:
        """Extra assets discoverable only on certain pages (Lost Minis: the
        full-size image linked from a File: detail page)."""
        return []

    def page_extra_meta(self, html_text: str, page_url: str) -> dict:
        """Adapter-specific fields merged into page.yaml (e.g. wiki_title)."""
        return {}
