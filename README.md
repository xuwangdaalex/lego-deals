# LEGO Deals API

A daily-updated JSON feed for LEGO products listed by Myer Australia with a verified discount of at least 20% against the Australian LEGO RRP.

## JSON endpoints

After GitHub Pages has been enabled and the first deployment succeeds:

- **Qualifying deals:** https://xuwangdaalex.github.io/lego-deals/api/lego-deals.json
- **All products checked:** https://xuwangdaalex.github.io/lego-deals/api/all-products.json
- **Latest run status:** https://xuwangdaalex.github.io/lego-deals/api/last-updated.json
- **Run history:** https://xuwangdaalex.github.io/lego-deals/api/history.json

Raw GitHub file URLs are also available for a public repository, for example:
`https://raw.githubusercontent.com/xuwangdaalex/lego-deals/main/api/lego-deals.json`

## Discount calculation and verification

`(rrp_aud - myer_price_aud) / rrp_aud * 100`

The threshold is inclusive (20% or more). The scraper only marks a product as verified when it can read Myer product pricing and a matching set number's Australian RRP from LEGO's official product data. Myer's crossed-out or previous price is not used as RRP.

Scraping is best-effort and site markup/sitemaps can change. The API reports coverage as `complete` or `incomplete`; an incomplete run must not be interpreted as proof that no deals exist. Products with unverified RRP are not included in the deals feed.

## Daily automation

GitHub Actions checks at the two UTC hours that cover 08:00 Sydney time across daylight-saving changes. The script runs only when Sydney local time is 08:00. You can also run it manually from the **Actions** tab.

## One-time setup

1. Open repository **Settings → Pages**.
2. Under **Build and deployment → Source**, select **GitHub Actions**.
3. Open **Actions** and run **Update LEGO deals API** once using **Run workflow**.

GitHub Pages sites are publicly accessible. If you want a public API endpoint, make sure the repository and its contents are appropriate to publish.
