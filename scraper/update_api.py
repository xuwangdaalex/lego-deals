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
        # BIG W's category supports ?page=N pagination (48 products per page).
        "max_pages": 11,
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
    """Return actionable diagnostics while keeping response bodies out of logs."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.exceptions.Timeout):
        return "ReadTimeout/ConnectTimeout"
    if isinstance(exc, RuntimeError):
        detail = str(exc).strip()
        return f"RuntimeError: {detail}" if detail else "RuntimeError"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"{type(exc).__name__}: {str(exc)[:180]}"
    return f"{type(exc).__name__}: {str(exc)[:180]}" if str(exc) else type(exc).__name__


def browser_get(url: str, *, disable_http2: bool = False) -> str:
    """Render a public page with Chromium without waiting for every network request.

    BIG W can occasionally terminate HTTP/2 sessions from hosted runners. For that
    retailer only, callers may retry using Chromium with HTTP/2 disabled; this is
    a transport-compatibility retry, not an attempt to bypass access controls.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright is not installed")
    with sync_playwright() as playwright:
        launch_options = {"headless": True}
        if disable_http2:
            launch_options["args"] = ["--disable-http2"]
        browser = playwright.chromium.launch(**launch_options)
        try:
            page = browser.new_page(
                locale="en-AU",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            response = page.goto(url, wait_until="commit", timeout=60000)
            status = response.status if response is not None else None
            if status is not None and status in (401, 403):
                raise RuntimeError(f"browser received HTTP {status}")
            # Some retail pages keep analytics or recommendation requests open.
            # Wait briefly for the main document and client-rendered product cards.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            html = page.content()
            if len(html) < 1000:
                raise RuntimeError(
                    f"browser returned an unexpectedly short page ({len(html)} bytes)"
                )
            return html
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
    print(f"Trying normal browser rendering for {url} after {format_error(last_error)}.")
    try:
        return browser_get(url)
    except Exception as browser_exc:
        # BIG W has returned ERR_HTTP2_PROTOCOL_ERROR from GitHub Actions.
        # Retry the same public page over HTTP/2-disabled Chromium mode.
        if "bigw.com.au" in url.lower() and "ERR_HTTP2_PROTOCOL_ERROR" in str(browser_exc):
            try:
                print(f"Retrying BIG W page with Chromium HTTP/2 disabled: {url}")
                return browser_get(url, disable_http2=True)
            except Exception as retry_exc:
                raise RuntimeError(
                    f"requests failed ({format_error(last_error)}); "
                    f"browser fallback failed ({format_error(browser_exc)}); "
                    f"HTTP/2-disabled retry failed ({format_error(retry_exc)})"
                ) from retry_exc
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


# BIG W catalogue product data is cached from the public category HTML.
# The third-party Big W Scraper API is intentionally not used.
_BIGW_PRODUCT_CACHE = {}


def _bigw_card_price(card):
    """Extract the displayed BIG W price from a product-card fragment."""
    # Prefer explicit price metadata or price-labelled elements.
    selectors = (
        '[itemprop="price"]',
        '[data-testid*="price"]',
        '[class*="price"]',
        '[aria-label*="$"]',
    )
    for selector in selectors:
        for node in card.select(selector):
            raw = (
                node.get("content")
                or node.get("value")
                or node.get("aria-label")
                or node.get_text(" ", strip=True)
            )
            match = re.search(r"\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)", str(raw))
            if match:
                try:
                    amount = float(match.group(1).replace(",", ""))
                    if amount > 0:
                        return amount
                except ValueError:
                    pass

    # BIG W product cards normally place the current price before any
    # "Originally $X" or "Save $X" promotional text.
    text = card.get_text(" ", strip=True)
    match = re.search(r"\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)", text)
    if match:
        try:
            amount = float(match.group(1).replace(",", ""))
            return amount if amount > 0 else None
        except ValueError:
            return None
    return None


def _bigw_card_name(anchor, card):
    candidates = [
        anchor.get("aria-label", ""),
        anchor.get("title", ""),
        " ".join(img.get("alt", "") for img in anchor.select("img")),
        anchor.get_text(" ", strip=True),
    ]
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and "lego" in candidate.lower():
            # Remove common trailing price/promotion text from the card label.
            candidate = re.split(r"\s+\$\s*\d", candidate, maxsplit=1)[0].strip()
            return candidate

    text = card.get_text("\n", strip=True)
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if "lego" in line.lower() and len(line) > 8:
            return re.split(r"\s+\$\s*\d", line, maxsplit=1)[0].strip()
    return ""


def _bigw_card_fragment(anchor):
    """Return the nearest useful product-card ancestor without walking the page."""
    node = anchor
    best = anchor
    for _ in range(6):
        if node is None or getattr(node, "name", None) in ("body", "html"):
            break
        text = node.get_text(" ", strip=True)
        if text and ("$" in text or node.select_one('[itemprop="price"]')):
            best = node
            # Prefer a compact card rather than a large grid/list container.
            if len(text) < 1400:
                return node
        node = node.parent
    return best


def discover_bigw_from_catalogue():
    """Read BIG W's public LEGO category HTML and follow its page=N pagination."""
    config = RETAILERS["bigw"]
    urls = []
    errors = []
    seen_pages = set()
    page_limit = min(config.get("max_pages", 11), 20)

    for page in range(1, page_limit + 1):
        page_url = config["category"] if page == 1 else f'{config["category"]}?page={page}'
        try:
            html = get_bigw_catalogue_page(page_url)
        except Exception as exc:
            errors.append(
                f'BIG W catalogue page {page}: {format_error(exc)} ({page_url})'
            )
            # A single page can fail at BIG W's CDN while later pages remain
            # available. Keep checking the remaining page numbers rather than
            # abandoning the whole catalogue at the first transient failure.
            continue

        soup = BeautifulSoup(html, "html.parser")
        page_urls = []
        for anchor in soup.select("a[href]"):
            href = urljoin(page_url, anchor.get("href", "").strip())
            parsed = urlparse(href)
            host = parsed.netloc.lower().removeprefix("www.")
            path = parsed.path.lower()
            if not host.endswith(config["host"]) or "/product/" not in path:
                continue

            product_url = href.split("?")[0].rstrip("/")
            card = _bigw_card_fragment(anchor)
            card_text = card.get_text(" ", strip=True)
            name = _bigw_card_name(anchor, card)
            combined = " ".join((name, card_text, path))
            number = set_number(combined)
            if "lego" not in combined.lower() and not number:
                continue

            price = _bigw_card_price(card)
            if price is not None:
                _BIGW_PRODUCT_CACHE[product_url] = {
                    "name": name or f"LEGO set {number or ''}".strip(),
                    "url": product_url,
                    "sku": number or "",
                    "price_aud": price,
                    "currency": "AUD",
                    "stock_status": (
                        "out_of_stock"
                        if re.search(r"out of stock|unavailable", card_text, re.I)
                        else "unknown"
                    ),
                    "data_source": "BIG W public catalogue HTML",
                }
            page_urls.append(product_url)

        page_urls = list(dict.fromkeys(page_urls))
        if not page_urls:
            errors.append(
                f"BIG W catalogue page {page} contained no LEGO product links ({page_url})."
            )
            # Do not assume an empty/partially rendered page means later pages
            # are empty too; continue to the next page number.
            continue

        signature = tuple(page_urls)
        if signature in seen_pages:
            break
        seen_pages.add(signature)
        urls.extend(page_urls)
        print(
            f"BIG W catalogue page {page}: found {len(page_urls)} products "
            f"({len(urls)} unique URLs total; {len(_BIGW_PRODUCT_CACHE)} prices read from cards)."
        )

        if len(urls) >= MAX_PRODUCTS_PER_RETAILER:
            urls = urls[:MAX_PRODUCTS_PER_RETAILER]
            break
        # Do not stop just because a page yielded only a few cards. BIG W
        # sometimes serves partially rendered catalogue HTML; later pages may
        # still contain valid product cards.
        time.sleep(0.25)

    unique = list(dict.fromkeys(urls))[:MAX_PRODUCTS_PER_RETAILER]
    print(
        f"BIG W catalogue: discovered {len(unique)} LEGO URLs; "
        f"cached {len(_BIGW_PRODUCT_CACHE)} catalogue prices."
    )
    return unique, errors


def get_bigw_catalogue_page(url: str) -> str:
    """Fetch BIG W's public catalogue HTML using its category and canonical URL forms.

    The canonical /c/<category-id> route is also published by BIG W and can
    behave differently from the longer /toys/... route at the CDN edge.
    """
    parsed = urlparse(url)
    category_id = parsed.path.rstrip("/").split("/")[-1]
    candidates = [url]
    if category_id.isdigit():
        canonical = f"https://www.bigw.com.au/c/{category_id}"
        if parsed.query:
            canonical = f"{canonical}?{parsed.query}"
        if canonical not in candidates:
            candidates.append(canonical)

    errors = []
    for candidate in candidates:
        last_error = None
        # Keep each network attempt bounded so a dead route does not consume
        # most of the scheduled workflow's runtime.
        for attempt in range(2):
            try:
                response = session().get(
                    candidate,
                    timeout=(8, 25),
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://www.bigw.com.au/",
                    },
                )
                response.raise_for_status()
                if len(response.text) < 1000:
                    raise RuntimeError(
                        f"BIG W returned an unexpectedly short catalogue page ({len(response.text)} bytes)"
                    )
                return response.text
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status is not None and 400 <= status < 500 and status != 429:
                    errors.append(f"{candidate}: {format_error(exc)}")
                    break
                last_error = exc
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_error = exc
            if attempt == 0:
                time.sleep(2)

        if last_error is not None:
            errors.append(f"{candidate}: {format_error(last_error)}")

    if sync_playwright is None:
        raise RuntimeError(
            f"direct catalogue requests failed ({'; '.join(errors)}); Playwright unavailable"
        )

    # Try Chromium over HTTP/2-disabled transport first; do not use third-party
    # scraping services or attempt to bypass authentication/access controls.
    browser_errors = []
    for candidate in candidates:
        try:
            return browser_get(candidate, disable_http2=True)
        except Exception as exc:
            browser_errors.append(f"{candidate}: {format_error(exc)}")

    raise RuntimeError(
        f"direct catalogue requests failed ({'; '.join(errors)}); "
        f"browser retries failed ({'; '.join(browser_errors)})"
    )

def discover_product_urls(retailer_key: str):
    if retailer_key == "bigw":
        return discover_bigw_from_catalogue()
    config = RETAILERS[retailer_key]
    base = config["category"]
    urls, seen_pages, errors = [], set(), []
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
