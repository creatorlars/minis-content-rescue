"""Lost Minis Wiki adapter (MediaWiki behind a Sucuri WAF).

URL/identity/layout logic is ported verbatim from the old tools/lostminis_scrape.py
so the existing on-disk mirror resumes byte-for-byte. Only the engine around it
changed. Transport is Playwright (plain urllib is blocked by the WAF).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from posixpath import normpath
from urllib.parse import parse_qs, quote, urljoin, urlparse

from ..common.fetcher import PlaywrightFetcher
from ..common.paths import archive_dir
from ..common.site import SiteAdapter

BASE = "https://www.miniatures-workshop.com/lostminiswiki/index.php?title=Main_Page"
HOSTS = {"www.miniatures-workshop.com", "miniatures-workshop.com"}
MIRROR_HOST = "www.miniatures-workshop.com"
WIKI_PREFIX = "/lostminiswiki/"
ASSET_EXTS = {".jpg", ".jpeg", ".gif", ".png", ".bmp", ".webp", ".ico", ".css", ".svg"}

FILE_HREF_RE = re.compile(
    r"""href=["']([^"']*index\.php\?title=File:[^"'&]+)["']""", re.I)
MW_FILE_DESC_RE = re.compile(
    r"""<a[^>]+href=["']([^"']*title=File:[^"']+)["'][^>]*class=["']mw-file-description["'][^>]*>"""
    r"""(?:.|\n)*?<img[^>]+src=["']([^"']+)["']""", re.I)
FULL_IMAGE_LINK_RE = re.compile(
    r"""<div class="fullImageLink"[^>]*>\s*<a href=["']([^"']+)["']""", re.I)


class LostMinisAdapter(SiteAdapter):
    name = "lostminis"
    label = "Lost Minis Wiki"
    base_url = BASE
    hosts = HOSTS
    mirror_host = MIRROR_HOST
    user_agent = "LZX-LMW-Archive/1.0 (+local static mirror; contact via workspace owner)"
    copyright_notice = ("Content © contributors and Lost Minis Wiki; "
                        "respect site terms and MediaWiki licensing when reusing.")
    encoding = "utf-8"
    default_delay = 0.35
    has_file_detail_pages = True

    def __init__(self, fetcher=None):
        super().__init__(fetcher or PlaywrightFetcher(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"))

    @staticmethod
    def default_out_dir() -> Path:
        return archive_dir("lostminis")

    # --- URL identity (verbatim from lostminis_scrape.py) -------------------
    def is_internal(self, url: str) -> bool:
        p = urlparse(urljoin(BASE, url))
        if p.netloc and p.netloc.lower() not in HOSTS:
            return False
        path = (p.path or "/").replace("\\", "/")
        return path.startswith(WIKI_PREFIX)

    def wiki_title(self, url: str) -> str | None:
        p = urlparse(self.normalize_page_url(url))
        if not self.is_internal(url):
            return None
        titles = parse_qs(p.query).get("title")
        return titles[0] if titles else None

    @staticmethod
    def title_to_mirror_slug(title: str) -> str:
        slug = title.replace("/", "__").replace("\\", "__")
        for ch in '<>:"|?*':
            slug = slug.replace(ch, "_")
        return slug

    def normalize_page_url(self, url: str) -> str:
        p = urlparse(urljoin(BASE, url))
        host = p.netloc.lower() or MIRROR_HOST
        path = (p.path or "/").replace("\\", "/")
        if not path.startswith(WIKI_PREFIX):
            path = WIKI_PREFIX.rstrip("/") + path if path.startswith("/") \
                else WIKI_PREFIX + path.lstrip("/")
        path = normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        titles = parse_qs(p.query).get("title")
        if titles:
            title = titles[0].replace(" ", "_")
            return f"https://{host}{WIKI_PREFIX}index.php?title={quote(title, safe='/:')}"
        if path.rstrip("/").endswith("index.php"):
            return f"https://{host}{WIKI_PREFIX}index.php?title=Main_Page"
        return f"https://{host}{path}"

    def is_crawlable_page(self, url: str) -> bool:
        if not self.is_internal(url):
            return False
        title = self.wiki_title(url)
        if title is None:
            return False
        if title.startswith("Special:"):
            return False
        action = parse_qs(urlparse(url).query).get("action", [""])[0]
        return action in ("", "view")

    def is_html_page_url(self, url: str) -> bool:
        return self.is_crawlable_page(url)

    def is_file_detail_page(self, url: str) -> bool:
        title = self.wiki_title(url)
        return bool(title and title.startswith("File:"))

    # --- mirror layout ------------------------------------------------------
    def url_to_mirror_rel(self, url: str) -> str:
        title = self.wiki_title(url)
        if title:
            return f"lostminiswiki/pages/{self.title_to_mirror_slug(title)}"
        p = urlparse(self.normalize_page_url(url))
        return (p.path or "/").lstrip("/")

    def page_files(self, out_dir: Path, page_url: str):
        rel = self.url_to_mirror_rel(page_url)
        d = self.mirror_root(out_dir) / rel
        return d, d / "page.html", d / "page.yaml"

    def asset_mirror_path(self, out_dir: Path, asset_url: str):
        if not self.is_internal(asset_url):
            return None
        p = urlparse(self.normalize_asset_url(asset_url))
        rel = (p.path or "/").lstrip("/")
        if not rel:
            return None
        if p.query:
            rel = f"{rel}/_q_{hashlib.md5(p.query.encode()).hexdigest()[:16]}"
        return self.mirror_root(out_dir) / rel.replace("\\", "/")

    # --- asset canon --------------------------------------------------------
    def looks_like_asset_url(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in ASSET_EXTS):
            return True
        return "/images/" in path or path.endswith("load.php") or "/skins/" in path

    def normalize_asset_url(self, url: str) -> str:
        p = urlparse(urljoin(BASE, url))
        host = p.netloc.lower() or MIRROR_HOST
        path = (p.path or "").replace("\\", "/")
        if "/images/" in path and p.query:
            return f"https://{host}{path}"
        if not p.netloc:
            return urljoin(BASE, url)
        return p.geturl()

    def asset_key(self, url: str) -> str:
        if self.is_internal(url) and "/images/" in urlparse(url).path:
            return self.normalize_asset_url(url)
        if self.is_internal(url):
            return self.normalize_page_url(url)
        return url

    def mediawiki_thumb_to_full(self, url: str) -> str | None:
        p = urlparse(self.normalize_asset_url(url))
        path = p.path
        marker = "/images/thumb/"
        if marker not in path:
            return None
        prefix, rest = path.split(marker, 1)
        parts = rest.split("/")
        if len(parts) < 2:
            return None
        original = "/".join(parts[:-1])
        return f"https://{p.netloc.lower() or MIRROR_HOST}{prefix}/images/{original}"

    def expand_asset_url(self, url: str) -> list[str]:
        full = self.mediawiki_thumb_to_full(url)
        return [full] if full else []

    # --- crawl hooks --------------------------------------------------------
    def page_type(self, url: str) -> str:
        return "file_detail" if self.is_file_detail_page(url) else "article"

    def extra_html_assets(self, html_text: str, page_url: str):
        out = []
        for file_href, img_src in MW_FILE_DESC_RE.findall(html_text):
            file_page = self.normalize_page_url(urljoin(page_url, file_href))
            out.append((img_src, "img-thumb", "", file_page))
        return out

    def _collect_file_page_urls(self, html_text: str, page_url: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for href in FILE_HREF_RE.findall(html_text):
            full = self.normalize_page_url(urljoin(page_url, href))
            if self.is_file_detail_page(full) and full not in seen:
                seen.add(full)
                found.append(full)
        return found

    def collect_extra_targets(self, html_text: str, page_url: str) -> list[str]:
        return self._collect_file_page_urls(html_text, page_url)

    def _full_image(self, html_text: str, page_url: str) -> str | None:
        m = FULL_IMAGE_LINK_RE.search(html_text)
        return self.normalize_asset_url(urljoin(page_url, m.group(1))) if m else None

    def extract_page_extra_assets(self, html_text: str, page_url: str) -> list[dict]:
        if not self.is_file_detail_page(page_url):
            return []
        full = self._full_image(html_text, page_url)
        if not full:
            return []
        return [{"src": full, "url": full, "alt": self.wiki_title(page_url) or "",
                 "kind": "file-detail-full"}]

    def page_extra_meta(self, html_text: str, page_url: str) -> dict:
        meta: dict = {"wiki_title": self.wiki_title(page_url)}
        file_urls = self._collect_file_page_urls(html_text, page_url)
        if file_urls and not self.is_file_detail_page(page_url):
            meta["file_detail_pages"] = [
                {"url": u, "mirror_dir": self.url_to_mirror_rel(u)} for u in file_urls]
        if self.is_file_detail_page(page_url):
            full = self._full_image(html_text, page_url)
            if full:
                meta["full_image_url"] = full
        return meta
