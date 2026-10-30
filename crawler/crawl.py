"""Crawl every URL in urls.json and save clean page content as JSON.

Run discover_urls.py first to produce urls.json.
"""
import hashlib
import json
import os
import pathlib
import re
import time

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")

HEADERS = {"User-Agent": "EmbraceChatbotMock/1.0 (+internal prototype)"}
REQUEST_DELAY_SECONDS = 0.5  # be polite to the origin server

BASE = os.environ.get("SITE_BASE_URL", "https://www.embracehealthcare.com.au").rstrip("/")

HERE = pathlib.Path(__file__).parent
URLS_FILE = HERE / "urls.json"
OUT_DIR = HERE / "crawl_output"


def clean_page(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url

    for tag in soup.select("nav, footer, header, script, style, aside, form"):
        tag.decompose()

    body = soup.find("main") or soup.find("article") or soup.body
    raw_text = body.get_text(separator=" ", strip=True) if body else ""
    text = re.sub(r"\s+", " ", raw_text).strip()

    return {
        "url": url,
        "title": title,
        "text": text,
        "hash": hashlib.sha256(text.encode()).hexdigest(),
    }


def slug_for(url: str) -> str:
    path = url.replace(BASE, "").strip("/")
    return path.replace("/", "__") or "home"


def main():
    if not URLS_FILE.exists():
        raise SystemExit("urls.json not found — run discover_urls.py first")

    urls = json.loads(URLS_FILE.read_text())

    # Wipe previous output first — otherwise leftover pages from a prior
    # site (e.g. after changing SITE_BASE_URL) would linger alongside the
    # new crawl and get mixed into chunks.json by build_context.py.
    if OUT_DIR.exists():
        for f in OUT_DIR.glob("*.json"):
            f.unlink()
    OUT_DIR.mkdir(exist_ok=True)

    ok, failed = 0, 0
    for url in urls:
        try:
            page = clean_page(url)
            slug = slug_for(url)
            (OUT_DIR / f"{slug}.json").write_text(json.dumps(page, indent=2))
            print(f"OK   {url}  ({len(page['text'])} chars)")
            ok += 1
        except Exception as e:
            print(f"FAIL {url}: {e}")
            failed += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nDone. {ok} pages saved to {OUT_DIR}, {failed} failed.")


if __name__ == "__main__":
    main()
