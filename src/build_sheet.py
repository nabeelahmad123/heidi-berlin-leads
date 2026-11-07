"""Build the deliverable workbook (import into Google Sheets: File > Import > Upload).

Tabs:
  Top leads        - LLM-verified property managers, best first, with evidence
  All leads        - every Berlin company found, scored
  KPI definitions  - what each KPI means, weight, why it matters for Heidi, coverage
  Validation       - accuracy vs the hand-labelled set, check-loop statistics
  Sources          - where the data came from and how much each source added
Output: output/heidi_berlin_hausverwaltungen.xlsx
"""
import json
import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import WEIGHTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "output" / "heidi_berlin_hausverwaltungen.xlsx"
HEAD = PatternFill("solid", fgColor="1F3A5F")

KPI_INFO = [
    ("size_fit", "Size class (S <300, M 300-2,000, L 2,000-10,000, XL >10,000 units)",
     "M scores highest: enough meters to matter, small enough to decide fast. XL firms often run in-house metering.",
     "units stated on the website, else headcount"),
    ("rental_focus", "Manages rental property (Mietverwaltung) vs only WEG",
     "In rental management the landlord decides on the metering provider. A WEG switch needs an owners' vote, so the sales cycle is longer.",
     "services named on the website"),
    ("vdiv_member", "Member of VDIV Berlin-Brandenburg",
     "Heidi is an official VDIV Kooperationspartner, so members can be approached through a channel they already trust.",
     "public VDIV-BB member list"),
    ("reachable_dm", "Named decision maker (+ personal email)",
     "A named Geschäftsführer/Inhaber turns a cold lead into a person to call. Personal email scores higher than info@.",
     "Impressum (LLM-extracted, verbatim-checked) and VDIV contact person"),
    ("digital_ready", "Tenant/owner portal or a current website",
     "Firms already using software adopt Heidi's dashboard, uVI and ERP interfaces more easily.",
     "portal mentions, copyright year on the site"),
    ("workload", "Hiring on the website",
     "VDIV reports most managers feel overloaded. A firm that is hiring is stretched, and automated billing takes work off the team.",
     "job/career pages"),
    ("altbau_stock", "Old building stock (Altbau, Gründerzeit)",
     "Older buildings are more likely to still have non-remote-readable meters, which must be replaced by 31 Dec 2026 (HeizkostenV §5).",
     "portfolio descriptions on the website"),
]


def write(ws, df, widths=None):
    ws.append(list(df.columns))
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in df.itertuples(index=False):
        ws.append([("" if pd.isna(v) else v) for v in row])
    for i, col in enumerate(df.columns, 1):
        w = (widths or {}).get(col) or min(50, max(10, int(df[col].astype(str).str.len().quantile(0.9)) + 2 if len(df) else 12, len(col) + 2))
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions


def evidence(domain):
    f = DATA / "extract" / f"{domain}.json"
    if not domain or not f.exists():
        return "", ""
    fields = json.loads(f.read_text())["fields"]
    ev = [fields.get(k) for k in ("units_evidence", "portfolio_evidence", "altbau_evidence", "portal_evidence")]
    dm = " | ".join(d.get("evidence", "") for d in fields.get("decision_makers") or [])
    return " | ".join(e for e in ev if e)[:400], dm[:300]


def main():
    leads = pd.read_csv(DATA / "leads.csv", dtype=str).fillna("")
    leads["lead_score"] = pd.to_numeric(leads["lead_score"])
    leads["first_website"] = leads["website"]
    base = ["name", "lead_score", "confidence", "top_reasons", "size_class", "units_managed", "employees",
            "manages_rental", "manages_weg", "tenant_portal", "hiring", "altbau_focus", "founded",
            "decision_makers", "vdiv_contact", "email", "email_type", "phone", "street", "plz",
            "district", "website", "vdiv_member", "is_property_manager", "data_basis", "sources", "source_urls"]

    top = leads[leads["data_basis"].isin(["LLM-verified", "LLM + human review"])].copy()
    top = top[top["is_property_manager"].isin(["yes", "partly"])]
    ev = top["domain"].map(evidence)
    top["evidence_on_website"] = [e[0] for e in ev]
    top["decision_maker_evidence"] = [e[1] for e in ev]
    top = top.sort_values(["lead_score", "confidence"], ascending=False)
    op = DATA / "openers.csv"
    if op.exists():
        o = pd.read_csv(op, dtype=str).fillna("")
        o["key"] = o["name"].str.lower().str.replace(r"[^a-z0-9]", "", regex=True).str[:20]
        top["key"] = top["name"].str.lower().str.replace(r"[^a-z0-9]", "", regex=True).str[:20]
        top = top.merge(o[["key", "angle", "opener_de"]], on="key", how="left").drop(columns="key").fillna("")
        top = top.rename(columns={"angle": "sales_angle", "opener_de": "call_opener_de"})

    wb = Workbook()
    ws = wb.active
    ws.title = "Top leads"
    extra = [c for c in ("sales_angle", "call_opener_de") if c in top]
    cols = base[:4] + extra + base[4:] + ["evidence_on_website", "decision_maker_evidence"]
    write(ws, top[cols],
          {"name": 38, "top_reasons": 34, "sales_angle": 40, "call_opener_de": 70, "decision_makers": 40,
           "evidence_on_website": 60, "decision_maker_evidence": 50, "source_urls": 40})
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    write(wb.create_sheet("All leads"), leads.sort_values(["lead_score", "confidence"], ascending=False)[base],
          {"name": 38, "top_reasons": 34, "source_urls": 40})

    total = len(leads)
    rows = []
    cov = {"size_fit": leads["size_class"] != "", "rental_focus": (leads["manages_rental"] != "") | (leads["manages_weg"] != ""),
           "vdiv_member": leads["vdiv_member"] != "", "reachable_dm": (leads["decision_makers"] != "") | (leads["vdiv_contact"] != ""),
           "digital_ready": leads["tenant_portal"] != "", "workload": leads["hiring"] != "", "altbau_stock": leads["altbau_focus"] != ""}
    for key, what, why, source in KPI_INFO:
        rows.append({"KPI": key, "What it measures": what, "Weight (of 100)": WEIGHTS[key],
                     "Why it matters for Heidi": why, "Data source": source,
                     "Coverage (all leads)": f"{cov[key].mean():.0%}",
                     "Coverage (top leads)": f"{cov[key][leads['data_basis'] == 'LLM-verified'].mean():.0%}"})
    rows.append({"KPI": "missing data", "What it measures": "How unknown values are scored",
                 "Weight (of 100)": "", "Why it matters for Heidi": "Unknown = neutral 0.5, not 0, so thin websites are not punished. The 'confidence' column shows how much of each score rests on observed data.",
                 "Data source": "", "Coverage (all leads)": "", "Coverage (top leads)": ""})
    rows.append({"KPI": "not used", "What it measures": "Current metering provider (ista, Techem, ...)",
                 "Weight (of 100)": "", "Why it matters for Heidi": "Would be the strongest signal, but 0 of 18 pilot websites name their provider. Recommended as the first question on the sales call instead.",
                 "Data source": "", "Coverage (all leads)": "", "Coverage (top leads)": ""})
    write(wb.create_sheet("KPI definitions"), pd.DataFrame(rows),
          {"What it measures": 40, "Why it matters for Heidi": 70, "Data source": 36})

    val = []
    for v in ("v1", "v2", "v3"):
        f = DATA / f"eval_report_{v}.json"
        if f.exists():
            val.append({"version": v, **json.loads(f.read_text())["summary"]})
    log = [json.loads(l) for l in (DATA / "extract_log.jsonl").read_text().splitlines()]
    run = pd.DataFrame(log)
    stats = pd.DataFrame([{
        "LLM calls": len(run),
        "sites processed": run["domain"].nunique(),
        "passed first check": int(((run["attempt"] == 1) & run["passed"]).sum()),
        "fixed by retry with feedback": int(((run["attempt"] == 2) & run["passed"]).sum()),
        "fixed by escalation to stronger model": int(((run["attempt"] == 3) & run["passed"]).sum()),
    }])
    ws = wb.create_sheet("Validation")
    write(ws, pd.DataFrame(val) if val else pd.DataFrame([{"note": "no eval yet"}]))
    ws.append([])
    ws.append(["Check-and-fix loop over all runs"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    ws.append(list(stats.columns))
    ws.append(list(stats.iloc[0]))

    spot = DATA / "spotcheck.csv"
    if spot.exists():
        ws.append([])
        ws.append(["Independent spot check: random sample of top leads re-read on the live website"])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        sc = pd.read_csv(spot, dtype=str).fillna("")
        ws.append(list(sc.columns))
        for row in sc.itertuples(index=False):
            ws.append(list(row))

    corr = DATA / "corrections.csv"
    if corr.exists():
        ws.append([])
        ws.append(["Human review: corrections applied on top of the model output"])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        cr = pd.read_csv(corr, dtype=str).fillna("")
        ws.append(list(cr.columns))
        for row in cr.itertuples(index=False):
            ws.append(list(row))

    rep = json.loads((DATA / "merge_report.json").read_text())
    src = pd.DataFrame([{"source": k, "listings": rep["listings_per_source"].get(k, ""),
                         "companies after dedupe": v} for k, v in rep["companies_per_source"].items()])
    ws = wb.create_sheet("Sources")
    write(ws, src)
    ws.append([])
    for k, v in rep["pairwise_overlap"].items():
        ws.append([f"overlap {k}", v])
    ws.append(["unique companies", rep["unique_companies"]])

    OUT.parent.mkdir(exist_ok=True)
    wb.save(OUT)
    print(f"{OUT}  top leads: {len(top)}, all leads: {len(leads)}", file=sys.stderr)


if __name__ == "__main__":
    main()
