# LEGO Deals API

A daily-updated JSON feed for LEGO products from seven Australian retailers:

- Myer
- BIG W
- Target
- Kmart
- Bricks Megastore (LEGO Certified Stores)
- David Jones
- EB Games

Amazon is not included.

## BIG W catalogue page

A separate browser page for BIG W product data is available at:

https://xuwangdaalex.github.io/lego-deals/bigw.html

The page reads BIG W entries from `api/all-products.json` and links to BIG W's live catalogue. It does not directly scrape BIG W in the browser; the GitHub Actions scraper must successfully collect BIG W data for products to appear.

## JSON endpoints

After GitHub Pages has been enabled and the first deployment succeeds:

- **Qualifying deals (20%+ off verified Australian LEGO RRP):** https://xuwangdaalex.github.io/lego-deals/api/lego-deals.json
- **All products checked across retailers:** https://xuwangdaalex.github.io/lego-deals/api/all-products.json
- **Latest run status and retailer coverage:** https://xuwangdaalex.github.io/lego-deals/api/last-updated.json
- **Run history:** https://xuwangdaalex.github.io/lego-deals/api/history.json

Raw GitHub file URLs are also available, for example:
`https://raw.githubusercontent.com/xuwangdaalex/lego-deals/main/api/lego-deals.json`

## Discount calculation and verification

`(Australian LEGO RRP - retailer price) / Australian LEGO RRP * 100`

The threshold is inclusive (20% or more). A product is included in the deals feed only when the retailer product page provides a price and the scraper independently matches the LEGO set number to Australian RRP data from LEGO's official product pages. A retailer's crossed-out or previous price is not treated as RRP.

## Coverage and limitations

The scraper discovers product pages from each retailer's public LEGO catalogue and reads product structured data (JSON-LD). Retailer sites can change their markup, use JavaScript rendering, or block automated requests. The API therefore reports discovery, price-check, verification, and error counts for each retailer. A run marked `incomplete` must not be interpreted as proof that no deals exist.

The scraper is best-effort and does not guarantee every listing or live store stock. Availability is reported as `unknown` unless a retailer exposes reliable stock data in product structured data.

## Daily automation

GitHub Actions checks at the two UTC hours that cover 08:00 Sydney time across daylight-saving changes. The script runs only when Sydney local time is 08:00. You can also run it manually from the **Actions** tab.

## One-time setup

1. Open repository **Settings → Pages**.
2. Under **Build and deployment → Source**, select **GitHub Actions**.
3. Open **Actions** and run **Update LEGO deals API** once using **Run workflow**.

GitHub Pages sites are publicly accessible. Make sure the repository and its contents are appropriate to publish.

## Refresh BIG W locally on a Mac

BIG W may be reachable in a normal browser on your Mac even when GitHub Actions cannot connect. The local scraper uses Playwright/Chromium and updates only BIG W records, preserving other retailer products already in the JSON files.

1. Pull the latest repository changes:
   ```bash
   git pull
   ```
2. Create a separate Python environment for Monterey. This does not change your system Python, Node.js, or existing Playwright installation:
   ```bash
   python3 -m venv .venv-monterey
   source .venv-monterey/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -r requirements-monterey.txt
   ```
   The Monterey requirements pin Playwright to an older release so it does not use the newer bundled Node runtime that failed on your Mac. This is a compatibility workaround, not a guarantee; we'll test it before running the full scraper.
3. Install the matching Chromium browser:
   ```bash
   python -m playwright install chromium
   ```
4. From the repository root, run:
   ```bash
   python scraper/update_bigw_local.py
   ```
   Chromium opens and visits the BIG W LEGO catalogue pages. To run without showing the browser window:
   ```bash
   python scraper/update_bigw_local.py --headless
   ```
5. Review the updated files in `api/`, then publish them:
   ```bash
   git add api/
   git commit -m "Update BIG W LEGO catalogue"
   git push
   ```

To use this environment again later, run `source .venv-monterey/bin/activate` from the repository root before running the scraper.

The local scraper reads product cards from BIG W's public catalogue. It stops if it cannot extract any products and does not overwrite API files in that case. BIG W discounts are verified against matching Bricks Megastore listing prices, which are a reference benchmark rather than official LEGO RRP.

