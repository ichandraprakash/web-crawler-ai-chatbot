"""Find all crawlable URLs on the target site.

Tries a sitemap first (fast, structured), checking a few common index paths
since sites name these differently — WordPress's /wp-sitemap.xml, or a plain
/sitemap.xml / /sitemap_index.xml pointing at child sitemaps like
sitemap-page.xml, sitemap-post.xml, sitemap-category.xml, etc. Whatever the
naming, we only exclude child sitemaps/URLs that look like taxonomy noise
(category, tag, author pages) rather than real content — so this keeps
working even if the site's sitemap layout changes later.

If no sitemap can be found or parsed at all, falls back to a plain
breadth-first crawl: start at the homepage, follow same-domain <a> links,
skip obvious non-content files (images, PDFs, etc.), stop after MAX_PAGES or
MAX_DEPTH.
"""
import json
import os
import pathlib
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

BASE = os.environ.get("SITE_BASE_URL", "https://www.embracehealthcare.com.au").rstrip("/")

# Sitemap index paths to try, in order, until one parses successfully.
CANDIDATE_SITEMAP_PATHS = ["/sitemap.xml", "/wp-sitemap.xml", "/sitemap_index.xml"]

# Child sitemaps or individual URLs containing any of these are skipped —
# they're taxonomy/archive noise, not pages a visitor would actually land on.
EXCLUDE_SUBSTRINGS = [
    "category", "categories", "/tag/", "tag-", "sitemap-tag",
    "author", "taxonomy", "/user/", "sitemap-user", "signature",
]

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
HEADERS = {"User-Agent": "EmbraceChatbotMock/1.0 (+internal prototype)"}

OUT_FILE = pathlib.Path(__file__).parent / "urls.json"

# Link-crawl fallback settings
MAX_PAGES = 300
MAX_DEPTH = 4
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip",
    ".css", ".js", ".json", ".xml", ".mp4", ".mp3", ".woff", ".woff2",
)
SKIP_PREFIXES = ("mailto:", "tel:", "javascript:", "#")


def is_excluded(url: str) -> bool:
    lower = url.lower()
    return any(s in lower for s in EXCLUDE_SUBSTRINGS)


def fetch_sitemap(url: str) -> tuple[str, list[str]]:
    """Returns (root_tag, locs). root_tag is 'sitemapindex' or 'urlset'."""
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    tag = root.tag.split("}")[-1]
    locs = [loc.text.strip() for loc in root.findall(".//sm:loc", NS) if loc.text]
    return tag, locs


def try_sitemaps() -> list[str] | None:
    for path in CANDIDATE_SITEMAP_PATHS:
        index_url = f"{BASE}{path}"
        try:
            tag, locs = fetch_sitemap(index_url)
        except Exception as e:
            print(f"No sitemap at {index_url} ({e})")
            continue

        if tag == "sitemapindex":
            print(f"Found sitemap index at {index_url}: {len(locs)} child sitemaps")
            selected = [l for l in locs if not is_excluded(l)]
            skipped = [l for l in locs if is_excluded(l)]
            print(f"Using {len(selected)} of them:")
            for s in selected:
                print(f"  - {s}")
            if skipped:
                print(f"Skipping {len(skipped)} (category/tag/author-style): {skipped}")

            all_urls: set[str] = set()
            for sitemap_url in selected:
                try:
                    _, urls = fetch_sitemap(sitemap_url)
                    urls = [u for u in urls if not is_excluded(u)]
                    print(f"  {sitemap_url}: {len(urls)} urls")
                    all_urls.update(urls)
                except Exception as e:
                    print(f"  FAILED {sitemap_url}: {e}")

            if all_urls:
                return sorted(all_urls)
            print(f"  {index_url} had no usable child sitemaps — trying next candidate")

        elif tag == "urlset":
            urls = [u for u in locs if not is_excluded(u)]
            print(f"Found flat sitemap at {index_url}: {len(urls)} usable urls (of {len(locs)})")
            if urls:
                return sorted(set(urls))

    return None


def normalize(url: str) -> str:
    """Strip fragment/query, drop trailing slash inconsistency."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def is_content_link(url: str, base_netloc: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_netloc:
        return False
    if any(parsed.path.lower().endswith(ext) for ext in SKIP_EXTENSIONS):
        return False
    if is_excluded(url):
        return False
    return True


def crawl_by_links() -> list[str]:
    base_netloc = urlparse(BASE).netloc
    seen: set[str] = {normalize(BASE)}
    queue: list[tuple[str, int]] = [(BASE, 0)]
    found: list[str] = []

    while queue and len(found) < MAX_PAGES:
        url, depth = queue.pop(0)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except Exception as e:
            print(f"  FAILED {url}: {e}")
            continue

        found.append(url)
        print(f"  [{len(found)}] depth={depth} {url}")

        if depth >= MAX_DEPTH:
            continue

        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(SKIP_PREFIXES):
                continue
            absolute = urljoin(url, href)
            if not absolute.startswith(("http://", "https://")):
                continue
            if not is_content_link(absolute, base_netloc):
                continue
            norm = normalize(absolute)
            if norm in seen:
                continue
            seen.add(norm)
            queue.append((absolute, depth + 1))

    return sorted(set(found))


def main():
    urls = try_sitemaps()

    if not urls:
        print(f"\nNo usable sitemap found — crawling {BASE} by following links instead (max {MAX_PAGES} pages, depth {MAX_DEPTH})...")
        urls = crawl_by_links()

    OUT_FILE.write_text(json.dumps(urls, indent=2))
    print(f"\nWrote {len(urls)} URLs to {OUT_FILE}")


if __name__ == "__main__":
    main()
