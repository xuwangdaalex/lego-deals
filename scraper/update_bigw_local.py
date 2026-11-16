#!/usr/bin/env python3
"""Fetch BIG W's public LEGO catalogue locally with Playwright and merge it into the API.

Run from the repository root:
    python3 scraper/update_bigw_local.py

This script updates only BIG W records in api/all-products.json, then refreshes
the derived deal and coverage JSON files. It does not fetch other retailers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is missing. From the repository root, activate .venv-monterey and run: python -m pip install -r requirements-monterey.txt")
    raise SystemExit(1)

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
CATEGORY_URL = "https://www.bigw.com.au/toys/lego/c/6822101"
RETAILER_KEY = "bigw"
THRESHOLD = 20.0
MAX_PAGES = 20
PAGE_SIZE = 48


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def set_number(value: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{4,6})(?!\d)", value or "")
    return match.group(1) if match else None


def parse_catalogue_page(html: str, page_url: str) -> tuple[list[dict], int | None]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    total_match = re.search(r"\b([\d,]+)\s+results\b", page_text, re.I)
    total = int(total_match.group(1).replace(",", "")) if total_match else None
    products = []

    for card in soup.select('[data-testid="product-tile"]'):
        name_node = card.select_one('[data-optly-product-tile-name]')
        price_node = card.select_one('[data-testid="price-value"]')
        link = card.select_one('a[href*="/product/"]')
        if not name_node or not price_node or not link:
            continue

        name = re.sub(r"\s+", " ", name_node.get_text(" ", strip=True)).strip()
        href = urljoin(page_url, link.get("href", "").strip())
        price_raw = price_node.get("content") or price_node.get_text(" ", strip=True)
        try:
            price = float(str(price_raw).replace("$", "").replace(",", "").strip())
        except ValueError:
            continue
        if not name or price <= 0 or "lego" not in name.lower():
            continue

        number = set_number(name + " " + href)
        if not number:
            continue

        image = card.select_one("img")
        image_url = ""
        if image:
            image_url = image.get("src") or image.get("data-src") or ""
        card_text = card.get_text(" ", strip=True)
        discount_match = re.search(r"(\d{1,2})\s*%\s*OFF", card_text, re.I)

        products.append({
            "retailer": RETAILER_KEY,
            "retailer_name": "BIG W",
            "set_number": number,
            "name": name,
            "price_aud": round(price, 2),
            "product_url": href.split("?")[0],
            "rrp_aud": None,
            "discount_percent": None,
            "rrp_url": None,
            "rrp_source": None,
            "verified": False,
            "stock_status": "unknown",
            "image_url": image_url,
            "promotion_label": f"{discount_match.group(1)}% OFF" if discount_match else None,
        })

    return products, total


def scrape(headless: bool = False) -> tuple[list[dict], list[str]]:
    collected: dict[str, dict] = {}
    total_products = None
    scan_errors: list[str] = []
    with sync_playwright() as playwright:
        # BIG W allows the user's installed Chrome but blocks bundled Chromium.
        browser = playwright.chromium.launch(channel="chrome", headless=headless)
        # Use Playwright's default Chrome page, matching the successful local test.
        # BIG W returned 403 with the custom browser context used previously.
        page = browser.new_page()
        try:
            for page_number in range(1, MAX_PAGES + 1):
                url = CATEGORY_URL if page_number == 1 else f"{CATEGORY_URL}?page={page_number}"
                print(f"Opening BIG W catalogue page {page_number}: {url}")
                response = page.goto(url, wait_until="domcontentloaded", timeout=90000)
                if response is not None and response.status >= 400:
                    error = f"BIG W returned HTTP {response.status} on page {page_number}; scan stopped after {len(collected)} unique products."
                    print(f"WARNING: {error}")
                    scan_errors.append(error)
                    break

                # Wait for the actual product cards, not analytics or every network request.
                page.locator('[data-testid="product-tile"]').first.wait_for(timeout=30000)
                page.wait_for_timeout(1500)
                html = page.content()
                products, detected_total = parse_catalogue_page(html, url)
                if detected_total:
                    total_products = detected_total
                if not products:
                    print(f"No product cards found on page {page_number}; stopping.")
                    break

                new_count = 0
                for product in products:
                    key = product["set_number"]
                    if key not in collected:
                        collected[key] = product
                        new_count += 1
                print(f"Page {page_number}: {len(products)} cards, {new_count} new LEGO sets; total unique {len(collected)}.")

                if total_products and len(collected) >= total_products:
                    break
                if len(products) < PAGE_SIZE:
                    break
                time.sleep(1)
        finally:
            browser.close()

    if not collected:
        raise RuntimeError("No BIG W LEGO products were extracted. Existing API files were not changed.")
    print(f"BIG W extraction {'partial' if scan_errors else 'complete'}: {len(collected)} unique products"
          + (f" out of {total_products} catalogue results." if total_products else "."))
    return list(collected.values()), scan_errors


def merge_into_api(bigw_products: list[dict], scan_errors: list[str] | None = None):
    scan_errors = scan_errors or []
    all_path = API / "all-products.json"
    deals_path = API / "lego-deals.json"
    updated_path = API / "last-updated.json"
    history_path = API / "history.json"

    all_data = read_json(all_path, {"products": [], "coverage": {"retailers": {}}})
    products = all_data.get("products", [])
    other_products = [p for p in products if p.get("retailer") != RETAILER_KEY]

    # Use the current Bricks Megastore listing price as the project's reference benchmark.
    reference = {
        str(p.get("set_number")): p
        for p in other_products
        if p.get("retailer") == "bricksmegastore" and p.get("price_aud")
    }
    for product in bigw_products:
        benchmark = reference.get(product["set_number"])
        if benchmark:
            rrp = float(benchmark["price_aud"])
            product.update({
                "rrp_aud": round(rrp, 2),
                "discount_percent": round((rrp - product["price_aud"]) / rrp * 100, 2) if rrp else None,
                "rrp_url": benchmark.get("product_url"),
                "rrp_source": "Bricks Megastore listing price",
                "verified": True,
            })

    merged = other_products + bigw_products
    merged.sort(key=lambda p: (p.get("retailer_name", ""), p.get("set_number", ""), p.get("name", "")))
    generated = datetime.now(timezone.utc).isoformat()

    coverage = all_data.get("coverage") or {}
    retailers = coverage.setdefault("retailers", {})
    qualifying = [
        p for p in bigw_products
        if p.get("verified") and p.get("discount_percent") is not None
        and p["discount_percent"] >= THRESHOLD
    ]
    retailers[RETAILER_KEY] = {
        "name": "BIG W",
        "product_urls_discovered": len(bigw_products),
        "products_with_prices_checked": len(bigw_products),
        "verified_products": sum(1 for p in bigw_products if p.get("verified")),
        "qualifying_deals": len(qualifying),
        "status": "incomplete" if scan_errors else "complete",
        "errors": scan_errors,
        "no_data": False,
        "source": "Local Playwright browser on user's Mac",
    }
    coverage["product_count"] = len(merged)
    coverage["verified_product_count"] = sum(1 for p in merged if p.get("verified"))
    coverage["complete"] = all(
        item.get("status") == "complete" for item in retailers.values()
    )
    coverage["status"] = "complete" if coverage["complete"] else "incomplete"
    coverage["errors"] = [
        error for key, item in retailers.items() if key != RETAILER_KEY
        for error in item.get("errors", [])
    ][:100]

    no_data = [
        {"retailer": key, "name": item.get("name", key), "reason": "No usable data"}
        for key, item in retailers.items() if item.get("no_data")
    ]
    deals = [
        p for p in merged
        if p.get("retailer") != "bricksmegastore"
        and p.get("verified")
        and p.get("discount_percent") is not None
        and p["discount_percent"] >= THRESHOLD
    ]
    deals.sort(key=lambda p: p.get("discount_percent") or 0, reverse=True)

    all_payload = {
        **all_data,
        "generated_at": generated,
        "coverage": coverage,
        "stores_with_no_data": no_data,
        "products": merged,
    }
    deals_payload = read_json(deals_path, {})
    deals_payload.update({
        "generated_at": generated,
        "currency": "AUD",
        "discount_threshold_percent": THRESHOLD,
        "coverage": coverage,
        "stores_with_no_data": no_data,
        "deals": deals,
    })
    updated_payload = read_json(updated_path, {})
    updated_payload.update({
        "status": coverage["status"],
        "generated_at": generated,
        "products_checked": len(merged),
        "verified_products": coverage["verified_product_count"],
        "qualifying_deals": len(deals),
        "retailers": retailers,
        "stores_with_no_data": no_data,
        "message": "BIG W refreshed locally; other retailer records were preserved.",
        "errors": coverage["errors"],
    })

    write_json(all_path, all_payload)
    write_json(deals_path, deals_payload)
    write_json(updated_path, updated_payload)

    history = read_json(history_path, {"runs": []})
    history.setdefault("runs", []).append({
        "generated_at": generated,
        "status": coverage["status"],
        "products_checked": len(merged),
        "verified_products": coverage["verified_product_count"],
        "qualifying_deals": len(deals),
        "source": "Local BIG W refresh",
    })
    history["runs"] = history["runs"][-365:]
    write_json(history_path, history)

    print("\nAPI updated successfully:")
    print(f"  BIG W products: {len(bigw_products)}")
    print(f"  BIG W verified against Bricks Megastore: {retailers[RETAILER_KEY]['verified_products']}")
    print(f"  BIG W deals >= {THRESHOLD:.0f}%: {len(qualifying)}")
    print(f"  Total products across retailers: {len(merged)}")
    print(f"  Overall API status: {coverage['status']}")


def main():
    parser = argparse.ArgumentParser(description="Fetch BIG W LEGO catalogue locally and update the LEGO Deals API.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium without showing its window.")
    args = parser.parse_args()

    if not (API / "all-products.json").exists():
        print("api/all-products.json not found. Run this script from a local clone of the lego-deals repository.")
        raise SystemExit(1)

    products, scan_errors = scrape(headless=args.headless)
    merge_into_api(products, scan_errors)


if __name__ == "__main__":
    main()
