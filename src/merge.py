"""Merge all source lists into one deduplicated company table.

Two listings are the same company if they share a website domain, a phone
number or a normalised name. Matches are clustered with union-find, so A~B
and B~C puts A, B and C in one company. Within a cluster the first non-empty
value wins, in source priority order (VDIV is member-maintained).
Output: data/companies.csv, data/merge_report.json
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"
SOURCES = [  # priority order
    ("VDIV", "vdiv_berlin.csv"),
    ("Gelbe Seiten", "gelbeseiten_berlin.csv"),
    ("Das Örtliche", "oertliche_berlin.csv"),
    ("11880", "11880_berlin.csv"),
]
FIELDS = ["name", "street", "plz", "district", "phone", "website", "domain",
          "contact_person", "email", "lat", "lon"]
LEGAL = r"\b(gmbh|mbh|ug|ag|kg|ohg|gbr|e\s?k|e\s?kfm|e\s?v|co|haftungsbeschränkt|inh)\b"
# Directory hosts and shared platforms must not merge unrelated companies.
SHARED_DOMAINS = {"", "facebook.com", "immobilienscout24.de", "jimdo.com", "wixsite.com",
                  "business.site", "t-online.de", "gelbeseiten.de", "11880.com"}


def domain(url):
    u = str(url).lower().strip()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    return u.split("/")[0].strip()


def name_key(name):
    n = str(name).lower().replace("&amp;", " ").replace("&#124;", " ").replace("&", " ")
    n = n.replace(".", " ")
    n = re.sub(LEGAL, " ", n)
    n = re.sub(r"[^a-zäöüß0-9 ]", " ", n)
    return " ".join(n.split())


def phone_key(phone):
    digits = re.sub(r"\D", "", str(phone))
    if digits.startswith("49"):
        digits = "0" + digits[2:]
    return digits if len(digits) >= 7 else ""


class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def load():
    frames = []
    for label, file in SOURCES:
        path = DATA / file
        if not path.exists():
            print(f"skip {label}: {file} missing", file=sys.stderr)
            continue
        df = pd.read_csv(path, dtype=str).fillna("")
        df["source"] = label
        for col in FIELDS + ["source_url", "gs_rating", "gs_reviews"]:
            if col not in df:
                df[col] = ""
        df["domain"] = df["website"].map(domain)
        frames.append(df)
    rows = pd.concat(frames, ignore_index=True)
    rows["name"] = rows["name"].str.replace("&amp;", "&").str.replace("&#124;", "|")
    rows["name_key"] = rows["name"].map(name_key)
    rows["phone_key"] = rows["phone"].map(phone_key)
    return rows


def cluster(rows):
    uf = UnionFind(len(rows))
    for key in ("domain", "phone_key", "name_key"):
        first = {}
        for i, value in rows[key].items():
            if not value or (key == "domain" and value in SHARED_DOMAINS):
                continue
            if value in first:
                uf.union(first[value], i)
            else:
                first[value] = i
    rows["cluster"] = [uf.find(i) for i in range(len(rows))]
    return rows


def combine(group):
    out = {}
    for col in FIELDS:
        vals = [v for v in group[col] if v]
        out[col] = vals[0] if vals else ""
    out["sources"] = " + ".join(dict.fromkeys(group["source"]))
    out["n_sources"] = group["source"].nunique()
    out["vdiv_member"] = "yes" if (group["source"] == "VDIV").any() else "no"
    out["source_urls"] = " | ".join(dict.fromkeys(u for u in group["source_url"] if u))
    out["gs_rating"] = next((v for v in group["gs_rating"] if v), "")
    out["gs_reviews"] = next((v for v in group["gs_reviews"] if v), "")
    return out


def main():
    rows = cluster(load())
    rows["_order"] = range(len(rows))
    companies = [combine(g.sort_values("_order")) for _, g in rows.groupby("cluster", sort=True)]
    out = pd.DataFrame(companies)
    out.insert(0, "id", range(1, len(out) + 1))
    out.to_csv(DATA / "companies.csv", index=False)

    per_source = rows.groupby("source")["cluster"].nunique().to_dict()
    overlap = {}
    labels = [l for l, _ in SOURCES if l in per_source]
    sets = {l: set(rows.loc[rows["source"] == l, "cluster"]) for l in labels}
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            overlap[f"{a} & {b}"] = len(sets[a] & sets[b])
    report = {
        "listings_per_source": rows["source"].value_counts().to_dict(),
        "companies_per_source": per_source,
        "pairwise_overlap": overlap,
        "unique_companies": len(out),
        "with_website": int((out["domain"] != "").sum()),
        "with_contact_person": int((out["contact_person"] != "").sum()),
        "in_2plus_sources": int((out["n_sources"] >= 2).sum()),
    }
    (DATA / "merge_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
