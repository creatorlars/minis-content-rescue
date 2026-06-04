"""Solegends (The Stuff of Legends) adapter — a static late-1990s HTML site.

URL/identity/layout logic ported verbatim from the old tools/solegends_scrape.py so
the existing mirror resumes unchanged. Transport is plain urllib. The thousands of
HTTP 300/404 from broken legacy links are now classified PERMANENT by the shared
engine (retry.py) and parked in the dead-letter set, so the queue can finally drain.
"""
from __future__ import annotations

from pathlib import Path
from posixpath import normpath
from urllib.parse import urlparse

from ..common.fetcher import UrllibFetcher
from ..common.paths import archive_dir
from ..common.site import SiteAdapter

BASE = "http://www.solegends.com/"
HOSTS = {"www.solegends.com", "solegends.com"}
MIRROR_HOST = "www.solegends.com"
ASSET_EXTS = {".jpg", ".jpeg", ".gif", ".png", ".bmp", ".webp", ".ico", ".css", ".svg"}
HTML_EXTS = {".htm", ".html"}


class SolegendsAdapter(SiteAdapter):
    name = "solegends"
    label = "The Stuff of Legends"
    base_url = BASE
    hosts = HOSTS
    mirror_host = MIRROR_HOST
    user_agent = "LZX-SOL-Archive/1.0 (+local static mirror; contact via workspace owner)"
    copyright_notice = ("Page and site contents ©1998-2008 The Stuff of Legends, "
                        "may not be copied without permission (see disclaimers.htm).")
    encoding = "iso-8859-1"
    default_delay = 0.35

    def __init__(self, fetcher=None):
        super().__init__(fetcher or UrllibFetcher(self.user_agent))

    @staticmethod
    def default_out_dir() -> Path:
        return archive_dir("solegends")

    # --- URL identity (verbatim from solegends_scrape.py) -------------------
    def is_internal(self, url: str) -> bool:
        p = urlparse(url)
        return p.netloc.lower() in HOSTS or p.netloc == ""

    def normalize_page_url(self, url: str) -> str:
        p = urlparse(url)
        path = (p.path or "/").replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        path = normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        host = p.netloc.lower() or MIRROR_HOST
        return f"http://{host}{path}"

    def is_html_page_url(self, url: str) -> bool:
        path = urlparse(self.normalize_page_url(url)).path.lower()
        if path.endswith("/") or path == "/":
            return True
        ext = Path(path).suffix
        return not ext or ext in HTML_EXTS

    def is_crawlable_page(self, url: str) -> bool:
        return self.is_internal(url) and self.is_html_page_url(url)

    # --- mirror layout ------------------------------------------------------
    def url_to_mirror_rel(self, url: str) -> str:
        p = urlparse(self.normalize_page_url(url))
        path = p.path
        if path.endswith("/"):
            path = path + "index.htm"
        if path == "/":
            path = "/index.htm"
        rel = path.lstrip("/")
        stem = Path(rel).name
        if stem.lower().endswith((".htm", ".html")):
            return str(Path(rel).parent / stem).replace("\\", "/")
        return rel.replace("\\", "/")

    def page_files(self, out_dir: Path, page_url: str):
        rel = self.url_to_mirror_rel(page_url)
        base = self.mirror_root(out_dir) / rel
        if rel.lower().endswith((".htm", ".html")):
            d = base.parent / Path(rel).stem
        else:
            d = base
        return d, d / "page.html", d / "page.yaml"

    def asset_mirror_path(self, out_dir: Path, asset_url: str):
        if not self.is_internal(asset_url):
            return None
        p = urlparse(self.normalize_page_url(asset_url))
        rel = (p.path or "/").lstrip("/")
        if not rel:
            return None
        return self.mirror_root(out_dir) / rel.replace("\\", "/")

    # --- asset canon --------------------------------------------------------
    def looks_like_asset_url(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in ASSET_EXTS)
