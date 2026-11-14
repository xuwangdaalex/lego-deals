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
