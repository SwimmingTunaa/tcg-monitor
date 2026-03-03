"""
Dig into the raw HTML around price and stock areas.
"""
from bs4 import BeautifulSoup

with open("/tmp/ebgames_product.html") as f:
    html = f.read()

soup = BeautifulSoup(html, "lxml")

# Find the product detail / buy box area
print("── Searching for price text '$' ──")
import re
for el in soup.find_all(string=re.compile(r'\$\d+')):
    parent = el.parent
    print(f"  <{parent.name} class={parent.get('class')}> '{el.strip()[:80]}'")

print("\n── product-detail / buy-box / purchase area ──")
for sel in ['.product-detail', '.buy-box', '.purchase', '.product-info', '.product-buy', '.pdp']:
    el = soup.select_one(sel)
    if el:
        print(f"  FOUND: {sel}")
        print(f"  {str(el)[:300]}")
    else:
        print(f"  not found: {sel}")

print("\n── divs/spans near the h1 ──")
h1 = soup.find("h1")
if h1:
    # Walk up to find a meaningful container
    container = h1.parent
    for _ in range(4):
        if container:
            print(f"\n  Parent: <{container.name} class={container.get('class')}>")
            # Print direct children summary
            for child in container.children:
                if hasattr(child, 'name') and child.name:
                    text = child.get_text(strip=True)[:60]
                    print(f"    <{child.name} class={child.get('class')}> {text}")
            container = container.parent

print("\n── Preorder date / release date text ──")
for el in soup.find_all(string=re.compile(r'pre.?order|release|27 mar|deposit', re.I)):
    parent = el.parent
    print(f"  <{parent.name} class={parent.get('class')}> '{el.strip()[:100]}'")
