#!/bin/bash
# Double-click this file in Finder to refresh the LEGO Deals API.
# It must be run from a local clone of xuwangdaalex/lego-deals.

set -Eeuo pipefail
BACKUP_DIR=""

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

finish() {
  status=$?
  if [ -n "${BACKUP_DIR:-}" ] && [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
  fi
  echo
  if [ "$status" -eq 0 ]; then
    echo "=================================================="
    echo "LEGO Deals update finished successfully."
    echo "=================================================="
  else
    echo "=================================================="
    echo "The update stopped because of an error (exit code: $status)."
    echo "Read the message above, fix the issue, and run this file again."
    echo "=================================================="
  fi
  echo
  read -r -p "Press Return to close this window..."
}
trap finish EXIT

echo "=================================================="
echo "       LEGO DEALS - ONE-CLICK API REFRESH"
echo "=================================================="
echo
echo "Repository: $ROOT"
echo

# Confirm this is the expected Git repository.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: This file must be inside your lego-deals Git repository."
  exit 1
fi

REPO_URL="$(git remote get-url origin 2>/dev/null || true)"
if [[ "$REPO_URL" != *"xuwangdaalex/lego-deals"* ]]; then
  echo "ERROR: The 'origin' remote does not look like xuwangdaalex/lego-deals."
  echo "Current origin: ${REPO_URL:-not configured}"
  exit 1
fi

# Use the Monterey-compatible virtual environment already used for local scraping.
if [ -x "$ROOT/.venv-monterey/bin/python" ]; then
  PYTHON="$ROOT/.venv-monterey/bin/python"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  echo "ERROR: Python virtual environment not found."
  echo "Expected .venv-monterey or .venv in this repository."
  echo "Please set up the environment before running this file."
  exit 1
fi

echo "Checking Python packages and installed Google Chrome..."
"$PYTHON" - <<'PY'
import requests
import bs4
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    browser.close()

print("Python packages and Google Chrome: OK")
PY

# Do not overwrite any of your own uncommitted work.
if [ -n "$(git status --porcelain)" ]; then
  echo
  echo "ERROR: Your repository has uncommitted changes."
  echo "Please commit or stash your changes, then run this file again."
  git status --short
  exit 1
fi

echo
echo "Step 1/4: Downloading the latest repository changes..."
git pull --rebase origin main

echo
echo "Creating a safety copy of the current API files..."
BACKUP_DIR="$(mktemp -d)"
cp api/*.json "$BACKUP_DIR/"

echo
echo
echo "Step 2/4: Refreshing BIG W only using your installed Google Chrome..."
if ! "$PYTHON" scraper/update_bigw_local.py; then
  echo "BIG W refresh failed. Restoring the previous API files so existing data is not lost."
  cp "$BACKUP_DIR"/*.json api/
  exit 1
fi

echo
echo "Step 3/4: Checking generated API files..."
for file in api/all-products.json api/lego-deals.json api/last-updated.json api/history.json api/bigw-lego-data.json; do
  if [ ! -s "$file" ]; then
    echo "ERROR: $file is missing or empty. Restoring previous API files."
    cp "$BACKUP_DIR"/*.json api/
    exit 1
  fi
done
if ! "$PYTHON" - <<'PY'
import json
from pathlib import Path

for name in ("all-products.json", "lego-deals.json", "last-updated.json", "history.json"):
    path = Path("api") / name
    with path.open(encoding="utf-8") as f:
        json.load(f)
    print(f"Valid JSON: {path}")

data = json.loads(Path("api/all-products.json").read_text(encoding="utf-8"))
products = data.get("products", [])
bigw_count = sum(1 for p in products if p.get("retailer") == "bigw")
print(f"Total products in all-products.json: {len(products)}")
print(f"BIG W products: {bigw_count}")
print(f"Overall API status: {data.get('coverage', {}).get('status', 'unknown')}")
bigw_file = Path("api/bigw-lego-data.json")
bigw_data = json.loads(bigw_file.read_text(encoding="utf-8"))
print(f"Products in bigw-lego-data.json: {bigw_data.get('product_count', len(bigw_data.get('products', [])))}")
if bigw_count == 0 or not bigw_data.get("products"):
    raise SystemExit("Validation failed: no BIG W products were found in the combined or standalone JSON file.")
PY
then
  echo "API validation failed. Restoring previous API files."
  cp "$BACKUP_DIR"/*.json api/
  exit 1
fi

echo
echo "Step 4/4: Committing and pushing updated API files to GitHub..."
git add api/

if git diff --cached --quiet; then
  echo "No API changes to commit. GitHub is already up to date."
  exit 0
fi

git config user.name "LEGO Deals Local Updater"
git config user.email "lego-deals-local-updater@users.noreply.github.com"
git commit -m "Refresh LEGO deals API from local Mac"

# Retry if another update reaches main while this run is in progress.
for attempt in 1 2 3; do
  if git push origin HEAD:main; then
    echo
    echo "GitHub push completed successfully."
    exit 0
  fi

  if [ "$attempt" -lt 3 ]; then
    echo "Push attempt $attempt failed. Fetching latest main and retrying..."
    git pull --rebase origin main
  fi
done

echo "ERROR: Could not push to GitHub after 3 attempts."
echo "Check your saved GitHub credentials and repository access."
exit 1
