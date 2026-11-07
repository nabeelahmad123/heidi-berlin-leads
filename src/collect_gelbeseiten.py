"""Collect Berlin Hausverwaltungen from Gelbe Seiten.

The first 50 results come from the listing page, the rest from the
"Mehr anzeigen" endpoint (/ajaxsuche, 10 per call). Neither path is
disallowed in gelbeseiten.de/robots.txt. One request per second.
Output: data/gelbeseiten_berlin.csv
"""
import base64
import csv
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://www.gelbeseiten.de/branchen/hausverwaltung/berlin"
AJAX_URL = "https://www.gelbeseiten.de/ajaxsuche"
OUT = Path(__file__).resolve().parent.parent / "data" / "gelbeseiten_berlin.csv"
UA = {"User-Agent": "Mozilla/5.0 (case-study research; contact nabeel5003143@gmail.com)"}


def b64(value):
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception:
        return ""


def text(el, selector):
    found = el.select_one(selector)
    return found.get_text(" ", strip=True) if found else ""


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for art in soup.select("article.mod-Treffer"):
        site = art.select_one("[data-webseitelink]")
        address = text(art, ".mod-AdresseKompakt__adress-text") or text(art, "address")
        m = re.search(r"(\d{5})\s+Berlin", address)
        district = re.search(r"\(([^)]+)\)", address)
        rating = art.select_one(".mod-BewertungKompakt__number") or art.select_one("[class*=Bewertung] .gc-rating__number")
        reviews = re.search(r"(\d+)\s+Bewertung", art.get_text(" ", strip=True))
        rows.append({
            "name": text(art, "h2"),
            "category": text(art, ".mod-Treffer--besteBranche"),
            "street": address.split(",")[0].strip() if "," in address else "",
            "plz": m.group(1) if m else "",
            "district": district.group(1) if district else "",
            "phone": text(art, ".mod-TelefonnummerKompakt__phoneNumber") or text(art, "[class*=phoneNumber]"),
            "website": b64(site["data-webseitelink"]) if site else "",
            "gs_rating": rating.get_text(strip=True) if rating else "",
            "gs_reviews": reviews.group(1) if reviews else "",
            "source": "Gelbe Seiten",
            "source_url": f"https://www.gelbeseiten.de/gsbiz/{art.get('data-realid', '')}",
        })
    return rows


def main():
    s = requests.Session()
    s.headers.update(UA)
    first = s.get(LIST_URL, timeout=30).text
    total = int(re.search(r'id="loadMoreGesamtzahl">(\d+)<', first).group(1))
    rows = parse(first)
    position = len(rows) + 1
    while position <= total:
        time.sleep(1)
        r = s.post(AJAX_URL, timeout=30, data={
            "umkreis": "-1", "verwandt": "false", "WAS": "hausverwaltung",
            "WO": "berlin", "position": str(position), "anzahl": "10",
        })
        batch = parse(r.json().get("html", ""))
        if not batch:
            break
        rows.extend(batch)
        position += len(batch)
        print(f"  {len(rows)}/{total}", file=sys.stderr, end="\r")
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n{len(rows)} of {total} listings -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
