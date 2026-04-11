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

DEBUG = False
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = 645317853

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "price_history.json")
PRODUCTS_FILE = os.path.join(os.path.dirname(__file__), "products_to_track.txt")
OFFSET_FILE = os.path.join(os.path.dirname(__file__), "telegram_offset.txt")


def log(msg: str):
    if DEBUG:
        print(f"[debug] {msg}")


def telegram_get_chat_id():
    """Fetch chat ID from the most recent message sent to the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set.")
        return None
    resp = requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates")
    data = resp.json()
    if not data.get("ok") or not data.get("result"):
        print("No messages found. Send a message to the bot first, then retry.")
        return None
    chat_id = data["result"][-1]["message"]["chat"]["id"]
    print(f"Chat ID: {chat_id}")
    return chat_id


def telegram_send(text: str):
    """Send a message via Telegram bot. Silently skips if not configured."""
    if not TELEGRAM_BOT_TOKEN:
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
    )
    if resp.ok:
        log("Telegram message sent.")
    else:
        print(f"Telegram send failed: {resp.text}")


def telegram_reply(chat_id, text: str):
    """Reply to a specific chat."""
    if not TELEGRAM_BOT_TOKEN:
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
    )


def load_offset() -> int:
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    return 0


def save_offset(offset: int):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def check_telegram_messages():
    """Check for new Myntra URLs sent to the bot and add them to tracking."""
    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set.")
        return

    last_offset = load_offset()
    params = {"offset": last_offset + 1} if last_offset else {}
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
        params=params,
    )
    data = resp.json()

    if not data.get("ok") or not data.get("result"):
        print("No new messages.")
        return

    # Load existing tracked URLs
    existing_urls = set()
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE) as f:
            existing_urls = {line.strip() for line in f if line.strip()}

    myntra_pattern = re.compile(r'https?://www\.myntra\.com/\S+')

    for update in data["result"]:
        update_id = update["update_id"]
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")

        urls = myntra_pattern.findall(text)

        for url in urls:
            if url in existing_urls:
                print(f"Already tracking: {url}")
                telegram_reply(chat_id, f"Already tracking:\n{url}")
            else:
                with open(PRODUCTS_FILE, "a") as f:
                    f.write(f"\n{url}")
                existing_urls.add(url)
                print(f"Added: {url}")
                telegram_reply(chat_id, f"✅ Added for tracking:\n{url}")

        if not urls and text:
            telegram_reply(chat_id, "Send me a Myntra product URL to start tracking its price.")

        save_offset(update_id)

    print(f"Processed {len(data['result'])} update(s).")


def handle_list(chat_id):
    """Send a numbered list of tracked products to the chat."""
    if not os.path.exists(PRODUCTS_FILE):
        telegram_reply(chat_id, "No products being tracked.")
        return

    with open(PRODUCTS_FILE) as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        telegram_reply(chat_id, "No products being tracked.")
        return

    history = load_history()
    lines = []
    for i, url in enumerate(urls, 1):
        entry = history.get(url)
        if entry:
            name = entry.get("product_name", url)
        else:
            # Derive name from URL slug
            parts = url.rstrip("/").split("/")
            name = next((p.replace("-", " ").title() for p in reversed(parts) if not p.isdigit()), url)
        lines.append(f"{i}. {name}")

    msg = "📋 *Tracked Products:*\n" + "\n".join(lines)
    telegram_reply(chat_id, msg)


def handle_remove(chat_id, text: str):
    """Remove a product by its number from the tracked list."""
    parts = text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        telegram_reply(chat_id, "Usage: /remove <number>\nUse /list to see product numbers.")
        return

    index = int(parts[1])

    if not os.path.exists(PRODUCTS_FILE):
        telegram_reply(chat_id, "No products being tracked.")
        return

    with open(PRODUCTS_FILE) as f:
        urls = [line.strip() for line in f if line.strip()]

    if index < 1 or index > len(urls):
        telegram_reply(chat_id, f"Invalid number. Use /list to see products (1-{len(urls)}).")
        return

    removed_url = urls.pop(index - 1)

    # Get product name before removing from history
    history = load_history()
    entry = history.pop(removed_url, None)
    product_name = entry.get("product_name", removed_url) if entry else removed_url

    # Rewrite products file
    with open(PRODUCTS_FILE, "w") as f:
        f.write("\n".join(urls) + ("\n" if urls else ""))

    save_history(history)

    telegram_reply(chat_id, f"✅ Removed: {product_name}")


def handle_showprices(chat_id):
    """Scrape all tracked products and send a price summary."""
    if not os.path.exists(PRODUCTS_FILE):
        telegram_reply(chat_id, "No products being tracked.")
        return

    with open(PRODUCTS_FILE) as f:
        urls = [line.strip() for line in f if line.strip()]

    if not urls:
        telegram_reply(chat_id, "No products being tracked.")
        return

    telegram_reply(chat_id, f"⏳ Checking prices for {len(urls)} product(s)...")

    summaries = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}]")
        summaries.append(check_price(url))

    msg = f"📊 *Price Tracker Update*\n_{datetime.now().strftime('%d %b %Y, %I:%M %p')}_\n\n"
    msg += "\n\n".join(summaries)
    telegram_send(msg)


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
    log(f"Fetching {url} via requests...")
    session = requests.Session()
    try:
        response = session.get(url, headers=HEADERS, timeout=20)
        log(f"Response status: {response.status_code}")
        log(f"Response URL: {response.url}")
        response.raise_for_status()
    except Exception as e:
        log(f"ERROR: Request failed: {e}")
        raise

    html = response.text
    log(f"HTML length: {len(html)}")

    has_pdp_data = "pdpData" in html
    has_ld_json = "application/ld+json" in html
    has_price_key = '"price"' in html or '"mrp"' in html
    log(f"Has pdpData: {has_pdp_data}")
    log(f"Has ld+json: {has_ld_json}")
    log(f"Has price/mrp keys: {has_price_key}")

    if not (has_pdp_data or has_ld_json or has_price_key):
        log(f"WARNING: No price data found. First 3000 chars of HTML:")
        print(html[:3000])

    return html


WATCHED_SIZES = {
    "shoes": ["9", "10"],   # UK Size
    "shirts": ["M"],
}


def extract_sizes(html: str) -> list:
    """Extract size availability from Myntra HTML."""
    match = re.search(r'"sizes":\s*\[', html)
    if not match:
        return []
    start = match.start() + len('"sizes":')
    try:
        decoder = json.JSONDecoder()
        sizes, _ = decoder.raw_decode(html, start)
        return sizes
    except (json.JSONDecodeError, ValueError):
        return []


def check_size_availability(sizes: list, url: str) -> str:
    """Check if watched sizes are available. Returns a summary string."""
    if not sizes:
        return ""

    # Determine product type from URL
    is_shoe = any(kw in url.lower() for kw in ["shoes", "sneakers", "sandals", "footwear"])
    watched = WATCHED_SIZES["shoes"] if is_shoe else WATCHED_SIZES["shirts"]

    results = []
    for s in sizes:
        label = s.get("label", "")
        if label in watched:
            available = s.get("available", False)
            status = "✅" if available else "❌"
            size_type = s.get("sizeType") or ""
            display = f"{size_type} {label}".strip()
            results.append(f"{status} {display}")

    return " | ".join(results) if results else ""


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


def check_price(url: str) -> str:
    """Check price for a URL. Returns a summary line for Telegram."""
    print(f"Fetching: {url}\n")

    html = fetch_page(url)
    price_data = extract_price(html)

    if not price_data:
        print("Could not extract price from the page.")
        return f"❌ Could not extract price: {url}"

    product_name = extract_product_name(html, url)
    selling_price = price_data["selling_price"]
    mrp = price_data["mrp"]
    discount = price_data["discount_percent"]
    now = datetime.now().isoformat(timespec="seconds")

    sizes = extract_sizes(html)
    size_info = check_size_availability(sizes, url)

    print(f"Product : {product_name}")
    print(f"MRP     : ₹{mrp:,}")
    print(f"Price   : ₹{selling_price:,}")
    if discount:
        print(f"Discount: {discount}% off")
    if size_info:
        print(f"Sizes   : {size_info}")

    history = load_history()
    prev = history.get(url)
    change_line = ""

    if prev:
        prev_price = prev["selling_price"]
        if selling_price < prev_price:
            drop = prev_price - selling_price
            drop_pct = round(drop / prev_price * 100, 1)
            print(f"\n*** PRICE DROP DETECTED! ***")
            print(f"    Was : ₹{prev_price:,}  (recorded {prev['recorded_at']})")
            print(f"    Now : ₹{selling_price:,}")
            print(f"    Drop: ₹{drop:,} ({drop_pct}% cheaper)")
            change_line = f" 🔻 ₹{drop:,} ({drop_pct}%)"
        elif selling_price > prev_price:
            rise = selling_price - prev_price
            rise_pct = round(rise / prev_price * 100, 1)
            print(f"\nPrice went up by ₹{rise:,} ({rise_pct}%) since last check ({prev['recorded_at']}).")
            change_line = f" 🔺 ₹{rise:,} ({rise_pct}%)"
        else:
            print(f"\nNo price change since last check ({prev['recorded_at']}).")
            change_line = " ➖ no change"
    else:
        print("\nNo previous price on record — current price saved as baseline.")
        change_line = " 🆕 first check"

    history[url] = {
        "product_name": product_name,
        "mrp": mrp,
        "selling_price": selling_price,
        "discount_percent": discount,
        "recorded_at": now,
    }
    save_history(history)
    print(f"\nHistory saved to {HISTORY_FILE}")

    discount_str = f" ({discount}% off)" if discount else ""
    size_line = f"\nSizes: {size_info}" if size_info else ""
    return f"*{product_name}*\n₹{selling_price:,}{discount_str}{change_line}{size_line}\n[View on Myntra]({url})"


if __name__ == "__main__":
    # Helper: fetch and print chat ID
    if len(sys.argv) > 1 and sys.argv[1] == "--chat-id":
        telegram_get_chat_id()
        sys.exit(0)

    # Check for new URLs from Telegram messages
    if len(sys.argv) > 1 and sys.argv[1] == "--check-messages":
        check_telegram_messages()
        sys.exit(0)

    summaries = []

    if len(sys.argv) > 1:
        # Track a single URL passed as argument
        summaries.append(check_price(sys.argv[1]))
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
            summaries.append(check_price(url))
            print()
    else:
        print("Usage: python price_tracer.py <myntra-product-url>")
        print("       python price_tracer.py --chat-id")
        print()
        print("Or create products_to_track.txt with one URL per line to track multiple products.")
        sys.exit(1)

    # Send Telegram summary
    if summaries and TELEGRAM_BOT_TOKEN:
        msg = f"📊 *Price Tracker Update*\n_{datetime.now().strftime('%d %b %Y, %I:%M %p')}_\n\n"
        msg += "\n\n".join(summaries)
        telegram_send(msg)
