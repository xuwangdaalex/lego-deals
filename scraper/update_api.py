"""Conservative Myer/LEGO AU deal collector.

Only emits a deal when both retailer and official LEGO AU prices are independently
read from product structured data and the set number matches.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LegoDealsMonitor/1.0; +https://github.com/xuwangdaalex/lego-deals)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
TIMEOUT = 25
THRESHOLD = 20.0


def get(url: str) -> str:
    response = SESSION.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    return response.text


def jsonld_objects(soup: BeautifulSoup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                for key in ("itemListElement", "mainEntity"):
                    value = item.get(key)
                    if isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, dict):
                        stack.append(value)


def product_data(url: str):
    soup = BeautifulSoup(get(url), "html.parser")
    for item in jsonld_objects(soup):
        typ = item.get("@type", [])
        types = typ if isinstance(typ, list) else [typ]
        if "Product" not in types:
            continue
        offers = item.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") or offers.get("lowPrice")
        currency = offers.get("priceCurrency")
        if price is None:
            continue
        try:
            amount = float(str(price).replace(",", "").replace("$", ""))
        except ValueError:
            continue
        if currency and currency != "AUD":
            continue
        return {
            "name": item.get("name"),
            "url": item.get("url") or url,
            "sku": str(item.get("sku") or item.get("mpn") or ""),
            "price_aud": amount,
            "currency": currency or "AUD",
        }
    return None


def myer_product_urls():
    sitemap = "https://www.myer.com.au/sitemap.xml"
    xml = ET.fromstring(get(sitemap))
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in xml.findall(".//sm:loc", ns) if e.text]
    sitemap_urls = locs
    if locs and all(u.endswith(".xml") or "sitemap" in u for u in locs):
        sitemap_urls = []
        for child in locs[:30]:
            try:
                child_xml = ET.fromstring(get(child))
                sitemap_urls.extend(e.text for e in child_xml.findall(".//sm:loc", ns) if e.text)
            except Exception:
                continue
    # Restrict to product-looking URLs; don't crawl every category or editorial page.
    return list(dict.fromkeys(
        u for u in sitemap_urls
        if "myer.com.au/p/" in u or re.search(r"/product/|/products/", u, re.I)
    ))


def set_number(text: str) -> str | None:
    # LEGO set numbers are typically 4-6 digits. Prefer explicit set/SKU references.
    match = re.search(r"(?<!\d)(\d{4,6})(?!\d)", text or "")
    return match.group(1) if match else None


def lego_official_rrp(number: str):
    search_url = "https://www.lego.com/en-au/search?q=" + quote(number)
    soup = BeautifulSoup(get(search_url), "html.parser")
    candidates = []
    for a in soup.select('a[href*="/product/"]'):
        href = urljoin("https://www.lego.com", a.get("href", ""))
        if number not in href:
            continue
        candidates.append(href.split("?")[0])
    for url in dict.fromkeys(candidates):
        try:
            data = product_data(url)
        except Exception:
            continue
        if data and number in (data.get("url", "") + " " + data.get("sku", "") + " " + data.get("name", "")):
            return data
    return None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: Path, payload):
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def main():
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    # Workflow has two UTC schedules to cover DST. Only run on the local 08:00 trigger.
    if "--force" not in sys.argv and now.hour != 8:
        print(f"Skipping schedule outside 08:00 Sydney time (now {now.isoformat()}).")
        return

    generated = datetime.now(timezone.utc).isoformat()
    products, deals, errors = [], [], []
    try:
        urls = myer_product_urls()
    except Exception as exc:
        urls = []
        errors.append(f"Myer sitemap unavailable: {type(exc).__name__}: {exc}")

    checked = 0
    for url in urls:
        try:
            myer = product_data(url)
            if not myer:
                continue
            checked += 1
            name = myer.get("name") or ""
            number = set_number(name + " " + myer.get("sku", "") + " " + url)
            record = {
                "set_number": number,
                "name": name,
                "myer_price_aud": myer["price_aud"],
                "myer_url": url,
                "rrp_aud": None,
                "discount_percent": None,
                "verified": False,
                "stock_status": "unknown",
            }
            if number:
                try:
                    lego = lego_official_rrp(number)
                    if lego:
                        rrp = lego["price_aud"]
                        discount = round((rrp - myer["price_aud"]) / rrp * 100, 2) if rrp else None
                        record.update({
                            "rrp_aud": rrp,
                            "discount_percent": discount,
                            "lego_url": lego["url"],
                            "verified": True,
                        })
                        if discount is not None and discount >= THRESHOLD:
                            deals.append(record)
                except Exception as exc:
                    errors.append(f"LEGO AU lookup failed for set {number}: {type(exc).__name__}")
            products.append(record)
            time.sleep(0.35)
        except Exception as exc:
            errors.append(f"Myer product fetch failed: {type(exc).__name__}")
            continue

    coverage_complete = bool(urls) and checked > 0 and not errors
    status = "complete" if coverage_complete else "incomplete"
    deals.sort(key=lambda x: x["discount_percent"] or 0, reverse=True)

    deals_payload = {
        "generated_at": generated, "currency": "AUD",
        "discount_threshold_percent": THRESHOLD,
        "coverage": {"complete": coverage_complete, "status": status,
                     "myer_product_urls_discovered": len(urls),
                     "myer_products_with_prices_checked": checked,
                     "verified_product_count": sum(1 for p in products if p["verified"]),
                     "errors": errors[:50]},
        "deals": deals,
    }
    all_payload = {
        "generated_at": generated, "currency": "AUD",
        "coverage": deals_payload["coverage"], "products": products,
    }
    write_json(API / "lego-deals.json", deals_payload)
    write_json(API / "all-products.json", all_payload)
    write_json(API / "last-updated.json", {
        "status": status, "generated_at": generated,
        "products_checked": checked, "verified_products": sum(1 for p in products if p["verified"]),
        "qualifying_deals": len(deals), "message": "Prices are included only when product data is independently verified.",
        "errors": errors[:50],
    })

    history_path = API / "history.json"
    history = load_json(history_path, {"runs": []})
    history.setdefault("runs", []).append({
        "generated_at": generated, "status": status,
        "products_checked": checked, "verified_products": sum(1 for p in products if p["verified"]),
        "qualifying_deals": len(deals),
    })
    history["runs"] = history["runs"][-365:]
    write_json(history_path, history)
    print(f"Run status={status}; products checked={checked}; deals={len(deals)}")


if __name__ == "__main__":
    main()
