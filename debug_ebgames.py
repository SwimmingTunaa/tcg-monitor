"""
Debug script — dumps EB Games product page HTML so we can find the right selectors.
Waits for JS to render before capturing.
"""
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from discovery.base_discovery import STEALTH_JS, BROWSER_PROFILE_DIR, make_playwright_context
from bs4 import BeautifulSoup
import os

URL = "https://www.ebgames.com.au/product/toys-and-collectibles/339236-pokemon-tcg-mega-evolution-perfect-order-elite-trainer-box"

with sync_playwright() as p:
    context = make_playwright_context(p, headed=False, profile_dir=os.path.abspath(BROWSER_PROFILE_DIR))
    context.add_init_script(STEALTH_JS)
    page = context.new_page()

    print(f"Loading {URL}...")
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)

    # Wait for any h1 to appear
    try:
        page.wait_for_selector("h1", timeout=15000)
        print("h1 found")
    except PlaywrightTimeout:
        print("h1 not found after 15s")

    # Extra wait for JS hydration
    page.wait_for_timeout(3000)

    html = page.content()
    page.close()
    context.close()

print(f"Got {len(html):,} chars")

with open("/tmp/ebgames_product.html", "w") as f:
    f.write(html)
print("Saved to /tmp/ebgames_product.html")

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
