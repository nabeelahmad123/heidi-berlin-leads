"""Collect Berlin Hausverwaltungen from Das Örtliche.

Topic pages (Themen/Hausverwaltung/Berlin-Seite-N.html) embed a schema.org
ItemList with name, phone, address and geo-coordinates. robots.txt allows
all paths. One request per second; stops at the first non-200 response.
Output: data/oertliche_berlin.csv
"""
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.dasoertliche.de/Themen/Hausverwaltung/Berlin{}.html"
OUT = Path(__file__).resolve().parent.parent / "data" / "oertliche_berlin.csv"
UA = {"User-Agent": "Mozilla/5.0 (case-study research; contact nabeel5003143@gmail.com)"}


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tag in soup.select('script[type="application/ld+json"]'):
        data = json.loads(tag.string or tag.get_text() or "{}")
        if data.get("@type") != "ItemList":
            continue
        for el in data.get("itemListElement", []):
            item = el.get("item", {})
            addr, geo = item.get("address", {}), item.get("geo", {})
            rows.append({
                "name": item.get("name", ""),
                "street": addr.get("streetAddress", ""),
                "plz": addr.get("postalCode", ""),
                "phone": item.get("telephone", ""),
                "lat": geo.get("latitude", ""),
                "lon": geo.get("longitude", ""),
                "source": "Das Örtliche",
                "source_url": item.get("url", ""),
            })
    total = re.search(r"Treffer\s*\d+-\d+\s*von\s*(\d+)", soup.get_text(" ", strip=True).replace("\xa0", " "))
    return rows, int(total.group(1)) if total else None


def main():
    s = requests.Session()
    s.headers.update(UA)
    rows, total = parse(s.get(BASE.format(""), timeout=30).text)
    page = 2
    while total and len(rows) < total:
        time.sleep(1)
        r = s.get(BASE.format(f"-Seite-{page}"), timeout=30)
        if r.status_code != 200:
            print(f"\nstopped: page {page} returned {r.status_code}", file=sys.stderr)
            break
        batch, _ = parse(r.text)
        if not batch:
            break
        rows.extend(batch)
        print(f"  page {page}: {len(rows)}/{total}", file=sys.stderr, end="\r")
        page += 1
    rows = list({r["source_url"]: r for r in rows}.values())
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} unique of {total} listed -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
