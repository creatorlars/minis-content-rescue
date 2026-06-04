"""HTML/CSS scanning shared by both sites.

These regexes and helpers were duplicated almost verbatim between the two old
scrapers; the only real divergence was how assets get expanded/keyed, which is now
delegated to the SiteAdapter. The behaviour is otherwise identical to the originals.
"""
from __future__ import annotations

import html.parser
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

if TYPE_CHECKING:
    from .site import SiteAdapter

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
SRC_RE = re.compile(r'\bsrc=["\']([^"\']+)["\']', re.I)
SRCSET_RE = re.compile(r'\bsrcset=["\']([^"\']+)["\']', re.I)
ALT_RE = re.compile(r'\balt=["\']([^"\']*)["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.I)
H1_RE = re.compile(r"<h1[^>]*>([^<]*)</h1>", re.I)
META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']', re.I)
META_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']', re.I)
LINK_CSS_RE = re.compile(
    r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']', re.I)
LINK_CSS_RE2 = re.compile(
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']stylesheet["\']', re.I)
BG_ATTR_RE = re.compile(r'\bbackground=["\']([^"\']+)["\']', re.I)
INPUT_IMG_RE = re.compile(r'<input[^>]+type=["\']image["\'][^>]*>', re.I)
HREF_IMG_RE = re.compile(
    r'href=["\']([^"\']+\.(?:jpg|jpeg|gif|png|bmp|webp))["\']', re.I)
CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")\s]+)['"]?\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(
    r"""@import\s+(?:url\(\s*['"]?([^'")\s]+)['"]?\s*\)|['"]([^'"]+)['"])""", re.I)


class LinkExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        for k, v in attrs:
            if k == "href" and v:
                self.links.append(v.strip())


def extract_first(regex: re.Pattern[str], text: str) -> str | None:
    m = regex.search(text)
    return m.group(1).strip() if m else None


def parse_srcset(value: str, page_url: str) -> list[str]:
    urls: list[str] = []
    for part in value.split(","):
        token = part.strip().split()[0] if part.strip() else ""
        if token:
            urls.append(urljoin(page_url, token))
    return urls


def classify_link(adapter: "SiteAdapter", href: str, page_url: str) -> dict[str, str]:
    full = urljoin(page_url, href.split("#")[0].strip())
    kind = "external"
    if href.startswith("#"):
        kind = "anchor"
    elif href.startswith(("mailto:", "javascript:")):
        kind = href.split(":")[0]
    elif adapter.is_internal(full):
        kind = "internal"
    return {"href": href, "url": full, "kind": kind}


def page_links(html_text: str) -> list[str]:
    parser = LinkExtractor()
    parser.feed(html_text)
    return parser.links


def collect_html_assets(adapter: "SiteAdapter", html_text: str,
                        page_url: str) -> list[dict[str, str]]:
    """Every internal image/asset reference in a page, deduped by canonical URL."""
    found: dict[str, dict[str, str]] = {}

    def add(src: str, kind: str, alt: str = "", *, file_page: str | None = None) -> None:
        src = src.strip()
        if not src or src.startswith(("data:", "javascript:", "#")):
            return
        full = adapter.normalize_asset_url(urljoin(page_url, src))
        if not adapter.is_internal(full):
            return
        if not adapter.looks_like_asset_url(full) and not kind.startswith("img"):
            return
        if full not in found:
            entry = {"src": src, "url": full, "alt": alt, "kind": kind}
            if file_page:
                entry["file_page"] = file_page
            found[full] = entry

    def add_with_full(src: str, kind: str, alt: str = "",
                      *, file_page: str | None = None) -> None:
        add(src, kind, alt, file_page=file_page)
        for extra in adapter.expand_asset_url(urljoin(page_url, src)):
            add(extra, "img-full", alt, file_page=file_page)

    # adapter-specific assets first (e.g. MediaWiki file-description thumbnails)
    for src, kind, alt, file_page in adapter.extra_html_assets(html_text, page_url):
        add_with_full(src, kind, alt, file_page=file_page)

    for tag in IMG_RE.findall(html_text):
        src_m = SRC_RE.search(tag)
        if src_m:
            alt_m = ALT_RE.search(tag)
            add_with_full(src_m.group(1), "img", alt_m.group(1) if alt_m else "")
        srcset_m = SRCSET_RE.search(tag)
        if srcset_m:
            for u in parse_srcset(srcset_m.group(1), page_url):
                add_with_full(u, "img-srcset")

    for tag in INPUT_IMG_RE.findall(html_text):
        src_m = SRC_RE.search(tag)
        if src_m:
            add_with_full(src_m.group(1), "input-image")

    for bg in BG_ATTR_RE.findall(html_text):
        add_with_full(bg, "background-attr")

    for href in HREF_IMG_RE.findall(html_text):
        add_with_full(href, "href-image")

    for match in CSS_URL_RE.finditer(html_text):
        add(match.group(1), "inline-style")

    return list(found.values())


def parse_stylesheet_hrefs(adapter: "SiteAdapter", html_text: str,
                           page_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for rx in (LINK_CSS_RE, LINK_CSS_RE2):
        for href in rx.findall(html_text):
            full = adapter.normalize_page_url(urljoin(page_url, href))
            if full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


def parse_css_asset_urls(adapter: "SiteAdapter", css_text: str,
                         css_url: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for rx in (CSS_URL_RE, CSS_IMPORT_RE):
        for match in rx.finditer(css_text):
            ref = match.group(1) or (
                match.group(2) if match.lastindex and match.lastindex >= 2 else None)
            if not ref or ref.startswith(("data:", "#")):
                continue
            full = adapter.normalize_page_url(urljoin(css_url, ref))
            if adapter.is_internal(full) and full not in seen:
                seen.add(full)
                urls.append(full)
    return urls


def build_tree(pages: list[dict]) -> dict:
    """Nested nav tree keyed by mirror path (ported verbatim from both scripts)."""
    root: dict = {"_pages": []}

    def insert(path_parts: list[str], page_entry: dict) -> None:
        node = root
        for part in path_parts[:-1]:
            node = node.setdefault(part, {"_pages": []})
            if "_pages" not in node:
                node["_pages"] = []
        node.setdefault("_pages", []).append(page_entry)

    for p in pages:
        parts = [x for x in Path(p["mirror_path"]).parts if x]
        insert(parts, {"title": p.get("title"), "url": p["url"], "mirror": p["mirror_dir"]})
    return root
