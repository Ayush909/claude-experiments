#!/usr/bin/env python3
"""
Myntra Price Tracer
Fetches the current price of a shoe from a Myntra URL and alerts on price drops.
Prices are persisted in price_history.json for comparison across runs.
"""

import re
import json
import sys
import os
from datetime import datetime
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4")
    sys.exit(1)

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "price_history.json")
PRODUCTS_FILE = os.path.join(os.path.dirname(__file__), "products_to_track.txt")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def fetch_page(url: str) -> str:
    session = requests.Session()
    response = session.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response.text


def extract_price(html: str) -> Optional[dict]:
    """
    Tries multiple strategies to extract price from Myntra's HTML.
    Returns a dict with 'mrp', 'selling_price', and 'discount_percent'.
    """
    # Strategy 1: look for pdpData JSON blob embedded in a <script> tag
    pdp_match = re.search(r'window\.__pdpData\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not pdp_match:
        pdp_match = re.search(r'"pdpData"\s*:\s*(\{.*?"style".*?\})', html, re.DOTALL)

    # Strategy 2: look for structured JSON in <script type="application/ld+json">
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next((d for d in data if d.get("@type") == "Product"), None)
            if data and data.get("@type") == "Product":
                offers = data.get("offers", {})
                price = offers.get("price") or offers.get("lowPrice")
                mrp = offers.get("highPrice") or price
                if price:
                    price = int(float(price))
                    mrp = int(float(mrp))
                    discount = round((mrp - price) / mrp * 100) if mrp > price else 0
                    return {"mrp": mrp, "selling_price": price, "discount_percent": discount}
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue

    # Strategy 3: regex scan for price patterns in the raw HTML
    # Myntra often has: "mrp":4999,"price":2999 or "discountedPrice":2999
    mrp_match = re.search(r'"mrp"\s*:\s*(\d+)', html)
    price_match = re.search(r'"(?:price|discountedPrice|sellingPrice)"\s*:\s*(\d+)', html)

    if mrp_match and price_match:
        mrp = int(mrp_match.group(1))
        selling_price = int(price_match.group(1))
        discount = round((mrp - selling_price) / mrp * 100) if mrp > selling_price else 0
        return {"mrp": mrp, "selling_price": selling_price, "discount_percent": discount}

    if price_match:
        price = int(price_match.group(1))
        return {"mrp": price, "selling_price": price, "discount_percent": 0}

    return None


def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def save_history(history: dict):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def extract_product_name(html: str, url: str) -> str:
    """Best-effort product name extraction."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("title")
    if tag and tag.string:
        return tag.string.strip()
    # Fallback: derive from URL segments
    parts = url.rstrip("/").split("/")
    for part in reversed(parts):
        if part.isdigit():
            continue
        return part.replace("-", " ").title()
    return url


def check_price(url: str):
    print(f"Fetching: {url}\n")

    html = fetch_page(url)
    price_data = extract_price(html)

    if not price_data:
        print("Could not extract price from the page.")
        print("Myntra may require a browser session. Try running with Selenium/Playwright.")
        sys.exit(1)

    product_name = extract_product_name(html, url)
    selling_price = price_data["selling_price"]
    mrp = price_data["mrp"]
    discount = price_data["discount_percent"]
    now = datetime.now().isoformat(timespec="seconds")

    print(f"Product : {product_name}")
    print(f"MRP     : ₹{mrp:,}")
    print(f"Price   : ₹{selling_price:,}")
    if discount:
        print(f"Discount: {discount}% off")

    history = load_history()
    prev = history.get(url)

    if prev:
        prev_price = prev["selling_price"]
        if selling_price < prev_price:
            drop = prev_price - selling_price
            drop_pct = round(drop / prev_price * 100, 1)
            print(f"\n*** PRICE DROP DETECTED! ***")
            print(f"    Was : ₹{prev_price:,}  (recorded {prev['recorded_at']})")
            print(f"    Now : ₹{selling_price:,}")
            print(f"    Drop: ₹{drop:,} ({drop_pct}% cheaper)")
        elif selling_price > prev_price:
            rise = selling_price - prev_price
            rise_pct = round(rise / prev_price * 100, 1)
            print(f"\nPrice went up by ₹{rise:,} ({rise_pct}%) since last check ({prev['recorded_at']}).")
        else:
            print(f"\nNo price change since last check ({prev['recorded_at']}).")
    else:
        print("\nNo previous price on record — current price saved as baseline.")

    history[url] = {
        "product_name": product_name,
        "mrp": mrp,
        "selling_price": selling_price,
        "discount_percent": discount,
        "recorded_at": now,
    }
    save_history(history)
    print(f"\nHistory saved to {HISTORY_FILE}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Track a single URL passed as argument
        check_price(sys.argv[1])
    elif os.path.exists(PRODUCTS_FILE):
        # Track all URLs from products_to_track.txt
        with open(PRODUCTS_FILE) as f:
            urls = [line.strip() for line in f if line.strip()]

        if not urls:
            print(f"No URLs found in {PRODUCTS_FILE}")
            sys.exit(1)

        print(f"Tracking {len(urls)} product(s)...\n")
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]")
            check_price(url)
            print()
    else:
        print("Usage: python price_tracer.py <myntra-product-url>")
        print()
        print("Or create products_to_track.txt with one URL per line to track multiple products.")
        print()
        print("Example:")
        print("  python price_tracer.py 'https://www.myntra.com/mailers/shoes/puma/puma-unisex-future-rider-displaced-sneakers/24093010/buy'")
        sys.exit(1)
