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

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

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


def format_error(exc: Exception) -> str:
    """Return useful diagnostics without dumping a full response body."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.exceptions.Timeout):
        return "ReadTimeout/ConnectTimeout"
    return type(exc).__name__


def browser_get(url: str) -> str:
    """Render a public page with a normal headless browser when requests cannot render it."""
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(locale="en-AU")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Allow client-side catalogue/product data to populate.
            page.wait_for_timeout(2500)
            return page.content()
        finally:
            browser.close()


def get(url: str) -> str:
    """Fetch with requests first; use a standard browser only for transient failures."""
    last_error = None
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = session().get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            response.raise_for_status()
            return response.text
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            # Do not try to work around access-denied or other permanent 4xx responses.
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            last_error = exc

        if attempt < REQUEST_ATTEMPTS - 1:
            delay = 2 ** (attempt + 1)
            print(f"Temporary fetch failure for {url}; retrying in {delay}s "
                  f"({attempt + 1}/{REQUEST_ATTEMPTS - 1}).")
            time.sleep(delay)

    # Browser rendering can help with slow or JS-rendered public pages. It does not
    # attempt to bypass CAPTCHAs, authentication, or access-denied responses.
    try:
        print(f"Trying normal browser rendering for {url} after {format_error(last_error)}.")
        return browser_get(url)
    except Exception as browser_exc:
        raise RuntimeError(
            f"requests failed ({format_error(last_error)}); "
            f"browser fallback failed ({format_error(browser_exc)})"
        ) from browser_exc


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


def parse_product_html(html: str, url: str):
    soup = BeautifulSoup(html, "html.parser")

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
    return None


def extract_product_data(url: str, retailer_key: str | None = None):
    if retailer_key == "bigw" and url in _BIGW_PRODUCT_CACHE:
        return _BIGW_PRODUCT_CACHE[url]
    html = get(url)
    data = parse_product_html(html, url)
    # David Jones may return a JS shell to requests even when the public page
    # itself is available. Render that product page normally.
    if data is None and retailer_key == "davidjones" and sync_playwright is not None:
        data = parse_product_html(browser_get(url), url)
    return data


# BIG W's public catalogue HTML is intermittently blocked in GitHub Actions.
# This independently operated scraper API exposes the same public category data.
_BIGW_PRODUCT_CACHE = {}

def discover_bigw_via_api():
    """Fetch BIG W LEGO listings from the public Big W Scraper API.

    The API documents category, brand and search endpoints. Try these routes in
    order because the service may change or temporarily restrict one route.
    """
    products = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.omkar.cloud",
        "Referer": "https://www.omkar.cloud/tools/big-w-scraper/category-products",
    }
    base = "https://big-w-scraper.omkar.cloud/big-w"
    routes = [
        (
            "category",
            f"{base}/categories/products",
            {"category": "6822101", "brand": "LEGO", "sold_by": "bigw",
             "page": 1, "limit": 48, "state": "NSW"},
        ),
        (
            "brand",
            f"{base}/brands/products",
            {"brand": "LEGO", "category": "6822101", "sold_by": "bigw",
             "page": 1, "limit": 48, "state": "NSW"},
        ),
        (
            "search",
            f"{base}/products/search",
            {"query": "lego", "category": "6822101", "brand": "LEGO",
             "sold_by": "bigw", "page": 1, "limit": 48, "state": "NSW"},
        ),
    ]

    last_error = None
    selected_route = None
    first_payload = None
    for route_name, endpoint, params in routes:
        try:
            response = session().get(
                endpoint, params=params, headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("BIG W API response did not contain a results list")
            selected_route = (route_name, endpoint, params)
            first_payload = payload
            print(f"BIG W API: using {route_name} endpoint.")
            break
        except Exception as exc:
            last_error = exc
            print(f"BIG W API {route_name} endpoint failed: {format_error(exc)}")

    if first_payload is None:
        raise RuntimeError(
            f"all documented BIG W API endpoints failed "
            f"(last error: {format_error(last_error)})"
        ) from last_error

    route_name, endpoint, base_params = selected_route
    total_pages = int(first_payload.get("total_pages") or 1)
    total_pages = min(total_pages, 20)

    for page in range(1, total_pages + 1):
        payload = first_payload if page == 1 else None
        if payload is None:
            params = dict(base_params)
            params["page"] = page
            response = session().get(
                endpoint, params=params, headers=headers,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
            response.raise_for_status()
            payload = response.json()

        results = payload.get("results") or []
        for item in results:
            if str(item.get("brand", "")).strip().lower() != "lego":
                continue
            if str(item.get("sold_by", "")).strip().lower() != "big w":
                continue

            pricing = item.get("pricing") or {}
            if str(pricing.get("currency") or "AUD").upper() != "AUD":
                continue
            try:
                amount = float(pricing.get("amount"))
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue

            product_url = item.get("link")
            if not product_url:
                continue
            product_url = product_url.split("?")[0].rstrip("/")
            name = str(item.get("name") or "").strip()
            article_id = str(item.get("id") or "")
            _BIGW_PRODUCT_CACHE[product_url] = {
                "name": name,
                "url": product_url,
                "sku": article_id,
                "price_aud": amount,
                "currency": "AUD",
                "stock_status": (
                    "in_stock" if item.get("is_in_stock") else "out_of_stock"
                ),
                "data_source": "BIG W listing via Big W Scraper API",
            }
            products.append(product_url)

        if page < total_pages:
            time.sleep(0.2)

    unique = list(dict.fromkeys(products))
    print(f"BIG W API: fetched {len(unique)} BIG W LEGO listings across {total_pages} pages.")
    return unique

def discover_product_urls(retailer_key: str):
    api_fallback_error = None
    if retailer_key == "bigw":
        try:
            api_urls = discover_bigw_via_api()
            if api_urls:
                print(f"BIG W API returned {len(api_urls)} LEGO listings.")
                return api_urls[:MAX_PRODUCTS_PER_RETAILER], []
            return [], ["BIG W data API returned no LEGO listings."]
        except Exception as exc:
            api_fallback_error = f"BIG W data API failed: {format_error(exc)}"
            print(api_fallback_error)
            # Fall back to the retailer's own category page if the API is unavailable.
    config = RETAILERS[retailer_key]
    base = config["category"]
    urls, seen_pages, errors = [], set(), ([api_fallback_error] if api_fallback_error else [])
    page_limit = min(MAX_PAGES, config.get("max_pages", MAX_PAGES))
    for page in range(1, page_limit + 1):
        page_url = config["page_url"](base, page)
        try:
            soup = BeautifulSoup(get(page_url), "html.parser")
        except Exception as exc:
            errors.append(
                f"{config['name']} catalogue page {page}: {format_error(exc)} ({page_url})"
            )
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
        if not unique and page == 1 and sync_playwright is not None:
            # Some catalogues render their product cards only after JavaScript runs.
            try:
                browser_html = browser_get(page_url)
                browser_soup = BeautifulSoup(browser_html, "html.parser")
                browser_found = []
                for anchor in browser_soup.select("a[href]"):
                    href = urljoin(page_url, anchor.get("href", "").strip())
                    parsed = urlparse(href)
                    host = parsed.netloc.lower().removeprefix("www.")
                    path = parsed.path.lower()
                    if host.endswith(config["host"]) and any(
                        part in path for part in config["product_path"]
                    ):
                        label = " ".join((anchor.get_text(" ", strip=True), path)).lower()
                        if "lego" in label or re.search(r"(?<!\\d)\\d{4,6}(?!\\d)", label):
                            browser_found.append(href.split("?")[0].rstrip("/"))
                unique = list(dict.fromkeys(browser_found))
            except Exception as exc:
                errors.append(
                    f"{config['name']} browser catalogue fallback: {format_error(exc)} ({page_url})"
                )
        if not unique:
            if page == 1:
                errors.append(
                    f"{config['name']} catalogue returned no product links after normal and browser fetch ({page_url})."
                )
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



def check_listing(retailer_key: str, url: str):
    """Read one retailer listing and normalize it to the API product schema."""
    data = extract_product_data(url, retailer_key)
    if not data:
        return None

    name = str(data.get("name") or "").strip()
    sku = str(data.get("sku") or "").strip()
    number = set_number(" ".join((sku, name, url)))
    if not number:
        return None

    price = data.get("price_aud")
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    config = RETAILERS[retailer_key]
    return {
        "retailer": retailer_key,
        "retailer_name": config["name"],
        "set_number": number,
        "name": name or f"LEGO set {number}",
        "price_aud": round(price, 2),
        "product_url": data.get("url") or url,
        "rrp_aud": None,
        "discount_percent": None,
        "rrp_url": None,
        "rrp_source": None,
        "verified": False,
        "stock_status": data.get("stock_status", "unknown"),
    }


def write_json(path: Path, payload):
    """Write API JSON deterministically and create the destination folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path, default):
    """Load an existing JSON file, returning a safe default if unavailable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


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
                tasks.append((key, url, pool.submit(check_listing, key, url)))
        for key, product_url, future in tasks:
            try:
                record = future.result()
                if record:
                    all_products.append(record)
                    retailer_coverage[key]["products_with_prices_checked"] += 1
            except Exception as exc:
                msg = (
                    f"{RETAILERS[key]['name']} product fetch failed: "
                    f"{format_error(exc)} ({product_url})"
                )
                retailer_coverage[key]["errors"].append(msg)
                errors.append(msg)

    # Bricks Megastore is the sole reference-price source.
    # Match retailer listings by LEGO set number; never fetch LEGO.com or
    # substitute another retailer's price as the reference.
    rrp_cache = {}
    for product in all_products:
        if product["retailer"] != "bricksmegastore" or not product["set_number"]:
            continue
        rrp_cache[product["set_number"]] = {
            "price_aud": product["price_aud"],
            "url": product["product_url"],
            "name": product["name"],
            "sku": product["set_number"],
            "source": "Bricks Megastore listing price",
        }

    for product in all_products:
        number = product["set_number"]
        lego = rrp_cache.get(number) if number else None
        if lego:
            rrp = lego["price_aud"]
            discount = round((rrp - product["price_aud"]) / rrp * 100, 2) if rrp else None
            product.update({
                "rrp_aud": rrp,
                "discount_percent": discount,
                "rrp_url": lego["url"],
                "rrp_source": lego.get("source", "Bricks Megastore listing price"),
                "verified": True,
            })
            key = product["retailer"]
            retailer_coverage[key]["verified_products"] += 1
            # Bricks Megastore is the reference source and is excluded from deal alerts.
            if key != "bricksmegastore" and discount is not None and discount >= THRESHOLD:
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
