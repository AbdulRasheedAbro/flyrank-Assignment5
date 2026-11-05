"""
FlyRank Internship — Week 5 Assignment A9
The Polite Scraper (Python lane).

Pipeline: fetch -> extract -> normalize -> validate -> store -> report.
Polite by default: honest UA, 10s timeout, >=500ms delay between live
requests, everything cached on disk, one retry on 5xx/timeout only,
never retries 404/403, survives one broken URL without crashing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, HttpUrl, ValidationError

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_URL = "https://books.toscrape.com"
CATALOGUE_PAGE_1 = f"{BASE_URL}/catalogue/page-1.html"

# ⚠️ Replace with your real GitHub repo URL
USER_AGENT = (
    "FlyRankInternship-A9/1.0 "
    "(+https://github.com/AbdulRasheedAbro/flyrank-Assignment5)"
)
HEADERS = {"User-Agent": USER_AGENT}

TIMEOUT_SECONDS = 10
DELAY_SECONDS = 0.6           # >= 500 ms between live requests
MAX_CATALOGUE_PAGES = 3

CACHE_DIR = "cache"
OUTPUT_DIR = "output"
BOOKS_PATH = os.path.join(OUTPUT_DIR, "books.json")
ERRORS_PATH = os.path.join(OUTPUT_DIR, "errors.json")
REPORT_PATH = os.path.join(OUTPUT_DIR, "run-report.json")

# One deliberately-broken URL: Stage 5 proof that one page can't kill the run.
FAKE_URL = (
    "https://books.toscrape.com/catalogue/"
    "this-does-not-exist_9999/index.html"
)


# --------------------------------------------------------------------------- #
# Schema (Stage 4)
# --------------------------------------------------------------------------- #

class BookRecord(BaseModel):
    """Validated shape of one finished record."""

    title: str
    product_url: HttpUrl
    price_text: str
    price_gbp: float = Field(ge=0)
    availability_text: str
    rating_text: str
    description: Optional[str] = None
    source_page: HttpUrl
    fetched_at: str


# --------------------------------------------------------------------------- #
# Utilities
# --------------------------------------------------------------------------- #

def ensure_dirs() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def cache_path_for(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{digest}.html")


def write_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Stage 1 & 5 — fetch (cache-aware, polite, retry once on transient errors)
# --------------------------------------------------------------------------- #

def fetch(url: str, report: dict) -> Optional[str]:
    """
    Return HTML for `url`. Uses the disk cache when available.
    Retries once on timeout/5xx. Never retries 404 or 403.
    Updates `report` counters. Returns None on failure.
    """
    path = cache_path_for(url)

    # Cache hit
    if os.path.exists(path):
        report["cache_hits"] += 1
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        print(f"CACHE HIT {url} ({len(html)} bytes)")
        return html

    # Two attempts max, only for retryable failures
    for attempt in (1, 2):
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            print(f"FETCH ERROR {url} attempt={attempt} err={exc}")
            if attempt == 2:
                report["failed_pages"] += 1
                return None
            time.sleep(1.0)
            continue

        status = response.status_code

        if status == 200:
            time.sleep(DELAY_SECONDS)          # polite spacing
            with open(path, "w", encoding="utf-8") as f:
                f.write(response.text)
            report["pages_fetched"] += 1
            print(f"FETCH {url} status=200 ({len(response.text)} bytes)")
            return response.text

        # Never retry 4xx we know about
        if status in (403, 404):
            print(f"FETCH FAIL {url} status={status} (no retry)")
            report["failed_pages"] += 1
            return None

        # 5xx → one polite retry
        print(f"FETCH FAIL {url} status={status} attempt={attempt}")
        if attempt == 2:
            report["failed_pages"] += 1
            return None
        time.sleep(1.0)

    return None


# --------------------------------------------------------------------------- #
# Stage 2 — discover catalogue pages and book URLs
# --------------------------------------------------------------------------- #

def discover_book_urls(report: dict) -> list[tuple[str, str]]:
    """
    Walk pages 1 -> 2 -> 3 of the catalogue.
    Return list of (absolute_book_url, source_catalogue_page_url) with duplicates removed.
    """
    pairs: list[tuple[str, str]] = []
    page_url: Optional[str] = CATALOGUE_PAGE_1
    pages_walked = 0

    while page_url and pages_walked < MAX_CATALOGUE_PAGES:
        html = fetch(page_url, report)
        if html is None:
            break

        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.select("article.product_pod h3 a"):
            href = anchor.get("href", "")
            pairs.append((urljoin(page_url, href), page_url))

        next_link = soup.select_one("li.next a")
        page_url = urljoin(page_url, next_link["href"]) if next_link else None
        pages_walked += 1

    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for book_url, source in pairs:
        if book_url not in seen:
            seen.add(book_url)
            unique.append((book_url, source))

    print(
        f"catalogue_pages={pages_walked} "
        f"discovered={len(pairs)} unique_urls={len(unique)}"
    )
    return unique


# --------------------------------------------------------------------------- #
# Stage 3 — extract raw record from one detail page
# --------------------------------------------------------------------------- #

def extract_raw_record(
    book_url: str, source_page: str, report: dict
) -> Optional[dict]:
    html = fetch(book_url, report)
    if html is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    product = soup.select_one("article.product_page") or soup

    h1 = product.select_one("h1")
    title = h1.get_text(strip=True) if h1 else None

    price_el = product.select_one("p.price_color")
    price_text = price_el.get_text(strip=True) if price_el else None

    avail_el = product.select_one("p.instock.availability")
    availability_text = avail_el.get_text(strip=True) if avail_el else None

    rating_el = product.select_one("p.star-rating")
    rating_text = None
    if rating_el and len(rating_el.get("class", [])) > 1:
        rating_text = rating_el["class"][1]

    desc_el = soup.select_one("#product_description ~ p")
    description = desc_el.get_text(strip=True) if desc_el else None

    if not title or not price_text:
        print(f"SKIP malformed book page {book_url}")
        return None

    return {
        "title": title,
        "product_url": book_url,
        "price_text": price_text,
        "availability_text": availability_text,
        "rating_text": rating_text,
        "description": description,
        "source_page": source_page,
        "fetched_at": utc_now_iso(),
    }


# --------------------------------------------------------------------------- #
# Stage 4 — normalise + validate
# --------------------------------------------------------------------------- #

def normalize_price(price_text: Optional[str]) -> float:
    if not price_text:
        return 0.0
    cleaned = (
        price_text.replace("£", "")
        .replace("Â", "")
        .replace(",", "")
        .strip()
    )
    return float(cleaned)


def validate_records(
    raw_records: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Return (good_records, bad_records).
    Dedupe by canonical product_url. Bad records carry the validation reason.
    """
    good_by_url: dict[str, dict] = {}
    bad: list[dict] = []

    for raw in raw_records:
        try:
            enriched = {**raw, "price_gbp": normalize_price(raw["price_text"])}
            record = BookRecord(**enriched)
        except (ValidationError, ValueError, KeyError) as exc:
            bad.append({"raw": raw, "reason": str(exc)})
            continue
        good_by_url[str(record.product_url)] = record.model_dump(mode="json")

    return list(good_by_url.values()), bad


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ensure_dirs()

    started_wall = utc_now_iso()
    started_clock = time.time()

    report = {
        "started_at": started_wall,
        "pages_fetched": 0,
        "cache_hits": 0,
        "valid_records": 0,
        "invalid_records": 0,
        "failed_pages": 0,
    }

    # Stage 2: discover
    work_items = discover_book_urls(report)

    # Stage 5 proof: one deliberate broken URL
    work_items.append((FAKE_URL, CATALOGUE_PAGE_1))

    # Stage 3: extract
    raw_records: list[dict] = []
    for book_url, source_page in work_items:
        raw = extract_raw_record(book_url, source_page, report)
        if raw is not None:
            raw_records.append(raw)

    # Stage 4: validate + store
    good, bad = validate_records(raw_records)
    report["valid_records"] = len(good)
    report["invalid_records"] = len(bad)

    write_json(BOOKS_PATH, good)
    write_json(ERRORS_PATH, bad)

    # Stage 5: report
    report["duration_seconds"] = round(time.time() - started_clock, 2)
    write_json(REPORT_PATH, report)

    print()
    print("=" * 60)
    print(f"books.json records: {len(good)}")
    print(f"errors.json records: {len(bad)}")
    print(f"failed_pages:   {report['failed_pages']}")
    print(f"cache_hits:     {report['cache_hits']}")
    print(f"pages_fetched:  {report['pages_fetched']}")
    print(f"duration:       {report['duration_seconds']} s")
    print("=" * 60)

    if len(good) != 60:
        print(
            f"WARNING: expected 60 valid records, got {len(good)}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())