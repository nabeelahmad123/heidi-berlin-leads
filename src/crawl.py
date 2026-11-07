"""Fetch each company's homepage plus its most informative subpages.

Subpages are picked by link text/URL: Impressum, Über uns, Leistungen,
Karriere, Mieter-/Eigentümerportal, Team, Kontakt. Max 6 pages per site.
Plain text is cached in data/pages/<domain>.json so re-runs are free.

Usage: python src/crawl.py [limit] [--vdiv-first]
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE = DATA / "pages"
UA = {"User-Agent": "Mozilla/5.0 (case-study research; contact nabeel5003143@gmail.com)"}
SUBPAGE = re.compile(r"impressum|über|ueber|about|unternehmen|leistung|service|verwaltung|"
                     r"karriere|jobs|stellen|portal|team|kontakt|referenz", re.I)
MAX_PAGES = 6
TIMEOUT = 12


def fetch(session, url):
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None, None
        r.encoding = r.apparent_encoding or r.encoding
        return r.url, r.text
    except requests.RequestException:
        return None, None


def bare_host(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return soup, " ".join(soup.get_text(" ", strip=True).split())


def crawl(website):
    url = website if website.startswith("http") else "http://" + website
    out = {"start_url": url, "final_url": None, "status": "unreachable", "pages": {}}
    with requests.Session() as s:
        s.headers.update(UA)
        final, html = fetch(s, url)
        if not html:
            return out
        out["final_url"], out["status"] = final, "ok"
        soup, text = page_text(html)
        out["pages"][final] = {"title": soup.title.get_text(strip=True) if soup.title else "", "text": text[:20000]}
        out["html_signals"] = html_signals(html)
        host = bare_host(final)
        links = []
        for a in soup.select("a[href]"):
            href = urljoin(final, a["href"]).split("#")[0]
            label = a.get_text(" ", strip=True)
            if (href.startswith("http") and bare_host(href) == host and href != final
                    and href not in links and SUBPAGE.search(href + " " + label)):
                links.append(href)
        if not any("impressum" in h.lower() for h in links):
            links.append(urljoin(final, "/impressum"))
        # Impressum first: it names the Geschäftsführer.
        links.sort(key=lambda h: 0 if "impressum" in h.lower() else 1)
        for href in links[: MAX_PAGES - 1]:
            time.sleep(0.3)
            u, h = fetch(s, href)
            if h:
                sp, t = page_text(h)
                out["pages"][u] = {"title": sp.title.get_text(strip=True) if sp.title else "", "text": t[:20000]}
    return out


def html_signals(html):
    """Cheap tech-stack hints from raw HTML (CMS, tenant portal vendors)."""
    low = html.lower()
    vendors = ["casavi", "immoware", "domus", "facilioo", "allthings", "aareon", "wodis",
               "karthago", "haufe", "powerhaus", "everreal", "objego", "crem", "hausperfekt"]
    return {
        "wordpress": "wp-content" in low,
        "wix": "wixstatic" in low,
        "jimdo": "jimdo" in low,
        "portal_vendors": [v for v in vendors if v in low],
        "copyright_year": max([int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d\d)", low)] or [0]),
    }


def job(row):
    path = CACHE / f"{row['domain']}.json"
    if path.exists():
        return row["domain"], "cached"
    data = crawl(row["website"])
    data["company_id"] = int(row["id"])
    path.write_text(json.dumps(data, ensure_ascii=False))
    return row["domain"], data["status"]


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    CACHE.mkdir(parents=True, exist_ok=True)
    c = pd.read_csv(DATA / "companies.csv", dtype=str).fillna("")
    c = c[(c["domain"] != "") & (c.get("prefilter", "keep") == "keep")]
    if "--vdiv-first" in sys.argv:
        c = c.sort_values("vdiv_member", ascending=False)
    if limit:
        c = c.head(limit)
    workers = 8
    if "--retry-failed" in sys.argv:
        # Second pass for sites that failed: slower and more patient.
        global TIMEOUT
        TIMEOUT, workers = 25, 3
        for f in CACHE.glob("*.json"):
            if json.loads(f.read_text()).get("status") != "ok":
                f.unlink()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (dom, status) in enumerate(pool.map(job, c.to_dict("records")), 1):
            print(f"{i:4}/{len(c)} {status:12} {dom}", file=sys.stderr)


if __name__ == "__main__":
    main()
