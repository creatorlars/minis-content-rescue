"""Archive path resolution. Replaces the old tools/_scrape_paths.py."""
from __future__ import annotations

from pathlib import Path

# repo root = .../minis-content-rescue  (this file is scrapers/common/paths.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = REPO_ROOT / "archive"


def archive_dir(site_name: str) -> Path:
    """Working mirror directory for a site (gitignored)."""
    return ARCHIVE_ROOT / site_name
