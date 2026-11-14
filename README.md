# LEGO Deals API

A daily-updated JSON feed for LEGO products at Myer Australia with a verified discount of at least 20% against the Australian LEGO RRP.

## API endpoints

Once GitHub Pages is enabled for this repository:

- `https://xuwangdaalex.github.io/lego-deals/api/lego-deals.json` — qualifying deals
- `https://xuwangdaalex.github.io/lego-deals/api/all-products.json` — products checked by the scraper
- `https://xuwangdaalex.github.io/lego-deals/api/last-updated.json` — latest run status

## Important data-quality rule

A product is included only when its Myer price and Australian LEGO RRP can both be verified for the same set. The discount is calculated as:

`(rrp_aud - myer_price_aud) / rrp_aud * 100`

The threshold is inclusive (20% or more). A failed or incomplete scrape must be reported as incomplete; it must not invent prices or claim full catalogue coverage.

## Automation

The GitHub Actions workflow is intended to run daily at 8:00 AM Sydney time (22:00 UTC on the previous day during AEDT, 23:00 UTC during AEST; the workflow schedule uses UTC and will need seasonal adjustment or a timezone-aware scheduler).

## Setup

1. In GitHub, open **Settings → Pages** and publish from the `main` branch, root folder.
2. Enable GitHub Actions if prompted.
3. Check the **Actions** tab after the first scheduled or manual run.

The initial API files contain empty, clearly marked data until a verified scrape has completed.
