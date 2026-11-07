"""MCP server: lets a sales rep query the Berlin lead database from Claude.

Example questions in Claude Desktop:
  "Top 10 rental-focused managers in Charlottenburg with a named Geschäftsführer"
  "Why does Nymwegen score 86? Show the evidence."
  "Which VDIV members manage 500-2,000 units and have no tenant portal?"

Every answer carries the source URLs and verbatim website evidence, so the
model can cite instead of guess. Read-only: the server never changes data.

Run:  uv run --python 3.12 --with "mcp<2" --with pandas python src/mcp_server.py
"""
import csv
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

DATA = Path(__file__).resolve().parent.parent / "data"
mcp = FastMCP("heidi-berlin-leads")

SUMMARY_FIELDS = ["id", "name", "lead_score", "confidence", "size_class", "units_managed",
                  "manages_rental", "manages_weg", "tenant_portal", "decision_makers",
                  "vdiv_contact", "email_type", "phone", "district", "plz", "website",
                  "vdiv_member", "data_basis", "top_reasons"]


def load():
    with (DATA / "leads.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    openers = {}
    op = DATA / "openers.csv"
    if op.exists():
        with op.open(encoding="utf-8") as f:
            openers = {r["name"]: r for r in csv.DictReader(f)}
    for r in rows:
        r["lead_score"] = int(r["lead_score"])
        r["units"] = int(float(r["units_managed"])) if r["units_managed"] else None
        o = openers.get(r["name"])
        r["call_opener_de"] = o["opener_de"] if o else ""
    return rows


def evidence(domain):
    f = DATA / "extract" / f"{domain}.json"
    if not domain or not f.exists():
        return {}
    fields = json.loads(f.read_text())["fields"]
    return {k: v for k, v in fields.items() if k.endswith("evidence") and v} | {
        "decision_makers": fields.get("decision_makers") or []}


@mcp.tool()
def search_leads(district: str = "", min_units: int = 0, max_units: int = 0,
                 rental: bool = False, weg: bool = False, vdiv_only: bool = False,
                 needs_decision_maker: bool = False, no_portal: bool = False,
                 verified_only: bool = True, min_score: int = 0, limit: int = 20) -> list[dict]:
    """Find Berlin property managers (Hausverwaltungen), best lead score first.

    district: part of the district name or a postcode prefix, e.g. "Charlottenburg" or "107".
    min_units / max_units: filter on stated managed units (0 = no limit). Companies with
    unknown units are excluded when a unit filter is set.
    rental / weg: require rental management or WEG management.
    vdiv_only: only VDIV members (Heidi is a VDIV partner).
    needs_decision_maker: only leads with a named Geschäftsführer/Inhaber or VDIV contact.
    no_portal: only companies without a tenant/owner portal (less digital today).
    verified_only: only leads whose website was checked by the LLM pipeline (default).
    """
    out = []
    for r in load():
        if verified_only and not r["data_basis"].startswith("LLM"):
            continue
        if district and district.lower() not in r["district"].lower() and not r["plz"].startswith(district):
            continue
        if (min_units or max_units) and r["units"] is None:
            continue
        if min_units and r["units"] < min_units:
            continue
        if max_units and r["units"] > max_units:
            continue
        if rental and r["manages_rental"] != "yes":
            continue
        if weg and r["manages_weg"] != "yes":
            continue
        if vdiv_only and r["vdiv_member"] != "yes":
            continue
        if needs_decision_maker and not (r["decision_makers"] or r["vdiv_contact"]):
            continue
        if no_portal and r["tenant_portal"] == "yes":
            continue
        if r["lead_score"] < min_score:
            continue
        out.append({k: r[k] for k in SUMMARY_FIELDS})
    out.sort(key=lambda r: (-r["lead_score"], -float(r["confidence"] or 0)))
    return out[:limit]


@mcp.tool()
def get_lead(name_or_id: str) -> dict:
    """Full record for one company: all KPIs, contacts, source URLs, verbatim website
    evidence and a suggested German call opener (if one exists). Matches id or name part."""
    rows = load()
    q = name_or_id.strip().lower()
    hit = next((r for r in rows if r["id"] == q), None) or next((r for r in rows if q in r["name"].lower()), None)
    if not hit:
        return {"error": f"no company matching {name_or_id!r}"}
    return hit | {"website_evidence": evidence(hit["domain"])}


@mcp.tool()
def explain_score(name_or_id: str) -> dict:
    """Break a lead score down into its KPIs and weights, so a rep can see why a lead ranks where it does."""
    from score import WEIGHTS  # local import keeps the server start fast
    lead = get_lead(name_or_id)
    if "error" in lead:
        return lead
    return {"name": lead["name"], "lead_score": lead["lead_score"], "confidence": lead["confidence"],
            "weights": WEIGHTS, "strongest_reasons": lead["top_reasons"],
            "kpi_values": {k: lead[k] for k in ("size_class", "units_managed", "manages_rental", "manages_weg",
                                                "vdiv_member", "decision_makers", "tenant_portal", "hiring",
                                                "altbau_focus")},
            "note": "Unknown KPIs score a neutral 0.5; confidence = share of weight based on observed data."}


@mcp.tool()
def market_overview() -> dict:
    """Counts across the whole Berlin list: by size class, district, data basis and portfolio type."""
    rows = load()

    def count(key):
        c = {}
        for r in rows:
            c[r[key] or "unknown"] = c.get(r[key] or "unknown", 0) + 1
        return dict(sorted(c.items(), key=lambda kv: -kv[1]))

    report = json.loads((DATA / "merge_report.json").read_text())
    return {"companies": len(rows), "by_data_basis": count("data_basis"), "by_size_class": count("size_class"),
            "top_districts": dict(list(count("district").items())[:12]),
            "manages_rental": count("manages_rental"), "sources": report["companies_per_source"],
            "estimated_berlin_market": "about 3,800-4,200 (capture-recapture estimate, VDIV vs directories)"}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    mcp.run()
