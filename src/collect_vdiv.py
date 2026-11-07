"""Collect Berlin members from the public VDIV Berlin-Brandenburg member list.

Source: https://vdiv-bb.de/mitglieder/ (one page, all members).
Output: data/vdiv_berlin.csv
"""
import csv
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://vdiv-bb.de/mitglieder/"
OUT = Path(__file__).resolve().parent.parent / "data" / "vdiv_berlin.csv"
UA = {"User-Agent": "Mozilla/5.0 (case-study research; contact nabeel5003143@gmail.com)"}


def text(card, selector):
    el = card.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select(".contact-list-contact-container"):
        zip_city = text(card, ".contact-list-address-country-and-state")
        m = re.match(r"(\d{5})\s+(.*)", zip_city)
        plz, city = (m.group(1), m.group(2)) if m else ("", zip_city)
        website = ""
        for field in card.select(".contact-list-custom-fields-container > div"):
            if "Internet" in field.get_text():
                a = field.select_one("a[href]")
                website = a["href"] if a else ""
                break
        rows.append({
            "name": text(card, ".contact-list-contact-name"),
            "contact_person": text(card, ".contact-list-job-title"),
            "email": text(card, ".contact-list-email"),
            "phone": text(card, ".contact-list-phone-1 a"),
            "street": text(card, ".contact-list-address-line-1"),
            "plz": plz,
            "city": city,
            "website": website,
            "source": "VDIV Berlin-Brandenburg",
            "source_url": URL,
        })
    return rows


def is_berlin(row):
    # Berlin postcodes are 10115-14199; the city field is free text ("Berllin", "Berlin-Wilmersdorf").
    return row["plz"].isdigit() and 10115 <= int(row["plz"]) <= 14199 and "berl" in row["city"].lower()


def main():
    html = requests.get(URL, headers=UA, timeout=30).text
    # The page renders the full list twice, so drop exact repeats.
    rows = list({tuple(r.values()): r for r in parse(html)}.values())
    berlin = [r for r in rows if is_berlin(r)]
    OUT.parent.mkdir(exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(berlin[0]))
        w.writeheader()
        w.writerows(berlin)
    print(f"{len(rows)} members total, {len(berlin)} in Berlin -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
