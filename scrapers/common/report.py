"""Final report writers: manifest.yaml, site-tree.yaml, pages-index.yaml,
assets-index.yaml.

The page index and asset rollup are rebuilt from the authoritative on-disk mirror
(every page.yaml and its image/stylesheet records) rather than from in-memory state,
so they stay complete and correct across resumes and partial runs.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import yamlio
from .htmlparse import build_tree

if TYPE_CHECKING:
    from .site import SiteAdapter
    from .state import CrawlState


def _scan_mirror(adapter: "SiteAdapter", out_dir: Path):
    root = adapter.mirror_root(out_dir)
    pages: list[dict[str, Any]] = []
    assets: dict[str, dict[str, Any]] = {}

    def add_asset(rec: Any, source_page: str) -> None:
        if not isinstance(rec, dict):
            return
        url = rec.get("url")
        if not url:
            return
        entry = assets.get(url)
        if entry is None:
            entry = {"url": url, "kind": rec.get("kind", "image"),
                     "local": rec.get("local"), "sha256": rec.get("sha256"), "sources": []}
            assets[url] = entry
        if rec.get("sha256") and not entry.get("sha256"):
            entry["sha256"] = rec["sha256"]
        if rec.get("local") and not entry.get("local"):
            entry["local"] = rec["local"]
        if source_page and source_page not in entry["sources"]:
            entry["sources"].append(source_page)
        for nested in rec.get("nested_assets", []) or []:
            add_asset(nested, source_page)

    if root.exists():
        for yaml_path in root.rglob("page.yaml"):
            try:
                meta = yamlio.load(yaml_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(meta, dict) or not meta.get("url"):
                continue
            url = str(meta["url"])
            pages.append({
                "url": url,
                "title": meta.get("title"),
                "mirror_dir": meta.get("mirror_dir"),
                "mirror_path": adapter.url_to_mirror_rel(url),
            })
            for rec in meta.get("images", []) or []:
                add_asset(rec, url)
            for rec in meta.get("stylesheets", []) or []:
                add_asset(rec, url)
    return pages, list(assets.values())


def write_reports(adapter: "SiteAdapter", state: "CrawlState", out_dir: Path,
                  *, started: str, finished: str) -> dict[str, Any]:
    pages, assets = _scan_mirror(adapter, out_dir)
    manifest = {
        "site": adapter.label,
        "base_url": adapter.base_url,
        "copyright_notice": adapter.copyright_notice,
        "crawl_started": started,
        "crawl_finished": finished,
        "pages_fetched": len(state.done),
        "pages_in_index": len(pages),
        "pages_discovered": len(state.discovered),
        "assets_tracked": len(assets),
        "dead_urls": len(state.dead),
        "errors": list(state.errors),
        "user_agent": adapter.user_agent,
    }
    (out_dir / "manifest.yaml").write_text(
        yamlio.dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (out_dir / "site-tree.yaml").write_text(
        yamlio.dump({"tree": build_tree(pages)}, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    (out_dir / "pages-index.yaml").write_text(
        yamlio.dump({"pages": pages}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (out_dir / "assets-index.yaml").write_text(
        yamlio.dump({"assets": assets}, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return manifest
