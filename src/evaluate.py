"""Score LLM extraction against the hand-labelled pilot set (data/pilot_labels.csv).

Metrics:
  - property-manager class accuracy (yes / partly / no)
  - decision-maker recall: labelled people found (surname match)
  - decision-maker precision: extracted people that are in the labels
  - units: exact match where the label has a number
  - portfolio: WEG / rental flags agree with the label
Writes data/eval_report.json and prints a per-site table.
"""
import json
import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data"


def surnames(cell):
    people = [p.strip() for p in str(cell).split(";") if p.strip() and "(" not in p]
    return {p.split()[-1].lower() for p in people}


def main():
    labels = pd.read_csv(DATA / "pilot_labels.csv", dtype=str).fillna("")
    rows, tp, fn, fp = [], 0, 0, 0
    cls_ok = cls_n = units_ok = units_n = port_ok = port_n = 0
    for lab in labels.itertuples():
        f = DATA / "extract" / f"{lab.domain}.json"
        if not f.exists():
            continue
        res = json.loads(f.read_text())
        out = res["fields"]
        pred_cls = out.get("is_property_manager", "")
        if lab.is_property_manager in ("yes", "partly", "no"):
            cls_n += 1
            cls_ok += pred_cls == lab.is_property_manager
        want = surnames(lab.decision_maker)
        got = {d["name"].split()[-1].lower() for d in out.get("decision_makers") or [] if d.get("name")}
        tp += len(want & got)
        fn += len(want - got)
        fp += len(got - want) if want else 0
        if lab.units:
            units_n += 1
            units_ok += str(out.get("units_managed")) == lab.units
        if lab.portfolio_type and lab.portfolio_type != "unknown":
            port_n += 1
            weg, miet = "WEG" in lab.portfolio_type, "Miet" in lab.portfolio_type
            port_ok += bool(out.get("manages_weg")) == weg and bool(out.get("manages_rental")) == miet
        rows.append({"domain": lab.domain, "status": res["status"], "class": f"{pred_cls} / {lab.is_property_manager}",
                     "people_found": f"{len(want & got)}/{len(want)}", "extra_people": sorted(got - want),
                     "units": f"{out.get('units_managed')} / {lab.units or '-'}"})
    report = {
        "sites_scored": len(rows),
        "class_accuracy": round(cls_ok / cls_n, 3) if cls_n else None,
        "decision_maker_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "decision_maker_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "units_exact": f"{units_ok}/{units_n}",
        "portfolio_agreement": f"{port_ok}/{port_n}",
    }
    print(pd.DataFrame(rows).to_string(index=False), file=sys.stderr)
    print(json.dumps(report, indent=2), file=sys.stderr)
    (DATA / "eval_report.json").write_text(json.dumps({"summary": report, "sites": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
