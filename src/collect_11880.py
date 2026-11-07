"""Collect Berlin Hausverwaltungen from 11880.com.

Each result page embeds a schema.org ItemList (JSON-LD) with name, email,
address and phone. 11880 has no website field, so the website is derived
from the email domain unless it is a free-mail provider.
Search pages are not disallowed in 11880.com/robots.txt. One request per second.
Output: data/11880_berlin.csv
"""
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.11880.com/suche/hausverwaltung/berlin"
OUT = Path(__file__).resolve().parent.parent / "data" / "11880_berlin.csv"
UA = {"User-Agent": "Mozilla/5.0 (case-study research; contact nabeel5003143@gmail.com)"}
FREEMAIL = {"gmail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "yahoo.de", "yahoo.com",
            "hotmail.com", "hotmail.de", "outlook.com", "outlook.de", "aol.com", "freenet.de",
            "icloud.com", "arcor.de", "posteo.de", "mail.de", "online.de", "live.de", "me.com"}


def items(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        blocks = data if isinstance(data, list) else [data]
        # The ItemList is nested: SearchResultsPage -> mainEntity.
        blocks += [b["mainEntity"] for b in blocks if isinstance(b.get("mainEntity"), dict)]
        for block in blocks:
            if block.get("@type") == "ItemList":
                for el in block.get("itemListElement", []):
                    yield el.get("item", {})


def row(item):
    addr = item.get("address", {})
    email = item.get("email", "")
    dom = email.split("@")[-1].lower() if "@" in email else ""
    rating = item.get("aggregateRating", {})
    return {
        "name": item.get("name", ""),
        "street": addr.get("streetAddress", ""),
        "plz": addr.get("postalCode", ""),
        "phone": item.get("telephone", ""),
        "email": email,
        "website": "" if not dom or dom in FREEMAIL else f"https://{dom}",
        "listing_type": item.get("@type", ""),
        "rating": rating.get("ratingValue", ""),
        "reviews": rating.get("reviewCount", ""),
        "source": "11880",
        "source_url": item.get("url", ""),
    }


def main():
    s = requests.Session()
    s.headers.update(UA)
    first = s.get(URL, timeout=30).text
    total = int(re.search(r"(\d+)\s*Treffer", BeautifulSoup(first, "html.parser").get_text(" ")).group(1))
    rows = [row(i) for i in items(first)]
    per_page = len(rows) or 1
    page = 2
    while len(rows) < total and page <= total // per_page + 2:
        time.sleep(1)
        batch = [row(i) for i in items(s.get(URL, params={"page": page}, timeout=30).text)]
        if not batch:
            break
        rows.extend(batch)
        print(f"  page {page}: {len(rows)}/{total}", file=sys.stderr, end="\r")
        page += 1
    rows = list({r["source_url"]: r for r in rows}.values())
    berlin = [r for r in rows if r["plz"].isdigit() and 10115 <= int(r["plz"]) <= 14199]
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(berlin)
    print(f"\n{len(rows)} unique of {total} listed, {len(berlin)} with a Berlin postcode -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
