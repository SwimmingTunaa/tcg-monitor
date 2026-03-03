"""
Run this ONCE in headed mode to solve the Cloudflare challenge.
After you pass it, the browser profile saves the cookies and
future headless runs will work automatically.
"""
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from discovery.base_discovery import STEALTH_JS, BROWSER_PROFILE_DIR, make_playwright_context
from bs4 import BeautifulSoup
import os

URL = "https://www.ebgames.com.au/product/toys-and-collectibles/339236-pokemon-tcg-mega-evolution-perfect-order-elite-trainer-box"

print("Opening headed browser — solve any Cloudflare challenge, then wait...")

with sync_playwright() as p:
    context = make_playwright_context(p, headed=True, profile_dir=os.path.abspath(BROWSER_PROFILE_DIR))
    context.add_init_script(STEALTH_JS)
    page = context.new_page()

    page.goto(URL, wait_until="domcontentloaded", timeout=30000)

    # Wait up to 60s for real product content (h1 with actual product name)
    print("Waiting for product page to load (solve Cloudflare if prompted)...")
    try:
        page.wait_for_function(
            "() => document.querySelector('h1') && document.querySelector('h1').innerText !== 'www.ebgames.com.au'",
            timeout=60000
        )
        print("✅ Product page loaded!")
    except PlaywrightTimeout:
        print("⚠️  Timed out — trying anyway...")

    page.wait_for_timeout(2000)
    html = page.content()
    page.close()
    # Wait before closing context so cookies flush to disk
    context.clear_permissions()
    context.close()
    import time; time.sleep(2)

print(f"Got {len(html):,} chars")

soup = BeautifulSoup(html, "lxml")

print("\n── h1 tags ──")
for el in soup.find_all("h1"):
    print(f"  class={el.get('class')}  text={el.get_text(strip=True)[:80]}")

print("\n── Elements with 'price' in class ──")
for el in soup.find_all(class_=lambda c: c and 'price' in ' '.join(c).lower()):
    print(f"  <{el.name} class={el.get('class')}> {el.get_text(strip=True)[:80]}")

print("\n── Buttons ──")
for el in soup.find_all("button"):
    print(f"  class={el.get('class')}  text={el.get_text(strip=True)[:80]}")

print("\n── Elements with 'preorder' or 'pre-order' in class/text ──")
for el in soup.find_all(class_=lambda c: c and any(x in ' '.join(c).lower() for x in ['preorder', 'pre-order'])):
    print(f"  <{el.name} class={el.get('class')}> {el.get_text(strip=True)[:80]}")

print("\n── Elements with 'stock' in class ──")
for el in soup.find_all(class_=lambda c: c and 'stock' in ' '.join(c).lower()):
    print(f"  <{el.name} class={el.get('class')}> {el.get_text(strip=True)[:80]}")

print("\n── Meta og:title / og:image ──")
for el in soup.find_all("meta", property=lambda p: p and p.startswith("og:")):
    print(f"  {el.get('property')} = {el.get('content', '')[:100]}")
