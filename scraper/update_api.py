"""Daily multi-retailer LEGO deals collector for Australia.

A product is a qualifying deal only when the retailer price and the matching
Australian LEGO RRP can both be read independently. Catalogue discovery is
best-effort because retailer page structures and anti-bot controls can change.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "api"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 45
REQUEST_ATTEMPTS = 3
THRESHOLD = 20.0
MAX_PAGES = 20
MAX_PRODUCTS_PER_RETAILER = 500
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LegoDealsMonitor/2.0; +https://github.com/xuwangdaalex/lego-deals)",
    "Accept-Language": "en-AU,en;q=0.9",
}

RETAILERS = {
    "legoau": {
        "name": "LEGO AU (Official)",
        "category": "https://www.lego.com/en-au/categories/all-sets",
        "host": "lego.com",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/product/",),
        # Start with the first server-rendered catalogue batch. The category uses
        # a Load More interface; pagination is validated before expanding it.
        "max_pages": 1,
    },
    "myer": {
        "name": "Myer",
        "category": "https://www.myer.com.au/c/all-toys/brand-lego",
        "host": "myer.com.au",
        "page_url": lambda base, n: base if n == 1 else f"{base}?pageNumber={n}",
        "product_path": ("/p/",),
    },
    "bigw": {
        "name": "BIG W",
        "category": "https://www.bigw.com.au/toys/lego/c/6822101",
        "host": "bigw.com.au",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/product/",),
        # BIG W exposes a large first batch and a separate "Load more" control;
        # avoid guessing unsupported ?page=N URLs which can trigger slow requests.
        "max_pages": 1,
    },
    "target": {
        "name": "Target",
        "category": "https://www.target.com.au/c/toys/lego/W152974",
        "host": "target.com.au",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/p/",),
    },
    "kmart": {
        "name": "Kmart",
        "category": "https://www.kmart.com.au/category/toys/shop-all-lego/",
        "host": "kmart.com.au",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/product/",),
    },
    "bricksmegastore": {
        "name": "Bricks Megastore",
        "category": "https://lego.bricksmegastore.com/collections/all-australian-locations",
        "host": "bricksmegastore.com",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/products/",),
    },
    "davidjones": {
        "name": "David Jones",
        "category": "https://www.davidjones.com/brand/lego",
        "host": "davidjones.com",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/product/", "/products/"),
    },
    "ebgames": {
        "name": "EB Games",
        "category": "https://www.ebgames.com.au/featured/lego",
        "host": "ebgames.com.au",
        "page_url": lambda base, n: base if n == 1 else f"{base}?page={n}",
        "product_path": ("/product/", "/products/"),
    },
}

_local = __import__("threading").local()


def session():
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    return _local.session


def get(url: str) -> str:
    """Fetch a page with retries for transient retailer timeouts and server errors."""
    last_error = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = session().get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if response.status_code == 429 or response.status_code >= 500:
                response.raise_for_status()
            response.raise_for_status()
            return response.text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
        except requests.exceptions.HTTPError as exc:
            # Retry rate limits and temporary upstream failures, but not ordinary 4xx errors.
            status = exc.response.status_code if exc.response is not None else None
            if status != 429 and (status is None or status < 500):
                raise
            last_error = exc

        if attempt < REQUEST_ATTEMPTS - 1:
            delay = 2 ** (attempt + 1)
            print(f"Temporary fetch failure for {url}; retrying in {delay}s "
                  f"({attempt + 1}/{REQUEST_ATTEMPTS - 1}).")
            time.sleep(delay)

    raise last_error


def jsonld_objects(soup: BeautifulSoup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            yield item
            for key in ("@graph", "itemListElement", "mainEntity", "offers"):
                value = item.get(key)
                if isinstance(value, list):
                    stack.extend(x for x in value if isinstance(x, dict))
                elif isinstance(value, dict):
                    stack.append(value)


def extract_product_data(url: str):
    soup = BeautifulSoup(get(url), "html.parser")

    # Prefer structured Product data when the retailer provides it.
    for item in jsonld_objects(soup):
        typ = item.get("@type", [])
        types = typ if isinstance(typ, list) else [typ]
        if "Product" not in types:
            continue
        offers = item.get("offers", {})
        if isinstance(offers, list):
            offers = next((x for x in offers if isinstance(x, dict)), {})
        if not isinstance(offers, dict):
            continue
        price = offers.get("price") or offers.get("lowPrice")
        currency = offers.get("priceCurrency") or "AUD"
        if price is None or currency != "AUD":
            continue
        try:
            amount = float(str(price).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            continue
        return {
            "name": item.get("name") or "",
            "url": item.get("url") or url,
            "sku": str(item.get("sku") or item.get("mpn") or ""),
            "price_aud": amount,
            "currency": currency,
        }

    # BIG W product pages may omit Product JSON-LD. Fall back to standard
    # product/SEO metadata rather than silently dropping every such listing.
    def meta_value(*selectors):
        for selector in selectors:
            tag = soup.select_one(selector)
            if tag:
                value = tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True)
                if value:
                    return str(value).strip()
        return ""

    name = meta_value(
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
        'meta[itemprop="name"]',
    )
    raw_price = meta_value(
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
        'meta[name="price"]',
    )
    currency = meta_value(
        'meta[property="product:price:currency"]',
        'meta[property="og:price:currency"]',
        'meta[itemprop="priceCurrency"]',
    ) or "AUD"

    if raw_price and currency.upper() == "AUD":
        match = re.search(r"\d+(?:[.,]\d{1,2})?", raw_price.replace(",", ""))
        if match:
            try:
                amount = float(match.group(0))
                if amount > 0:
                    sku = url.rstrip("/").split("/")[-1]
                    return {
                        "name": name,
                        "url": url,
                        "sku": sku,
                        "price_aud": amount,
                        "currency": "AUD",
                    }
            except ValueError:
                pass

    return None


def discover_product_urls(retailer_key: str):
    config = RETAILERS[retailer_key]
    base = config["category"]
    urls, seen_pages, errors = [], set(), []
    page_limit = min(MAX_PAGES, config.get("max_pages", MAX_PAGES))
    for page in range(1, page_limit + 1):
        page_url = config["page_url"](base, page)
        try:
            soup = BeautifulSoup(get(page_url), "html.parser")
        except Exception as exc:
            errors.append(f"{config['name']} catalogue page {page}: {type(exc).__name__}")
            break

        found = []
        for anchor in soup.select("a[href]"):
            href = urljoin(page_url, anchor.get("href", "").strip())
            parsed = urlparse(href)
            host = parsed.netloc.lower().removeprefix("www.")
            path = parsed.path.lower()
            if not host.endswith(config["host"]):
                continue
            if not any(part in path for part in config["product_path"]):
                continue
            label = " ".join((anchor.get_text(" ", strip=True), path)).lower()
            if "lego" not in label and not re.search(r"(?<!\d)\d{4,6}(?!\d)", label):
                continue
            found.append(href.split("?")[0].rstrip("/"))

        unique = list(dict.fromkeys(found))
        if not unique:
            if page == 1:
                errors.append(f"{config['name']} catalogue returned no product links (possibly JS-rendered or blocked).")
            break
        signature = tuple(unique)
        if signature in seen_pages:
            break
        seen_pages.add(signature)
        urls.extend(unique)
        if len(urls) >= MAX_PRODUCTS_PER_RETAILER:
            urls = urls[:MAX_PRODUCTS_PER_RETAILER]
            break
        if len(unique) < 5:
            break
        time.sleep(0.15)

    return list(dict.fromkeys(urls)), errors


def set_number(text: str) -> str | None:
    match = re.search(r"(?<!\d)(\d{4,6})(?!\d)", text or "")
    return match.group(1) if match else None


def lego_official_rrp(number: str):
    search_url = "https://www.lego.com/en-au/search?q=" + quote(number)
    soup = BeautifulSoup(get(search_url), "html.parser")
    candidates = []
    for anchor in soup.select('a[href*="/product/"]'):
        href = urljoin("https://www.lego.com", anchor.get("href", ""))
        if number in href:
            candidates.append(href.split("?")[0])
    for url in dict.fromkeys(candidates):
        try:
            data = extract_product_data(url)
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


def check_listing(retailer_key: str, url: str):
    config = RETAILERS[retailer_key]
    data = extract_product_data(url)
    if not data:
        return None
    name = data.get("name") or ""
    number = set_number(name + " " + data.get("sku", "") + " " + url)
    return {
        "retailer": retailer_key,
        "retailer_name": config["name"],
        "set_number": number,
        "name": name,
        "price_aud": data["price_aud"],
        "product_url": url,
        "rrp_aud": None,
        "discount_percent": None,
        "verified": False,
        "stock_status": "unknown",
    }


def main():
    now = datetime.now(ZoneInfo("Australia/Sydney"))
    if "--force" not in sys.argv and now.hour != 8:
        print(f"Skipping schedule outside 08:00 Sydney time (now {now.isoformat()}).")
        return

    generated = datetime.now(timezone.utc).isoformat()
    all_products, deals, errors = [], [], []
    retailer_coverage = {}
    discovered = {}

    for key in RETAILERS:
        urls, discovery_errors = discover_product_urls(key)
        discovered[key] = urls
        errors.extend(discovery_errors)
        retailer_coverage[key] = {
            "name": RETAILERS[key]["name"],
            "product_urls_discovered": len(urls),
            "products_with_prices_checked": 0,
            "verified_products": 0,
            "qualifying_deals": 0,
            "status": "incomplete",
            "errors": discovery_errors,
        }
        print(f"{RETAILERS[key]['name']}: discovered {len(urls)} product URLs")

    # Fetch product pages concurrently to keep the daily job within GitHub Actions limits.
    tasks = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for key, urls in discovered.items():
            for url in urls:
                tasks.append((key, pool.submit(check_listing, key, url)))
        for key, future in tasks:
            try:
                record = future.result()
                if record:
                    all_products.append(record)
                    retailer_coverage[key]["products_with_prices_checked"] += 1
            except Exception as exc:
                msg = f"{RETAILERS[key]['name']} product fetch failed: {type(exc).__name__}"
                retailer_coverage[key]["errors"].append(msg)
                errors.append(msg)

    # Build the Australian official RRP map from LEGO AU catalogue prices first.
    # This avoids a separate search request for every set already in the catalogue.
    rrp_cache = {}
    for product in all_products:
        if product["retailer"] == "legoau" and product["set_number"]:
            rrp_cache[product["set_number"]] = {
                "price_aud": product["price_aud"],
                "url": product["product_url"],
                "name": product["name"],
                "sku": product["set_number"],
            }

    # Fall back to LEGO AU's set search for retailer sets not in the catalogue batch.
    numbers = sorted({
        p["set_number"] for p in all_products
        if p["set_number"] and p["set_number"] not in rrp_cache
    })
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(lego_official_rrp, number): number for number in numbers}
        for future in as_completed(futures):
            number = futures[future]
            try:
                rrp_cache[number] = future.result()
                if rrp_cache[number] is None:
                    errors.append(f"LEGO AU RRP could not be verified for set {number}.")
            except Exception as exc:
                rrp_cache[number] = None
                errors.append(f"LEGO AU lookup failed for set {number}: {type(exc).__name__}")

    for product in all_products:
        number = product["set_number"]
        lego = rrp_cache.get(number) if number else None
        if lego:
            rrp = lego["price_aud"]
            discount = round((rrp - product["price_aud"]) / rrp * 100, 2) if rrp else None
            product.update({
                "rrp_aud": rrp,
                "discount_percent": discount,
                "lego_url": lego["url"],
                "verified": True,
            })
            key = product["retailer"]
            retailer_coverage[key]["verified_products"] += 1
            # LEGO AU is the official price reference, so its own listings are
            # included in all-products but excluded from retailer deal alerts.
            if key != "legoau" and discount is not None and discount >= THRESHOLD:
                deals.append(product)
                retailer_coverage[key]["qualifying_deals"] += 1

    for key, coverage in retailer_coverage.items():
        # Explicitly flag a retailer when discovery or product-page price extraction
        # returns no usable data, so API consumers can detect a silent feed failure.
        coverage["no_data"] = (
            coverage["product_urls_discovered"] == 0
            or coverage["products_with_prices_checked"] == 0
        )
        coverage["status"] = (
            "complete" if not coverage["no_data"] and not coverage["errors"]
            else "incomplete"
        )
        coverage["errors"] = coverage["errors"][:20]

    stores_with_no_data = [
        {"retailer": key, "name": value["name"], "reason": (
            "No product URLs discovered" if value["product_urls_discovered"] == 0
            else "No product prices could be read" if value["products_with_prices_checked"] == 0
            else "Retailer returned no usable data"
        )}
        for key, value in retailer_coverage.items() if value["no_data"]
    ]

    # A run is complete only if every retailer completed discovery and product checks.
    coverage_complete = all(x["status"] == "complete" for x in retailer_coverage.values()) and not errors
    status = "complete" if coverage_complete else "incomplete"
    deals.sort(key=lambda x: x["discount_percent"] or 0, reverse=True)
    all_products.sort(key=lambda x: (x["retailer_name"], x["set_number"] or "", x["name"]))

    coverage = {
        "complete": coverage_complete,
        "status": status,
        "retailers": retailer_coverage,
        "stores_with_no_data": stores_with_no_data,
        "product_count": len(all_products),
        "verified_product_count": sum(1 for p in all_products if p["verified"]),
        "errors": errors[:100],
    }
    write_json(API / "lego-deals.json", {
        "generated_at": generated, "currency": "AUD",
        "discount_threshold_percent": THRESHOLD,
        "coverage": coverage, "stores_with_no_data": stores_with_no_data, "deals": deals,
    })
    write_json(API / "all-products.json", {
        "generated_at": generated, "currency": "AUD",
        "coverage": coverage, "stores_with_no_data": stores_with_no_data, "products": all_products,
    })
    write_json(API / "last-updated.json", {
        "status": status, "generated_at": generated,
        "products_checked": len(all_products),
        "verified_products": coverage["verified_product_count"],
        "qualifying_deals": len(deals),
        "retailers": retailer_coverage,
        "stores_with_no_data": stores_with_no_data,
        "message": "Only independently verified retailer prices and matching Australian LEGO RRPs qualify as deals.",
        "errors": errors[:100],
    })

    history_path = API / "history.json"
    history = load_json(history_path, {"runs": []})
    history.setdefault("runs", []).append({
        "generated_at": generated, "status": status,
        "products_checked": len(all_products),
        "verified_products": coverage["verified_product_count"],
        "qualifying_deals": len(deals),
    })
    history["runs"] = history["runs"][-365:]
    write_json(history_path, history)
    print(f"Run status={status}; products checked={len(all_products)}; verified={coverage['verified_product_count']}; deals={len(deals)}")


if __name__ == "__main__":
    main()
