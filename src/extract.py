"""LLM extraction with a check-and-fix loop and a hard budget cap.

For each crawled site:
  1. Build a compact excerpt (Impressum + relevant passages, ~1.5k tokens).
  2. Haiku extracts structured fields via a forced tool call. Every field
     must come with a verbatim evidence quote from the excerpt.
  3. validate() checks schema, plausibility and grounding: each quote and
     each person name must literally appear in the source text.
  4. On failure: retry once with Haiku, feeding the errors back. Still
     failing: escalate to Sonnet. Still failing: keep only the fields that
     passed and mark the row needs_review.
Every attempt is logged to data/extract_log.jsonl; results are cached in
data/extract/<domain>.json; spend is tracked in data/spend.json and the run
stops before any call that could exceed the cap.

Usage: python src/extract.py [--domains d1,d2] [--ids-file f] [--limit n]
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PAGES = DATA / "pages"
OUT = DATA / "extract"
LOG = DATA / "extract_log.jsonl"
SPEND = DATA / "spend.json"

BUDGET_USD = 1.00
FAST, STRONG = "claude-haiku-5-5", "claude-sonnet-5-5"
# USD per million tokens (input, output). Check console.anthropic.com/pricing;
# set conservatively so the cap errs on the safe side.
PRICE = {FAST: (1.0, 5.0), STRONG: (3.0, 15.0)}
MAX_OUT = 1200
EXCERPT_CHARS = 6000

KEYWORDS = (r"einheit|wohnung|objekt|weg\b|weg-|eigentümer|miet|gewerbe|heizkost|verbrauch|"
            r"ablesung|messdienst|portal|app\b|digital|karriere|stellen|wir suchen|team|"
            r"mitarbeiter|gegründet|seit \d|jahre|bestand|altbau|neubau|gründerzeit|verwalten")

TOOL = {
    "name": "record_company",
    "description": "Record facts about one Berlin property-management company. "
                   "Only use information stated in the excerpt. Every non-null fact needs "
                   "an evidence quote copied character-for-character from the excerpt.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_property_manager": {"type": "string", "enum": ["yes", "partly", "no", "unclear"],
                                    "description": "yes = manages buildings for owners (Miet/WEG/SEV); partly = mainly broker/developer with some management; no = other business"},
            "is_property_manager_reason": {"type": "string", "description": "One short sentence in English explaining the classification (free text)"},
            "is_property_manager_evidence": {"type": ["string", "null"], "description": "Exact quote(s) supporting the classification; separate several quotes with ' … '. Null if the excerpt has nothing quotable."},
            "decision_makers": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Person's full name exactly as written, without titles like Herr/Frau/Dipl.-Kfm."},
                "role": {"type": "string", "description": "e.g. Geschäftsführer, Inhaber, Prokurist"},
                "evidence": {"type": "string"}},
                "required": ["name", "role", "evidence"]}},
            "manages_weg": {"type": ["boolean", "null"]},
            "manages_rental": {"type": ["boolean", "null"]},
            "manages_commercial": {"type": ["boolean", "null"]},
            "portfolio_evidence": {"type": ["string", "null"]},
            "units_managed": {"type": ["integer", "null"], "description": "Number of units (Einheiten/Wohnungen) managed, only if a number is stated"},
            "units_evidence": {"type": ["string", "null"]},
            "employees": {"type": ["integer", "null"], "description": "Only if a headcount is stated"},
            "employees_evidence": {"type": ["string", "null"]},
            "founded_year": {"type": ["integer", "null"]},
            "founded_evidence": {"type": ["string", "null"]},
            "tenant_or_owner_portal": {"type": ["boolean", "null"], "description": "Online portal or app for tenants/owners"},
            "portal_evidence": {"type": ["string", "null"]},
            "hiring": {"type": ["boolean", "null"], "description": "Open job ads on the site"},
            "hiring_evidence": {"type": ["string", "null"]},
            "altbau_focus": {"type": ["boolean", "null"], "description": "Portfolio described as Altbau / Gründerzeit / pre-war buildings"},
            "altbau_evidence": {"type": ["string", "null"]},
        },
        "required": ["is_property_manager", "is_property_manager_reason", "is_property_manager_evidence", "decision_makers",
                     "manages_weg", "manages_rental", "manages_commercial", "units_managed",
                     "employees", "founded_year", "tenant_or_owner_portal", "hiring", "altbau_focus"],
    },
}

SYSTEM = ("You extract facts about German property-management companies (Hausverwaltungen) "
          "from website text for a sales-lead database. Be literal: if the text does not state "
          "something, return null. Never guess numbers. Evidence quotes must be short (under 150 "
          "characters) and copied exactly from the excerpt, including spelling and punctuation.\n"
          "Rules:\n"
          "- Portfolio flags: service names in menus or headings count as evidence "
          "(e.g. 'Mietverwaltung' -> rental, 'WEG-Verwaltung' or 'Wohnungseigentumsanlagen' -> WEG, "
          "'Gewerbeimmobilien' -> commercial). A trade licence (§34c GewO) is not evidence of any type.\n"
          "- units_managed: only a number that appears literally in the excerpt. Never add numbers up. "
          "If both a total ('über 3000 Einheiten') and a breakdown appear, use the total.\n"
          "- Decision makers: people named as Geschäftsführer, Inhaber, Vorstand or vertreten durch "
          "in the Impressum or team page. Not authors of quotes, not tenants, not webmasters.")

EVIDENCE = {"units_managed": "units_evidence", "employees": "employees_evidence",
            "founded_year": "founded_evidence", "tenant_or_owner_portal": "portal_evidence",
            "hiring": "hiring_evidence", "altbau_focus": "altbau_evidence"}


# ---------- budget ----------

def spent():
    return json.loads(SPEND.read_text())["usd"] if SPEND.exists() else 0.0


def add_spend(model, usage):
    pin, pout = PRICE[model]
    # Cache writes cost 1.25x input, cache reads 0.1x input.
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (usage.input_tokens + 1.25 * cw + 0.1 * cr) * pin / 1e6 + usage.output_tokens * pout / 1e6
    data = json.loads(SPEND.read_text()) if SPEND.exists() else {"usd": 0.0, "calls": 0, "in": 0, "out": 0}
    data["usd"] += cost
    data["calls"] += 1
    data["in"] += usage.input_tokens
    data["out"] += usage.output_tokens
    SPEND.write_text(json.dumps(data, indent=2))
    return cost


def worst_case(model, prompt_chars):
    pin, pout = PRICE[model]
    return (prompt_chars / 3 + 1500) * pin / 1e6 + MAX_OUT * pout / 1e6


# ---------- excerpt ----------

def norm(s):
    return " ".join(str(s).replace("­", "").replace("‍", "").split()).lower()


def excerpt(site):
    pages = site.get("pages", {})
    # The Impressum is found by URL or title; English sites call it "imprint" or "legal notice".
    is_imp = lambda u, p: re.search(r"impressum|imprint|legal-notice|rechtliches", u + " " + p.get("title", ""), re.I)
    imp = " ".join(p["text"] for u, p in pages.items() if is_imp(u, p))
    if imp:
        # "Impressum" also appears in menus and footers. Start at the first
        # mention that is followed by actual company details.
        low = imp.lower()
        starts = [m.start() for m in re.finditer("impressum", low)] or [0]
        details = re.compile(r"geschäftsf|inhaber|vertreten|§\s*5|tmg|gmbh|e\.\s?k|handelsregister", re.I)
        i = next((s for s in starts if details.search(imp[s: s + 600])), starts[0])
        imp = imp[max(0, i - 50): max(0, i - 50) + 2500]
        # Drop the legal boilerplate that follows the company details.
        # Cut only after the company details: cookie banners mention "Datenschutz" before them.
        strong = list(re.finditer(r"geschäftsf|vertreten|inhaber|handelsregister|registergericht|"
                                  r"ust-id|umsatzsteuer", imp, re.I))
        after = strong[-1].end() if strong else 200
        cut = re.compile(r"haftungsausschluss|haftung für inhalte|disclaimer|streitschlichtung|"
                         r"verbraucherstreit|datenschutzerkl", re.I).search(imp, after)
        if cut:
            imp = imp[: cut.start()]
    rest = " ".join(p["text"] for u, p in pages.items()
                    if not is_imp(u, p) and "datenschutz" not in u.lower())
    spans = []
    for m in re.finditer(KEYWORDS, rest, re.I):
        a, b = max(0, m.start() - 150), min(len(rest), m.end() + 200)
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], b)
        else:
            spans.append((a, b))
    body = " … ".join(rest[a:b] for a, b in spans)
    title = next(iter(pages.values()), {}).get("title", "")
    text = f"TITLE: {title}\nIMPRESSUM: {imp}\nPAGES: {body}"
    return text[:EXCERPT_CHARS]


# ---------- checks ----------

def validate(out, source):
    """Return a list of problems. Empty list = passed."""
    src = norm(source)
    errs = []

    def grounded(quote, field):
        # Several quotes may be joined with an ellipsis; each part must be verbatim.
        for part in re.split(r"\s*(?:…|\.\.\.)\s*", quote or ""):
            if len(part) >= 8 and norm(part) not in src:
                errs.append(f"{field}: evidence quote not found verbatim in the excerpt: {part[:80]!r}")

    grounded(out.get("is_property_manager_evidence"), "is_property_manager")
    if out.get("is_property_manager") in ("yes", "partly", "no") and not out.get("is_property_manager_evidence"):
        errs.append("is_property_manager: a yes/partly/no answer needs a quote; use 'unclear' if there is none")
    for dm in out.get("decision_makers") or []:
        name = (dm.get("name") or "").strip()
        if not name:
            errs.append("decision_makers: empty name")
            continue
        grounded(dm.get("evidence"), f"decision_maker {name}")
        # Allow names split by titles in the source: every name token must appear.
        missing = [t for t in re.split(r"\s+", name) if len(t) > 1 and norm(t) not in src]
        if missing:
            errs.append(f"decision_maker {name}: tokens {missing} not in the excerpt")
    for field, ev in EVIDENCE.items():
        if out.get(field) not in (None, False):
            if not out.get(ev):
                errs.append(f"{field}: value given without evidence quote")
            else:
                grounded(out.get(ev), field)
    u = out.get("units_managed")
    if u is not None and not 1 <= u <= 200_000:
        errs.append(f"units_managed: implausible value {u}")
    if u is not None and out.get("units_evidence") and str(u) not in re.sub(r"[.\s]", "", out["units_evidence"]):
        errs.append(f"units_managed: number {u} does not appear in its evidence quote")
    y = out.get("founded_year")
    if y is not None and not 1850 <= y <= datetime.now().year:
        errs.append(f"founded_year: implausible value {y}")
    if out.get("employees") is not None and not 1 <= out["employees"] <= 5000:
        errs.append(f"employees: implausible value {out['employees']}")
    return errs


def strip_failed(out, errs):
    """Keep only fields whose checks passed (used when all retries fail)."""
    bad = " ".join(errs)
    clean = dict(out)
    clean["decision_makers"] = [d for d in out.get("decision_makers") or [] if d.get("name") and d["name"] not in bad]
    for field, ev in EVIDENCE.items():
        if field in bad:
            clean[field], clean[ev] = None, None
    return clean


# ---------- model call ----------

def call(client, model, text, feedback=None):
    msgs = [{"role": "user", "content": f"<excerpt>\n{text}\n</excerpt>"}]
    if feedback:
        msgs[0]["content"] += ("\n\nA previous extraction failed these automatic checks. Fix them: "
                               "copy quotes exactly from the excerpt or set the field to null.\n- "
                               + "\n- ".join(feedback))
    # The system prompt and tool schema are identical on every call, so cache them.
    tool = dict(TOOL, cache_control={"type": "ephemeral"})
    # Haiku accepts a forced tool call; Sonnet 5.5 only accepts "auto", so ask for the call in the prompt.
    choice = {"type": "tool", "name": "record_company"} if model == FAST else {"type": "auto"}
    if model != FAST:
        msgs[0]["content"] += "\n\nRespond only by calling the record_company tool."
    r = client.messages.create(model=model, max_tokens=MAX_OUT, system=SYSTEM, tools=[tool],
                               tool_choice=choice, messages=msgs)
    cost = add_spend(model, r.usage)
    if r.stop_reason == "max_tokens":
        raise TruncatedOutput(cost, r.usage)
    block = next((b for b in r.content if b.type == "tool_use"), None)
    if block is None:
        return {}, cost, r.usage
    return block.input, cost, r.usage


class TruncatedOutput(Exception):
    def __init__(self, cost, usage):
        super().__init__("output hit max_tokens")
        self.cost, self.usage = cost, usage


def log(entry):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def process(client, domain):
    cache = OUT / f"{domain}.json"
    if cache.exists():
        return json.loads(cache.read_text()), "cached"
    site = json.loads((PAGES / f"{domain}.json").read_text())
    if site.get("status") != "ok":
        return None, "unreachable"
    text = excerpt(site)
    plan = [(FAST, "first pass"), (FAST, "retry with feedback"), (STRONG, "escalated")]
    errs, out = None, None
    for attempt, (model, label) in enumerate(plan, 1):
        if spent() + worst_case(model, len(text)) > BUDGET_USD:
            raise SystemExit(f"budget cap ${BUDGET_USD:.2f} reached (spent ${spent():.4f}); stopping before {domain}")
        for backoff in (2, 8, 30):
            try:
                out, cost, usage = call(client, model, text, errs)
                break
            except TruncatedOutput as t:
                out, cost, usage = {}, t.cost, t.usage
                break
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError):
                time.sleep(backoff)
        else:
            return None, "api_error"
        errs = validate(out, text) if out else ["no usable tool call (cut off or missing); call record_company with short quotes"]
        log({"ts": datetime.now(timezone.utc).isoformat(), "domain": domain, "attempt": attempt,
             "model": model, "step": label, "passed": not errs, "errors": errs,
             "in_tokens": usage.input_tokens, "out_tokens": usage.output_tokens,
             "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0, "usd": round(cost, 6)})
        if not errs:
            status = "ok" if attempt == 1 else f"fixed_on_attempt_{attempt}"
            break
    else:
        out = strip_failed(out, errs)
        status = "needs_review"
    result = {"domain": domain, "status": status, "model": model, "attempts": attempt,
              "final_errors": errs, "fields": out, "excerpt_chars": len(text)}
    OUT.mkdir(exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result, status


def load_key():
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("ANTHROPIC_API_KEY missing in .env")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domains", default="")
    ap.add_argument("--ids-file", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.domains:
        domains = [d for d in args.domains.split(",") if d]
    elif args.ids_file:
        domains = [l.strip() for l in Path(args.ids_file).read_text().splitlines() if l.strip()]
    else:
        domains = sorted(p.stem for p in PAGES.glob("*.json"))
    if args.limit:
        domains = domains[: args.limit]
    client = anthropic.Anthropic(api_key=load_key())
    counts = {}
    for i, d in enumerate(domains, 1):
        _, status = process(client, d)
        counts[status] = counts.get(status, 0) + 1
        print(f"{i:4}/{len(domains)} {status:20} {d}  (spent ${spent():.4f})", file=sys.stderr)
    print(json.dumps(counts), file=sys.stderr)


if __name__ == "__main__":
    main()
