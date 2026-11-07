"""Turn company data into KPI columns and a 0-100 lead score for Heidi Systems.

Each KPI is scored 0..1. Missing data scores a neutral 0.5 instead of 0, so a
company is not punished for a thin website; `confidence` reports the share of
KPI weight that rests on observed data. LLM fields (data/extract/) override the
free keyword signals when present and verified.

Weights are assumptions (no Heidi win/loss data yet) and live in WEIGHTS so they
can be changed in one place; the sheet repeats them in the KPI-definitions tab.
Output: data/leads.csv
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"

WEIGHTS = {
    "size_fit": 20,          # 50-2,000 units: big enough to matter, small enough to switch fast
    "rental_focus": 15,      # rental: landlord decides; WEG needs an owners' vote (slower)
    "vdiv_member": 15,       # Heidi is a VDIV Kooperationspartner: warm channel
    "reachable_dm": 15,      # named decision maker (+ personal email)
    "digital_ready": 10,     # portal/app or modern site: used to software, easier switch
    "workload": 10,          # hiring = stretched team; automated billing removes work
    "altbau_stock": 15,      # old building stock: older meters, more 2026 retrofit pressure
}
assert sum(WEIGHTS.values()) == 100

GENERIC_MAIL = re.compile(r"^(info|kontakt|mail|office|post|service|hausverwaltung|verwaltung|zentrale|"
                          r"team|buero|büro|hv|immobilien|anfrage|empfang|email|hallo|support|webmaster)\b", re.I)


def site_text(domain):
    f = DATA / "pages" / f"{domain}.json"
    if not domain or not f.exists():
        return None, None
    site = json.loads(f.read_text())
    if site.get("status") != "ok":
        return "", site
    return " ".join(p["text"] for p in site["pages"].values()).lower(), site


def load_corrections():
    f = DATA / "corrections.csv"
    if not f.exists():
        return {}
    out = {}
    for r in pd.read_csv(f, dtype=str).fillna("").to_dict("records"):
        out.setdefault(r["domain"], []).append(r)
    return out


CORRECTIONS = load_corrections()


def llm(domain):
    """LLM result for a domain with human-review corrections applied on top."""
    f = DATA / "extract" / f"{domain}.json"
    if not domain or not f.exists():
        return None
    ex = json.loads(f.read_text())
    for c in CORRECTIONS.get(domain, []):
        if c["field"] == "status":
            ex["status"] = c["new_value"]
        elif c["field"] == "units_managed":
            ex["fields"]["units_managed"] = int(c["new_value"])
        elif c["field"] == "decision_makers":
            people = []
            for part in c["new_value"].split(";"):
                name, _, role = part.strip().partition(" (")
                people.append({"name": name.strip(), "role": role.rstrip(")"), "evidence": c["evidence"]})
            ex["fields"]["decision_makers"] = people
        else:
            ex["fields"][c["field"]] = c["new_value"]
        ex["status"] = "reviewed"
    return ex


def keyword_signals(text, site):
    """Free signals from crawled text. None = not observable."""
    if text is None:
        return {}
    units = None
    m = re.search(r"(?:über|rund|ca\.?|mehr als|etwa)?\s*(\d{1,3}(?:\.\d{3})+|\d{2,5})\s*\+?\s*"
                  r"(?:verwaltete\s+)?(?:wohn- und gewerbeeinheiten|einheiten|wohneinheiten|wohnungen)", text)
    if m:
        units = int(m.group(1).replace(".", ""))
    year = (site or {}).get("html_signals", {}).get("copyright_year") or None
    return {
        "weg": bool(re.search(r"weg-verwaltung|weg verwaltung|wohnungseigentümergemeinschaft|wohnungseigentumsanlage", text)) or None,
        "rental": bool(re.search(r"mietverwaltung|mietshausverwaltung|mietshäuser", text)) or None,
        "portal": bool(re.search(r"mieterportal|kundenportal|eigentümerportal|serviceportal|mieter-app|web-app|casavi|immoware|facilioo", text)) or None,
        "hiring": bool(re.search(r"stellenangebot|wir suchen|karriere|jobs\b|stellenausschreibung", text)) or None,
        "altbau": bool(re.search(r"altbau|gründerzeit|jahrhundertwende", text)) or None,
        "units": units,
        "site_year": year,
    }


def kpis(row):
    text, site = site_text(row["domain"])
    kw = keyword_signals(text, site)
    ex = llm(row["domain"])
    f = (ex or {}).get("fields", {})
    verified = ex is not None and ex.get("status") != "needs_review"
    reviewed = ex is not None and ex.get("status") == "reviewed"

    units = f.get("units_managed") if ex else kw.get("units")
    employees = f.get("employees") if ex else None
    weg = f.get("manages_weg") if ex else kw.get("weg")
    rental = f.get("manages_rental") if ex else kw.get("rental")
    portal = f.get("tenant_or_owner_portal") if ex else kw.get("portal")
    hiring = f.get("hiring") if ex else kw.get("hiring")
    altbau = f.get("altbau_focus") if ex else kw.get("altbau")
    dms = [d for d in f.get("decision_makers") or [] if d.get("name")]
    dm_names = "; ".join(f"{d['name']} ({d.get('role', '')})" for d in dms)
    contact = row["contact_person"]
    personal_mail = bool(row["email"]) and not GENERIC_MAIL.match(row["email"])

    # ---- size class ----
    if units:
        size_class = "S" if units < 300 else "M" if units <= 2000 else "L" if units <= 10000 else "XL"
    elif employees:
        size_class = "S" if employees <= 3 else "M" if employees <= 20 else "L"
    else:
        size_class = ""
    size_fit = {"S": 0.6, "M": 1.0, "L": 0.7, "XL": 0.3}.get(size_class)

    # ---- portfolio ----
    if rental:
        rental_focus = 1.0 if not weg else 0.85
    elif weg:
        rental_focus = 0.55
    else:
        rental_focus = None

    # ---- digital ----
    year = kw.get("site_year")
    if portal:
        digital = 1.0
    elif year:
        digital = 0.7 if year >= datetime.now().year - 2 else 0.3 if year <= 2018 else 0.5
    else:
        digital = None

    scores = {
        "size_fit": size_fit,
        "rental_focus": rental_focus,
        "vdiv_member": 1.0 if row["vdiv_member"] == "yes" else 0.0,
        "reachable_dm": 1.0 if (contact or dms) and personal_mail else 0.75 if (contact or dms) else 0.0,
        "digital_ready": digital,
        "workload": 1.0 if hiring else None,
        "altbau_stock": 1.0 if altbau else None,
    }
    observed = sum(WEIGHTS[k] for k, v in scores.items() if v is not None)
    score = sum(WEIGHTS[k] * (0.5 if v is None else v) for k, v in scores.items())

    pm = f.get("is_property_manager", "") if ex else ""
    if text is None:
        website_status = "no website"
    elif text == "":
        website_status = "unreachable"
    else:
        website_status = "ok"
    reasons = [k for k, v in sorted(scores.items(), key=lambda kv: -WEIGHTS[kv[0]] * (kv[1] or 0)) if v and v >= 0.75][:3]
    return {
        "lead_score": round(score),
        "confidence": round(observed / 100, 2),
        "is_property_manager": pm or ("excluded" if row.get("prefilter", "keep") != "keep" else "not checked"),
        "size_class": size_class,
        "units_managed": units or "",
        "employees": employees or "",
        "manages_rental": "" if rental is None else ("yes" if rental else "no"),
        "manages_weg": "" if weg is None else ("yes" if weg else "no"),
        "tenant_portal": "" if portal is None else ("yes" if portal else "no"),
        "hiring": "yes" if hiring else "",
        "altbau_focus": "yes" if altbau else "",
        "founded": f.get("founded_year") or "",
        "decision_makers": dm_names,
        "vdiv_contact": contact,
        "email": row["email"],
        "email_type": "" if not row["email"] else ("personal" if personal_mail else "generic"),
        "website_status": website_status,
        "data_basis": "LLM + human review" if reviewed else "LLM-verified" if verified else "LLM (needs review)" if ex else "keyword rules" if text else "directory only",
        "top_reasons": ", ".join(reasons),
    }


def main():
    c = pd.read_csv(DATA / "companies.csv", dtype=str).fillna("")
    k = pd.DataFrame([kpis(r) for r in c.to_dict("records")])
    leads = pd.concat([c, k], axis=1)
    leads = leads[leads["prefilter"] == "keep"]
    leads = leads[~leads["is_property_manager"].isin(["no"])]
    leads = leads.sort_values(["lead_score", "confidence"], ascending=False)
    leads.to_csv(DATA / "leads.csv", index=False)
    print(f"{len(leads)} leads; score range {leads.lead_score.min()}-{leads.lead_score.max()}; "
          f"median confidence {leads.confidence.median()}", file=sys.stderr)
    print(leads["data_basis"].value_counts().to_string(), file=sys.stderr)


if __name__ == "__main__":
    main()
