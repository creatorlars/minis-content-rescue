"""Generic completeness validator — works for both sites (Solegends never had one).

"Complete & valid" means:
  * nothing left in the queue and no pending transient failures;
  * every mirrored page has a non-empty page.html + parseable page.yaml;
  * every referenced internal asset is on disk and non-empty, OR failed with a
    permanent error (404/410/300...) — a genuinely-dead asset can't block 100%.

The runner uses `validate(...).ok` as each site's done signal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import retry, yamlio

if TYPE_CHECKING:
    from .site import SiteAdapter
    from .state import CrawlState


def validate(adapter: "SiteAdapter", out_dir: Path, *,
             state: "CrawlState | None" = None, min_pages: int = 1) -> dict[str, Any]:
    out_dir = Path(out_dir)
    issues: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    root = adapter.mirror_root(out_dir)
    if not root.exists():
        return {"ok": False, "issues": [f"missing mirror root {root}"],
                "warnings": [], "stats": stats}

    # frontier (queue / dead) from live state or the on-disk slim state
    if state is not None:
        queue_len: int | None = len(state.queue)
        dead = len(state.dead)
    else:
        sp = out_dir / "state.json"
        if sp.exists():
            try:
                d = json.loads(sp.read_text(encoding="utf-8"))
                queue_len = len(d.get("queue", []))
                dead = len(d.get("dead", {}))
            except (OSError, json.JSONDecodeError):
                queue_len, dead = None, 0
        else:
            queue_len, dead = None, 0

    pages = file_details = 0
    missing_html = bad_yaml = 0
    assets_total = assets_ok = assets_pending = assets_dead = 0

    for yaml_path in root.rglob("page.yaml"):
        try:
            meta = yamlio.load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            bad_yaml += 1
            continue
        if not isinstance(meta, dict):
            bad_yaml += 1
            continue
        html_path = yaml_path.parent / "page.html"
        if not html_path.exists() or html_path.stat().st_size == 0:
            missing_html += 1
        if meta.get("page_type") == "file_detail":
            file_details += 1
        else:
            pages += 1
        for img in meta.get("images") or []:
            if not isinstance(img, dict):
                continue
            local = img.get("local")
            if not local:
                continue
            assets_total += 1
            p = out_dir / local
            if p.exists() and p.stat().st_size > 0:
                assets_ok += 1
                continue
            err = img.get("error")
            if err and retry.classify(Exception(str(err))) == retry.PERMANENT:
                assets_dead += 1
            else:
                assets_pending += 1

    stats.update({
        "pages": pages,
        "file_details": file_details,
        "queued": queue_len,
        "dead_urls": dead,
        "assets_total": assets_total,
        "assets_ok": assets_ok,
        "assets_missing_pending": assets_pending,
        "assets_missing_dead": assets_dead,
        "missing_page_html": missing_html,
        "bad_yaml": bad_yaml,
    })

    if queue_len:
        issues.append(f"{queue_len} URLs still queued")
    if missing_html:
        issues.append(f"{missing_html} pages missing page.html")
    if bad_yaml:
        issues.append(f"{bad_yaml} unreadable page.yaml")
    if assets_pending:
        issues.append(f"{assets_pending} assets missing (retryable)")
    if pages + file_details < min_pages:
        issues.append(f"only {pages + file_details} pages mirrored (need >= {min_pages})")
    if assets_dead:
        warnings.append(f"{assets_dead} assets permanently unavailable (404/300) — accepted")
    if dead:
        warnings.append(f"{dead} dead URLs (permanent) — accepted")

    return {"ok": not issues, "issues": issues, "warnings": warnings, "stats": stats}
